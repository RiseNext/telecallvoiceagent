"""The text-mode conversation loop. Framework-free, bounded, no audio anywhere.

What this is for: proving that an agent definition holds a real conversation, calls
typed tools with server-injected context, and refuses what it must — before any
audio, any telephony or any realtime provider exists. It is also the harness the
evaluation suite drives, and the same registry and the same `rn_services` a live
call will use. Only the *transport* differs, which is why the registry is
framework-free.

**Every loop here is bounded, and every bound has a `StopReason`.** A provider that
returns tool calls forever, or empty turns forever, is a provider that would
otherwise spend a tenant's money until someone noticed. Specifically:

* turns per conversation — `guardrail_config.max_turns`
* tool-call rounds within one turn — `MAX_TOOL_ROUNDS_PER_TURN`
* tool calls within one round — `MAX_TOOL_CALLS_PER_ROUND`
* retries after unusable arguments — `guardrail_config.max_invalid_tool_retries`

There is no `while True` in this file.

**No state outside the function.** Everything the loop needs is a local; the result
is frozen. Two concurrent conversations share the snapshot (immutable) and the
registry (frozen) and nothing else. A module-level accumulator here would be
conversation state leaking between callers, which `CLAUDE.md` forbids outright.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from rn_agent.guardrails.disclosure import DisclosureFinding, DisclosureKind, detect_disclosure
from rn_agent.guardrails.optout import OptOutFinding, detect_opt_out
from rn_agent.instructions.render import CallInstructionContext, render_call_instructions
from rn_agent.snapshot import AgentSnapshot
from rn_agent.tools.base import ToolEnvelope, ToolOutcome, ToolRuntime
from rn_agent.tools.dispatch import dispatch_tool_call
from rn_agent.tools.registry import ToolRegistry
from rn_core.logging import get_logger
from rn_providers.llm import (
    Completion,
    FinishReason,
    LLMProvider,
    Message,
    MessageRole,
    ToolCallRequest,
    ToolSpecPayload,
    tool_result_message,
    user_message,
)

__all__ = [
    "MAX_TOOL_CALLS_PER_ROUND",
    "MAX_TOOL_ROUNDS_PER_TURN",
    "ConversationResult",
    "StopReason",
    "ToolExecutionRecord",
    "run_text_conversation",
]

_logger = get_logger(__name__)

#: How many times one caller turn may cycle through tool calls before the loop
#: gives up. A model that keeps asking for tools and never speaks is either stuck or
#: being driven somewhere; either way the caller is listening to silence.
MAX_TOOL_ROUNDS_PER_TURN = 4

#: How many tool calls may be executed from a single provider response. Providers
#: can emit several in parallel; an unbounded fan-out is a way to turn one turn into
#: an arbitrary amount of work.
MAX_TOOL_CALLS_PER_ROUND = 4


class StopReason(StrEnum):
    """Why the conversation ended. Every bound in the loop has one."""

    COMPLETED = "completed"
    """The script ran out of caller utterances. The normal ending."""
    OPT_OUT = "opt_out"
    """The caller asked not to be contacted again. Ends the conversation
    immediately — continuing to sell after an opt-out is the thing the guardrail
    exists to stop."""
    TURN_LIMIT = "turn_limit"
    TOOL_ROUND_LIMIT = "tool_round_limit"
    INVALID_ARGUMENT_LIMIT = "invalid_argument_limit"
    PROVIDER_STOPPED_EARLY = "provider_stopped_early"
    """The provider truncated or filtered a turn. Not treated as a completed turn:
    a half-said price is worse than no answer."""


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """One dispatched tool call and its envelope.

    Holds no arguments. The envelope's `data` is tenant content and this record is
    kept in memory for the caller to inspect and persist deliberately — it is never
    logged, and `rn_core`'s span filter would drop it anyway.
    """

    tool_name: str
    envelope: ToolEnvelope
    turn_index: int


@dataclass(frozen=True, slots=True)
class ConversationResult:
    """The outcome of one scripted conversation."""

    messages: tuple[Message, ...]
    tool_executions: tuple[ToolExecutionRecord, ...]
    stop_reason: StopReason
    #: Disclosure as found in the **first** assistant turn. `NONE` on a conversation
    #: where the agent never spoke, which is itself a failure.
    disclosure: DisclosureFinding = field(
        default_factory=lambda: DisclosureFinding(kind=DisclosureKind.NONE)
    )
    opt_out: OptOutFinding | None = None

    @property
    def assistant_turns(self) -> tuple[str, ...]:
        """Assistant text turns, in order. Transcript content — never log these."""
        return tuple(
            message.content
            for message in self.messages
            if message.role is MessageRole.ASSISTANT and message.content
        )

    @property
    def tool_names_called(self) -> tuple[str, ...]:
        return tuple(record.tool_name for record in self.tool_executions)

    @property
    def successful_tool_calls(self) -> int:
        return sum(1 for record in self.tool_executions if record.envelope.is_ok)


async def run_text_conversation(
    *,
    provider: LLMProvider,
    snapshot: AgentSnapshot,
    runtime: ToolRuntime,
    registry: ToolRegistry,
    caller_utterances: Sequence[str],
    call_context: CallInstructionContext | None = None,
) -> ConversationResult:
    """Run one scripted text conversation to completion.

    Args:
        provider: The LLM seam. In Phase 2 always a fake — the roadmap gate requires
            this to run in CI with no network egress.
        snapshot: The immutable agent configuration. Never mutated here.
        runtime: Server-side tool context. Its `TenantContext` is the only source of
            tenant authority in the whole loop.
        registry: The tool registry the model's calls are resolved against.
        caller_utterances: The caller's turns, in order.
        call_context: Per-call facts for instruction layer 4. Rendered once, appended
            to the snapshot prefix, and never written back into it.

    Returns:
        A frozen `ConversationResult`. Guardrail findings are reported, not acted on:
        writing a durable opt-out is Phase 9, and this function has no database.
    """
    instructions = snapshot.instruction_prefix
    if call_context is not None:
        suffix = render_call_instructions(snapshot.language_policy, call_context)
        instructions = f"{instructions}\n\n{suffix}"

    tools = snapshot.realtime_tool_specs()
    messages: list[Message] = []
    executions: list[ToolExecutionRecord] = []
    disclosure = DisclosureFinding(kind=DisclosureKind.NONE)
    disclosure_checked = False
    opt_out: OptOutFinding | None = None
    stop_reason = StopReason.COMPLETED

    max_turns = snapshot.guardrail_config.max_turns
    for turn_index, utterance in enumerate(caller_utterances):
        if turn_index >= max_turns:
            stop_reason = StopReason.TURN_LIMIT
            break

        # Opt-out runs on the caller's words BEFORE the model is asked to respond.
        # Independent of the model by design: it fires whether or not the model
        # notices, and it must not depend on the model having chosen to call a tool.
        finding = detect_opt_out(utterance)
        if finding.matched:
            opt_out = finding
            _logger.info(
                "agent.guardrail.opt_out_detected",
                organization_id=str(runtime.organization_id),
                agent_version_id=str(runtime.agent_version_id),
                call_id=str(runtime.call_id) if runtime.call_id else None,
                # The matched language, never the matched text: the excerpt is the
                # caller's own speech.
                language=finding.language.value if finding.language else None,
            )

        messages.append(user_message(utterance))

        turn = await _run_one_turn(
            provider=provider,
            snapshot=snapshot,
            runtime=runtime,
            registry=registry,
            instructions=instructions,
            messages=messages,
            tools=tools,
            turn_index=turn_index,
        )
        executions.extend(turn.executions)

        if not disclosure_checked and turn.first_assistant_text:
            disclosure = detect_disclosure(turn.first_assistant_text)
            disclosure_checked = True
            if not disclosure.disclosed:
                # A compliance finding, logged so it is visible outside a test run.
                # No transcript on the line -- only the fact of the absence.
                _logger.warning(
                    "agent.guardrail.disclosure_missing",
                    organization_id=str(runtime.organization_id),
                    agent_version_id=str(runtime.agent_version_id),
                    call_id=str(runtime.call_id) if runtime.call_id else None,
                )

        if turn.stop_reason is not None:
            stop_reason = turn.stop_reason
            break

        # An opt-out ends the conversation after the agent has acknowledged it. The
        # acknowledgement matters: hanging up mid-sentence on someone who asked to
        # be removed is a worse experience than the call they did not want.
        if opt_out is not None:
            stop_reason = StopReason.OPT_OUT
            break

    return ConversationResult(
        messages=tuple(messages),
        tool_executions=tuple(executions),
        stop_reason=stop_reason,
        disclosure=disclosure,
        opt_out=opt_out,
    )


@dataclass(frozen=True, slots=True)
class _TurnOutcome:
    #: The **first** non-empty assistant text produced during this turn, not the last.
    #:
    #: A greeting that discloses and calls a tool in the same message is one provider
    #: response with both `content` and `tool_calls` — which is what a good first turn
    #: looks like. Reporting the last text of the turn would mean the disclosure in
    #: that first message was never examined, and a compliant agent would be recorded
    #: as non-compliant.
    first_assistant_text: str | None
    executions: tuple[ToolExecutionRecord, ...]
    #: Set only when a bound was hit or the provider stopped early. `None` means the
    #: turn completed normally and the conversation continues.
    stop_reason: StopReason | None


async def _run_one_turn(
    *,
    provider: LLMProvider,
    snapshot: AgentSnapshot,
    runtime: ToolRuntime,
    registry: ToolRegistry,
    instructions: str,
    messages: list[Message],
    tools: Sequence[ToolSpecPayload],
    turn_index: int,
) -> _TurnOutcome:
    """One caller turn: provider calls and tool rounds until the agent speaks.

    Appends to `messages` in place, because the conversation is the loop's state and
    copying it per round would make the provider see a different history than the
    one that is kept.
    """
    executions: list[ToolExecutionRecord] = []
    invalid_argument_retries = 0
    max_invalid = snapshot.guardrail_config.max_invalid_tool_retries
    first_text: str | None = None

    for _round in range(MAX_TOOL_ROUNDS_PER_TURN):
        completion: Completion = await provider.complete(
            instructions=instructions,
            messages=messages,
            tools=tools,
        )
        messages.append(completion.message)
        # Captured before the tool-call branch below: a message may carry both text and
        # tool calls, and the text is the one the disclosure guardrail must see.
        if first_text is None and completion.message.content:
            first_text = completion.message.content

        if completion.finish_reason in (FinishReason.LENGTH, FinishReason.CONTENT_FILTER):
            return _TurnOutcome(
                first_assistant_text=first_text,
                executions=tuple(executions),
                stop_reason=StopReason.PROVIDER_STOPPED_EARLY,
            )

        requested = completion.message.tool_calls
        if not requested:
            return _TurnOutcome(
                first_assistant_text=first_text,
                executions=tuple(executions),
                stop_reason=None,
            )

        # Bounded fan-out. Extra calls are dropped rather than executed, and the
        # model is told nothing about them: a provider emitting more than this is
        # anomalous, and executing "as many as it asked for" is how one turn becomes
        # unbounded work.
        if len(requested) > MAX_TOOL_CALLS_PER_ROUND:
            _logger.warning(
                "agent.turn.tool_call_fanout_truncated",
                requested=len(requested),
                limit=MAX_TOOL_CALLS_PER_ROUND,
                organization_id=str(runtime.organization_id),
                agent_version_id=str(runtime.agent_version_id),
            )
            requested = requested[:MAX_TOOL_CALLS_PER_ROUND]

        for call in requested:
            envelope = await dispatch_tool_call(
                registry=registry,
                scope=snapshot,
                runtime=runtime,
                name=call.name,
                arguments_json=call.arguments_json,
            )
            executions.append(
                ToolExecutionRecord(tool_name=call.name, envelope=envelope, turn_index=turn_index)
            )
            messages.append(_tool_message(call, envelope))

            if envelope.outcome is ToolOutcome.INVALID_ARGUMENTS:
                invalid_argument_retries += 1

        if invalid_argument_retries > max_invalid:
            # The model cannot get its arguments right. Stop rather than let a
            # validation loop consume the turn budget.
            return _TurnOutcome(
                first_assistant_text=first_text,
                executions=tuple(executions),
                stop_reason=StopReason.INVALID_ARGUMENT_LIMIT,
            )

    return _TurnOutcome(
        first_assistant_text=first_text,
        executions=tuple(executions),
        stop_reason=StopReason.TOOL_ROUND_LIMIT,
    )


def _tool_message(call: ToolCallRequest, envelope: ToolEnvelope) -> Message:
    """Serialise an envelope for the model.

    Only the envelope goes back — outcome, message, data on success, field errors on
    a validation failure. No exception, no stack trace, no SQL, no provider body, no
    internal identifier. That property is enforced by the dispatcher, which never
    puts any of those into an envelope; this function simply has nothing else to
    send.

    `sort_keys` because this text is part of the conversation the provider caches,
    and a payload whose key order varies would defeat that for no reason.
    """
    payload: dict[str, object] = {
        "outcome": envelope.outcome.value,
        "message": envelope.message,
    }
    if envelope.data is not None:
        payload["data"] = dict(envelope.data)
    if envelope.field_errors:
        payload["errors"] = list(envelope.field_errors)
    if envelope.retryable:
        payload["retryable"] = True
    return tool_result_message(
        tool_call_id=call.id,
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
    )
