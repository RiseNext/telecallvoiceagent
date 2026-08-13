"""`search_knowledge` through the real registry and the real dispatcher.

Nothing here calls the handler directly. A tool is only ever invoked one sanctioned
way — `dispatch_tool_call` — and a test that bypasses it tests a function the platform
does not use: it would miss the permission check, the enabled-tool check, the argument
validation and the envelope mapping, which between them are most of what the tool
mechanism is for.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rn_agent.tools import ToolRegistry, dispatch_tool_call
from rn_agent.tools.base import Effect, ToolEnvelope, ToolOutcome, ToolRuntime, ToolServices
from rn_agent.tools.builtin import register_search_tools
from rn_agent.tools.builtin.search import MAX_RESULTS, SearchKnowledgeArgs
from rn_core.errors import InvariantViolation
from rn_core.ids import new_id
from rn_domain.identifiers import AgentVersionId, KnowledgeBaseId, OrganizationId
from rn_domain.permissions import ORG_PERMISSIONS
from rn_domain.tenancy import TenantContext
from rn_services.contracts import RetrievalResult, RetrievedChunk

pytestmark = pytest.mark.unit

ORGANIZATION_ID = OrganizationId(new_id())
AGENT_VERSION_ID = AgentVersionId(new_id())
BASE_ID = KnowledgeBaseId(new_id())


class _StubRetriever:
    """An in-memory `KnowledgeRetriever`. Records what it was asked."""

    def __init__(self, contents: Sequence[str] = ()) -> None:
        self._contents = list(contents)
        self.calls: list[tuple[str, int]] = []

    async def search(
        self,
        *,
        query: str,
        knowledge_base_ids: Sequence[KnowledgeBaseId] | None = None,
        k: int,
    ) -> RetrievalResult:
        self.calls.append((query, k))
        chunks = tuple(
            RetrievedChunk(
                chunk_id=f"chunk-{position}",
                knowledge_base_id=BASE_ID,
                knowledge_base_name="Services",
                content=content,
                score=1.0 - position / 10,
                embedding_model="stub-model",
            )
            for position, content in enumerate(self._contents[:k])
        )
        return RetrievalResult(chunks=chunks, requested_k=k, embedding_model="stub-model")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_search_tools(registry)
    return registry


def _runtime(
    *,
    retriever: _StubRetriever | None = None,
    permissions: frozenset[str] = frozenset({"org:knowledge:read"}),
) -> ToolRuntime:
    return ToolRuntime(
        context=TenantContext(organization_id=ORGANIZATION_ID, permissions=permissions),
        agent_version_id=AGENT_VERSION_ID,
        services=ToolServices(retrieval=retriever),
    )


class _Scope:
    def __init__(self, enabled: frozenset[str] = frozenset({"search_knowledge"})) -> None:
        self._enabled = enabled

    @property
    def organization_id(self) -> OrganizationId:
        return ORGANIZATION_ID

    @property
    def agent_version_id(self) -> AgentVersionId:
        return AGENT_VERSION_ID

    @property
    def enabled_tools(self) -> frozenset[str]:
        return self._enabled


async def _dispatch(
    arguments_json: str,
    *,
    retriever: _StubRetriever | None = None,
    scope: _Scope | None = None,
    permissions: frozenset[str] = frozenset({"org:knowledge:read"}),
) -> ToolEnvelope:
    return await dispatch_tool_call(
        registry=_registry(),
        scope=scope or _Scope(),
        runtime=_runtime(retriever=retriever, permissions=permissions),
        name="search_knowledge",
        arguments_json=arguments_json,
    )


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_it_is_read_only_and_uses_an_already_frozen_permission() -> None:
    """The permission is what decides whether this tool needed a migration. It did not:
    `org:knowledge:read` has been in the frozen catalog since migration `0001`, and the
    registry refuses any permission that is not."""
    spec = _registry().get("search_knowledge")
    assert spec is not None
    assert spec.effect is Effect.READ
    assert spec.permission == "org:knowledge:read"
    assert spec.permission in ORG_PERMISSIONS


def test_the_exported_schema_is_flat_and_carries_no_injected_context() -> None:
    """The Realtime tool schema is flat, and getting it wrong fails **silently**: the
    session accepts a nested spec and the model then never calls the tool."""
    spec = _registry().get("search_knowledge")
    assert spec is not None
    exported = spec.realtime_spec
    assert "function" not in exported
    assert set(exported) == {"type", "name", "description", "parameters"}
    assert set(exported["parameters"]["properties"]) == {"query", "limit"}


def test_the_argument_model_forbids_extra_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        SearchKnowledgeArgs(query="websites", organization_id="forged")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def test_a_successful_search_returns_content_and_a_source() -> None:
    retriever = _StubRetriever(["We build websites.", "We run digital marketing."])
    envelope = await _dispatch('{"query": "what do you do?"}', retriever=retriever)

    assert envelope.outcome is ToolOutcome.OK
    data = envelope.data
    assert data is not None
    assert data["count"] == 2
    assert data["results"][0] == {"content": "We build websites.", "source": "Services"}


async def test_the_envelope_carries_no_identifier_the_model_could_repeat() -> None:
    """No chunk id, no knowledge-base id, no score. The model has no use for any of
    them, and an identifier in its context is one more thing it can say out loud."""
    retriever = _StubRetriever(["We build websites."])
    envelope = await _dispatch('{"query": "websites"}', retriever=retriever)

    assert envelope.data is not None
    result = envelope.data["results"][0]
    assert set(result) == {"content", "source"}


async def test_the_default_limit_is_what_reaches_the_retriever() -> None:
    retriever = _StubRetriever(["a", "b", "c", "d", "e"])
    await _dispatch('{"query": "websites"}', retriever=retriever)
    assert retriever.calls == [("websites", 3)]


async def test_an_empty_result_is_ok_not_not_found() -> None:
    """A fuzzy query matching nothing is an ordinary conversational moment the agent
    should speak to, unlike `find_knowledge_base`, which is given an exact name."""
    envelope = await _dispatch('{"query": "quantum submarines"}', retriever=_StubRetriever())

    assert envelope.outcome is ToolOutcome.OK
    assert envelope.data is not None
    assert envelope.data["count"] == 0
    assert envelope.message  # something sayable, not an empty string


async def test_a_limit_above_the_ceiling_is_invalid_arguments() -> None:
    envelope = await _dispatch(
        f'{{"query": "websites", "limit": {MAX_RESULTS + 1}}}', retriever=_StubRetriever(["a"])
    )
    assert envelope.outcome is ToolOutcome.INVALID_ARGUMENTS


async def test_a_forged_organization_id_is_discarded_and_the_call_proceeds() -> None:
    """The dispatcher strips server-owned argument names *before* validation and logs
    the attempt, rather than letting `extra="forbid"` turn a prompt-injection attempt
    into an ordinary validation error and losing the signal.

    So the forged key changes nothing: the call runs, and the tenant it runs in is the
    runtime's, which came from a verified identity and was never in the arguments.
    """
    retriever = _StubRetriever(["a"])
    envelope = await _dispatch(
        '{"query": "websites", "organization_id": "00000000-0000-0000-0000-000000000000"}',
        retriever=retriever,
    )

    assert envelope.outcome is ToolOutcome.OK
    assert retriever.calls == [("websites", 3)]
    # And the forged value reached nothing: the retriever has no organization parameter
    # to have received it through, which is the structural half of the guarantee.
    assert "organization_id" not in SearchKnowledgeArgs.model_fields


async def test_without_the_permission_it_is_denied() -> None:
    envelope = await _dispatch(
        '{"query": "websites"}', retriever=_StubRetriever(["a"]), permissions=frozenset()
    )
    assert envelope.outcome is ToolOutcome.DENIED


async def test_a_tool_the_agent_version_does_not_enable_is_denied() -> None:
    envelope = await _dispatch(
        '{"query": "websites"}',
        retriever=_StubRetriever(["a"]),
        scope=_Scope(enabled=frozenset()),
    )
    assert envelope.outcome is ToolOutcome.DENIED


async def test_an_unwired_retriever_is_a_wiring_bug_not_a_refusal() -> None:
    """Telling the model "unavailable" would hide a broken deployment behind something
    that looks like an outage. The model did nothing wrong."""
    with pytest.raises(InvariantViolation):
        await _dispatch('{"query": "websites"}', retriever=None)
