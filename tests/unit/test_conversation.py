"""The text conversation loop: bounds, tool flow, guardrail wiring.

Every bound in the loop has a test here, because an unbounded loop against a paid
provider is a cost incident and, on a phone call, a caller trapped with an agent that
never speaks. A provider that misbehaves — tool calls forever, empty turns forever,
truncated turns — must terminate the conversation with a named reason, not keep going.

Zero network: the provider is `FakeLLMProvider` with a scripted tape, and the tool
services are in-memory.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rn_agent.conversation import (
    MAX_TOOL_CALLS_PER_ROUND,
    MAX_TOOL_ROUNDS_PER_TURN,
    StopReason,
    run_text_conversation,
)
from rn_agent.guardrails.disclosure import DisclosureKind
from rn_agent.instructions.render import CallInstructionContext
from rn_agent.snapshot import AgentSnapshot, build_snapshot
from rn_agent.tools import REGISTRY
from rn_agent.tools.base import ToolOutcome, ToolRuntime, ToolServices
from rn_core.clock import now_utc
from rn_core.errors import NotFoundError
from rn_core.ids import new_id
from rn_domain.entities.agents import AgentVersion
from rn_domain.enums import AgentVersionStatus
from rn_domain.identifiers import AgentId, AgentVersionId, CallId, OrganizationId, UserId
from rn_domain.tenancy import TenantContext
from rn_domain.values import LanguagePolicy, LanguageTag
from rn_providers.fakes import FakeLLMProvider, ScriptedToolCall, ScriptedTurn
from rn_providers.fakes.llm import ScriptExhaustedError
from rn_providers.llm import FinishReason, MessageRole
from rn_services.contracts import KnowledgeBaseSummary

pytestmark = [pytest.mark.unit]

_ORG = OrganizationId(new_id())
_VERSION = AgentVersionId(new_id())
_POLICY = LanguagePolicy(
    primary=LanguageTag("en"),
    allowed=(LanguageTag("en"), LanguageTag("hi-IN"), LanguageTag("te-IN")),
)


class _FakeCatalog:
    def __init__(self, names: Sequence[str] = ("Pricing", "Support")) -> None:
        self._names = list(names)

    async def list_knowledge_bases(self, *, limit: int) -> Sequence[KnowledgeBaseSummary]:
        return [
            KnowledgeBaseSummary(id=new_id(), name=name, description=None)  # type: ignore[arg-type]
            for name in self._names[:limit]
        ]

    async def find_knowledge_base(self, *, name: str) -> KnowledgeBaseSummary:
        if name not in self._names:
            raise NotFoundError("Knowledge base not found.", detail={"name": name})
        return KnowledgeBaseSummary(id=new_id(), name=name, description=None)  # type: ignore[arg-type]


def _snapshot(**guardrails: object) -> AgentSnapshot:
    version = AgentVersion(
        id=_VERSION,
        organization_id=_ORG,
        agent_id=AgentId(new_id()),
        version_number=1,
        instructions="You are a helpful assistant for a software services company.",
        language_policy=_POLICY,
        status=AgentVersionStatus.PUBLISHED,
        published_at=now_utc(),
        guardrail_config=dict(guardrails),
    )
    return build_snapshot(
        version=version,
        enabled_tool_names=["list_knowledge_bases", "find_knowledge_base"],
        knowledge_base_ids=(),
        registry=REGISTRY,
    )


def _runtime(*, catalog: _FakeCatalog | None = None) -> ToolRuntime:
    return ToolRuntime(
        context=TenantContext(
            organization_id=_ORG,
            actor_id=UserId(new_id()),
            permissions=frozenset({"org:knowledge:read"}),
        ),
        agent_version_id=_VERSION,
        call_id=CallId(new_id()),
        services=ToolServices(knowledge=catalog or _FakeCatalog()),
    )


async def _run(
    script: Sequence[ScriptedTurn],
    utterances: Sequence[str],
    *,
    snapshot: AgentSnapshot | None = None,
    catalog: _FakeCatalog | None = None,
    call_context: CallInstructionContext | None = None,
) -> object:
    return await run_text_conversation(
        provider=FakeLLMProvider(script),
        snapshot=snapshot or _snapshot(),
        runtime=_runtime(catalog=catalog),
        registry=REGISTRY,
        caller_utterances=utterances,
        call_context=call_context,
    )


# ---------------------------------------------------------------------------
# The happy path, and the Phase-2 gate
# ---------------------------------------------------------------------------


async def test_a_conversation_with_two_tool_calls_runs_end_to_end() -> None:
    """The Phase-2 definition-of-done gate: at least two tool calls, no network."""
    result = await _run(
        [
            ScriptedTurn(
                content="Hello, I'm an AI assistant from Acme. Let me check what I can help with.",
                tool_calls=(ScriptedToolCall(name="list_knowledge_bases", arguments={"limit": 2}),),
                expect_tool_offered="list_knowledge_bases",
            ),
            ScriptedTurn(content="I can help with Pricing and Support."),
            ScriptedTurn(
                tool_calls=(
                    ScriptedToolCall(name="find_knowledge_base", arguments={"name": "Pricing"}),
                ),
                expect_last_user_contains="pricing",
            ),
            ScriptedTurn(content="Yes, I have information about Pricing."),
        ],
        ["What can you help me with?", "Tell me about pricing."],
    )
    assert result.stop_reason is StopReason.COMPLETED  # type: ignore[attr-defined]
    assert result.tool_names_called == (  # type: ignore[attr-defined]
        "list_knowledge_bases",
        "find_knowledge_base",
    )
    assert result.successful_tool_calls == 2  # type: ignore[attr-defined]


async def test_tool_results_reach_the_model_as_the_envelope_only() -> None:
    """No exception, no stack trace, no SQL, no internal identifier."""
    provider = FakeLLMProvider(
        [
            ScriptedTurn(
                tool_calls=(
                    ScriptedToolCall(name="find_knowledge_base", arguments={"name": "Nope"}),
                )
            ),
            ScriptedTurn(content="Sorry, I don't have that."),
        ]
    )
    await run_text_conversation(
        provider=provider,
        snapshot=_snapshot(),
        runtime=_runtime(),
        registry=REGISTRY,
        caller_utterances=["Do you know about Nope?"],
    )
    tool_messages = [
        m.content or ""
        for call in provider.calls
        for m in call.messages
        if m.role is MessageRole.TOOL
    ]
    assert tool_messages
    joined = " ".join(tool_messages)
    assert "not_found" in joined
    for forbidden in ("Traceback", "NotFoundError", "SELECT", str(_ORG), str(_VERSION)):
        assert forbidden not in joined


async def test_the_instruction_prefix_reaches_the_provider_unchanged() -> None:
    """A per-call value baked into the prefix would break prompt caching silently."""
    snapshot = _snapshot()
    provider = FakeLLMProvider([ScriptedTurn(content="Hi, I'm an AI assistant.")])
    await run_text_conversation(
        provider=provider,
        snapshot=snapshot,
        runtime=_runtime(),
        registry=REGISTRY,
        caller_utterances=["Hello"],
    )
    assert provider.calls[0].instructions == snapshot.instruction_prefix


async def test_layer_four_is_appended_and_the_snapshot_is_untouched() -> None:
    snapshot = _snapshot()
    before = snapshot.instruction_prefix
    provider = FakeLLMProvider([ScriptedTurn(content="Hi Priya, I'm an AI assistant.")])
    await run_text_conversation(
        provider=provider,
        snapshot=snapshot,
        runtime=_runtime(),
        registry=REGISTRY,
        caller_utterances=["Hello"],
        call_context=CallInstructionContext(caller_name="Priya"),
    )
    sent = provider.calls[0].instructions
    assert sent.startswith(before)
    assert "Priya" in sent
    assert snapshot.instruction_prefix == before  # the snapshot was not rewritten


async def test_only_enabled_tools_are_offered() -> None:
    snapshot = build_snapshot(
        version=AgentVersion(
            id=_VERSION,
            organization_id=_ORG,
            agent_id=AgentId(new_id()),
            version_number=1,
            instructions="You are a helpful assistant for a software services company.",
            language_policy=_POLICY,
            status=AgentVersionStatus.PUBLISHED,
            published_at=now_utc(),
        ),
        enabled_tool_names=["list_knowledge_bases"],
        knowledge_base_ids=(),
        registry=REGISTRY,
    )
    provider = FakeLLMProvider([ScriptedTurn(content="Hi, I'm an AI assistant.")])
    await run_text_conversation(
        provider=provider,
        snapshot=snapshot,
        runtime=_runtime(),
        registry=REGISTRY,
        caller_utterances=["Hello"],
    )
    assert provider.calls[0].tool_names == ("list_knowledge_bases",)


# ---------------------------------------------------------------------------
# Guardrail wiring
# ---------------------------------------------------------------------------


async def test_disclosure_is_read_from_the_first_assistant_turn() -> None:
    result = await _run(
        [
            ScriptedTurn(content="Hello, I'm an AI assistant calling from Acme."),
            ScriptedTurn(content="Anything else?"),
        ],
        ["Hi", "Thanks"],
    )
    assert result.disclosure.kind is DisclosureKind.AFFIRMED_AI  # type: ignore[attr-defined]


async def test_disclosure_is_found_when_it_shares_a_message_with_a_tool_call() -> None:
    """A regression guard for a bug this suite caught.

    A good greeting discloses *and* starts working: one provider response carrying both
    `content` and `tool_calls`. Reading the last text of the turn instead of the first
    meant that disclosure was never examined, and a fully compliant agent was recorded
    as non-compliant — a compliance false negative, which is the worst direction.
    """
    result = await _run(
        [
            ScriptedTurn(
                content="Hello, I'm an AI assistant. Let me check what I can help with.",
                tool_calls=(ScriptedToolCall(name="list_knowledge_bases", arguments={"limit": 2}),),
            ),
            ScriptedTurn(content="I can help with Pricing and Support."),
        ],
        ["What can you do?"],
    )
    assert result.disclosure.kind is DisclosureKind.AFFIRMED_AI  # type: ignore[attr-defined]
    assert result.successful_tool_calls == 1  # type: ignore[attr-defined]


async def test_a_missing_disclosure_is_reported_not_hidden() -> None:
    """Detective, not preventive. We cannot constrain generated speech token by token,
    so absence becomes a finding — and a hard failure in evaluation."""
    result = await _run([ScriptedTurn(content="Hello! How can I help?")], ["Hi"])
    assert result.disclosure.kind is DisclosureKind.NONE  # type: ignore[attr-defined]


async def test_a_late_disclosure_does_not_count_as_a_first_turn_disclosure() -> None:
    result = await _run(
        [
            ScriptedTurn(content="Hello! How can I help?"),
            ScriptedTurn(content="By the way, I'm an AI assistant."),
        ],
        ["Hi", "Are you a person?"],
    )
    assert result.disclosure.kind is DisclosureKind.NONE  # type: ignore[attr-defined]


async def test_an_opt_out_ends_the_conversation_after_an_acknowledgement() -> None:
    """The acknowledgement matters: hanging up mid-sentence on someone who asked to be
    removed is a worse experience than the call they did not want."""
    result = await _run(
        [
            ScriptedTurn(content="Hello, I'm an AI assistant from Acme."),
            ScriptedTurn(content="Of course — I've noted that and we won't call again."),
            ScriptedTurn(content="This turn should never be reached."),
        ],
        ["Hi", "Please stop calling me.", "Anything else?"],
    )
    assert result.stop_reason is StopReason.OPT_OUT  # type: ignore[attr-defined]
    assert result.opt_out is not None and result.opt_out.matched  # type: ignore[attr-defined]
    # The agent got its acknowledgement turn, and then the third utterance was
    # never sent.
    assert len(result.assistant_turns) == 2  # type: ignore[attr-defined]


async def test_opt_out_detection_does_not_depend_on_the_model() -> None:
    """It fires whether or not the model notices, and whether or not it calls a tool."""
    result = await _run(
        [ScriptedTurn(content="Sure, let me tell you about our great offers!")],
        ["Remove me from your list."],
    )
    assert result.opt_out is not None and result.opt_out.matched  # type: ignore[attr-defined]


async def test_a_negated_opt_out_does_not_end_the_conversation() -> None:
    result = await _run(
        [
            ScriptedTurn(content="I'm an AI assistant — glad to hear it!"),
            ScriptedTurn(content="Happy to keep you posted."),
        ],
        ["Don't stop calling me, I like the updates.", "Great."],
    )
    assert result.opt_out is None  # type: ignore[attr-defined]
    assert result.stop_reason is StopReason.COMPLETED  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Bounds — one test per bound
# ---------------------------------------------------------------------------


async def test_the_turn_limit_is_enforced() -> None:
    snapshot = _snapshot(max_turns=2)
    result = await _run(
        [ScriptedTurn(content=f"Turn {i}, I'm an AI assistant.") for i in range(5)],
        ["one", "two", "three", "four", "five"],
        snapshot=snapshot,
    )
    assert result.stop_reason is StopReason.TURN_LIMIT  # type: ignore[attr-defined]
    assert len(result.assistant_turns) == 2  # type: ignore[attr-defined]


async def test_a_provider_that_only_ever_calls_tools_terminates() -> None:
    """A model stuck in a tool loop leaves the caller listening to silence."""
    result = await _run(
        [
            ScriptedTurn(
                tool_calls=(ScriptedToolCall(name="list_knowledge_bases", arguments={"limit": 1}),)
            )
            for _ in range(MAX_TOOL_ROUNDS_PER_TURN + 3)
        ],
        ["Hello"],
    )
    assert result.stop_reason is StopReason.TOOL_ROUND_LIMIT  # type: ignore[attr-defined]
    assert len(result.tool_executions) == MAX_TOOL_ROUNDS_PER_TURN  # type: ignore[attr-defined]


async def test_tool_call_fan_out_is_bounded_within_one_round() -> None:
    """A provider emitting many parallel calls must not turn one turn into
    arbitrary work."""
    result = await _run(
        [
            ScriptedTurn(
                tool_calls=tuple(
                    ScriptedToolCall(name="list_knowledge_bases", arguments={"limit": 1})
                    for _ in range(MAX_TOOL_CALLS_PER_ROUND + 4)
                )
            ),
            ScriptedTurn(content="Here is what I found."),
        ],
        ["Hello"],
    )
    assert len(result.tool_executions) == MAX_TOOL_CALLS_PER_ROUND  # type: ignore[attr-defined]


async def test_repeated_invalid_arguments_stop_the_turn() -> None:
    """A validation loop would otherwise eat the whole turn budget."""
    result = await _run(
        [
            ScriptedTurn(
                tool_calls=(
                    ScriptedToolCall(name="find_knowledge_base", arguments_json='{"bad": 1}'),
                )
            )
            for _ in range(MAX_TOOL_ROUNDS_PER_TURN)
        ],
        ["Hello"],
        snapshot=_snapshot(max_invalid_tool_retries=1),
    )
    assert result.stop_reason is StopReason.INVALID_ARGUMENT_LIMIT  # type: ignore[attr-defined]
    assert all(
        record.envelope.outcome is ToolOutcome.INVALID_ARGUMENTS
        for record in result.tool_executions  # type: ignore[attr-defined]
    )


async def test_a_truncated_turn_is_not_treated_as_a_completed_one() -> None:
    """A half-said price is worse than no answer."""
    result = await _run(
        [
            ScriptedTurn(
                content="I'm an AI assistant. Our price is four thousand and",
                finish_reason=FinishReason.LENGTH,
            )
        ],
        ["How much?"],
    )
    assert result.stop_reason is StopReason.PROVIDER_STOPPED_EARLY  # type: ignore[attr-defined]


async def test_a_filtered_turn_stops_the_conversation() -> None:
    result = await _run(
        [ScriptedTurn(content=None, finish_reason=FinishReason.CONTENT_FILTER)], ["Hello"]
    )
    assert result.stop_reason is StopReason.PROVIDER_STOPPED_EARLY  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The fake's own guarantees
# ---------------------------------------------------------------------------


async def test_an_exhausted_tape_raises_rather_than_returning_a_default() -> None:
    """A fake that returned something bland on exhaustion would let a loop bug — one
    extra provider round trip per turn — pass silently forever."""
    with pytest.raises(ScriptExhaustedError):
        await _run([ScriptedTurn(content="Only one turn scripted.")], ["one", "two"])


async def test_a_tape_expectation_failure_is_loud() -> None:
    """Without expectations, a reordered loop still produces the scripted output and
    the test still passes — the classic way scripted tests rot."""
    with pytest.raises(ScriptExhaustedError):
        await _run(
            [ScriptedTurn(content="Hi", expect_last_user_contains="something else entirely")],
            ["Hello"],
        )


async def test_no_state_is_shared_between_two_conversations() -> None:
    """Two callers, one snapshot, one registry, nothing else in common."""
    snapshot = _snapshot()
    first = await run_text_conversation(
        provider=FakeLLMProvider([ScriptedTurn(content="First caller. I'm an AI assistant.")]),
        snapshot=snapshot,
        runtime=_runtime(),
        registry=REGISTRY,
        caller_utterances=["Hello"],
    )
    second = await run_text_conversation(
        provider=FakeLLMProvider([ScriptedTurn(content="Second caller. I'm an AI assistant.")]),
        snapshot=snapshot,
        runtime=_runtime(),
        registry=REGISTRY,
        caller_utterances=["Hello"],
    )
    assert first.messages != second.messages
    assert len(first.messages) == len(second.messages) == 2
    assert first.tool_executions == () and second.tool_executions == ()
