"""A scripted `LLMProvider`. Deterministic, offline, self-checking.

The tape is a flat sequence: the *n*-th `complete()` call returns the *n*-th
`ScriptedTurn`. That is enough to express a full multi-turn tool flow —

    turn 1: assistant asks for `list_knowledge_bases`
    turn 2: assistant answers using the tool result
    turn 3: assistant answers the next caller utterance

— and it keeps the fake free of branching logic that would need its own tests.

Two properties that stop scripted tests from rotting:

**A tape that runs out raises.** `ScriptExhaustedError`, immediately. A fake that
returned a bland default on exhaustion would let a loop bug — one extra provider
round trip per turn — pass silently forever.

**A turn may assert what it expects to see.** `expect_last_user_contains` and
`expect_tool_result_for` are checked against the conversation the loop actually
passed in. Without them a reordered loop still produces the scripted output and
the test still passes, which is the failure mode scripted tests are famous for.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rn_core.errors import ApplicationError, ErrorCode
from rn_providers.llm import (
    Completion,
    FinishReason,
    Message,
    MessageRole,
    ToolCallRequest,
    ToolSpecPayload,
    Usage,
    assistant_message,
)

__all__ = ["FakeLLMProvider", "ScriptExhaustedError", "ScriptedToolCall", "ScriptedTurn"]


class ScriptExhaustedError(ApplicationError):
    """The conversation asked for more turns than the tape holds.

    Always a bug in the test or in the loop's bounding, never a provider
    condition — which is why it is a hard error rather than a `FinishReason`.
    """

    code = ErrorCode.INVARIANT


@dataclass(frozen=True, slots=True)
class ScriptedToolCall:
    """A tool call the scripted model will emit.

    `arguments` is serialised to JSON on the way out, matching what a real
    provider sends. `arguments_json` overrides it verbatim, which is how tests
    script malformed JSON, unknown fields and forged context keys — the cases
    that matter most and that a typed `arguments` mapping cannot express.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    arguments_json: str | None = None
    call_id: str | None = None

    def to_request(self, *, index: int) -> ToolCallRequest:
        payload = (
            self.arguments_json
            if self.arguments_json is not None
            # sort_keys so a tape produces byte-identical output across runs.
            else json.dumps(dict(self.arguments), sort_keys=True, separators=(",", ":"))
        )
        return ToolCallRequest(
            # Derived from the position in the tape, never from a clock or a
            # random source: a scripted run must be reproducible byte for byte.
            id=self.call_id or f"scripted-call-{index}",
            name=self.name,
            arguments_json=payload,
        )


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One provider response, plus optional assertions about its input."""

    content: str | None = None
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    finish_reason: FinishReason | None = None
    #: Fail unless the most recent user message contains this text
    #: (case-insensitive). Catches a loop that sends turns out of order.
    expect_last_user_contains: str | None = None
    #: Fail unless the conversation already contains a tool result for this tool.
    #: Catches a loop that never submits results back to the model.
    expect_tool_result_for: str | None = None
    #: Fail unless this tool is present in the offered specs. Catches a snapshot
    #: whose enabled set silently stopped reaching the provider.
    expect_tool_offered: str | None = None

    def resolved_finish_reason(self) -> FinishReason:
        if self.finish_reason is not None:
            return self.finish_reason
        return FinishReason.TOOL_CALLS if self.tool_calls else FinishReason.STOP


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """What the loop actually asked for. Inspected by assertions in tests."""

    instructions: str
    messages: tuple[Message, ...]
    tool_names: tuple[str, ...]
    max_output_tokens: int | None


class FakeLLMProvider:
    """A deterministic, offline `LLMProvider`.

    Satisfies the protocol structurally; it is not a subclass, because the
    protocol is exactly the contract and inheritance would add nothing.
    """

    def __init__(
        self,
        script: Iterable[ScriptedTurn],
        *,
        model_id: str = "fake-llm-1",
    ) -> None:
        self._script: tuple[ScriptedTurn, ...] = tuple(script)
        self._model_id = model_id
        self._index = 0
        self._calls: list[RecordedCall] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def calls(self) -> tuple[RecordedCall, ...]:
        """Every `complete()` invocation, in order."""
        return tuple(self._calls)

    @property
    def remaining(self) -> int:
        """Unused scripted turns. A test asserting 0 has proved the loop stopped
        where the tape expected, which a passing assertion on the output does not."""
        return len(self._script) - self._index

    async def complete(
        self,
        *,
        instructions: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpecPayload] = (),
        max_output_tokens: int | None = None,
    ) -> Completion:
        if self._index >= len(self._script):
            raise ScriptExhaustedError(
                "The scripted LLM tape is exhausted.",
                detail={"scripted_turns": len(self._script), "requested": self._index + 1},
            )

        turn = self._script[self._index]
        offered = tuple(str(spec.get("name", "")) for spec in tools)
        self._calls.append(
            RecordedCall(
                instructions=instructions,
                messages=tuple(messages),
                tool_names=offered,
                max_output_tokens=max_output_tokens,
            )
        )
        self._assert_expectations(turn, messages, offered)
        self._index += 1

        return Completion(
            message=assistant_message(
                turn.content,
                tool_calls=[
                    call.to_request(index=self._index * 10 + position)
                    for position, call in enumerate(turn.tool_calls)
                ],
            ),
            finish_reason=turn.resolved_finish_reason(),
            # Fixed rather than derived from token counts: a fake that invents
            # plausible usage numbers invites someone to assert on them.
            usage=Usage(input_tokens=0, cached_input_tokens=0, output_tokens=0),
            provider_response_id=f"scripted-response-{self._index}",
        )

    def _assert_expectations(
        self,
        turn: ScriptedTurn,
        messages: Sequence[Message],
        offered: tuple[str, ...],
    ) -> None:
        if turn.expect_last_user_contains is not None:
            last_user = next(
                (m.content or "" for m in reversed(messages) if m.role is MessageRole.USER),
                "",
            )
            if turn.expect_last_user_contains.lower() not in last_user.lower():
                raise ScriptExhaustedError(
                    "Scripted turn expectation failed: unexpected last user message.",
                    detail={
                        "turn": self._index,
                        "expected_substring": turn.expect_last_user_contains,
                    },
                )
        if turn.expect_tool_result_for is not None and not any(
            m.role is MessageRole.TOOL and turn.expect_tool_result_for in (m.content or "")
            for m in messages
        ):
            raise ScriptExhaustedError(
                "Scripted turn expectation failed: no tool result for the expected tool.",
                detail={"turn": self._index, "expected_tool": turn.expect_tool_result_for},
            )
        if turn.expect_tool_offered is not None and turn.expect_tool_offered not in offered:
            raise ScriptExhaustedError(
                "Scripted turn expectation failed: expected tool was not offered.",
                detail={"turn": self._index, "expected_tool": turn.expect_tool_offered},
            )
