"""The tool execution pipeline, adversarially.

Two properties are worth more than the rest of this file combined:

1. **The model never sees an exception.** Whatever a handler does — raise a typed
   error, raise something nobody anticipated, hang past its deadline — the model gets
   a `ToolEnvelope`. A stack trace handed to a model is a stack trace an agent reads
   out loud on the phone.

2. **Forged trusted identity changes nothing.** A model that supplies
   `organization_id` in its arguments does not get to influence the tenant. The value
   is discarded, the attempt is recorded, and the call runs against the server's
   tenant or not at all.

No database, no network. `ToolServices` handles are in-memory fakes, which is the
whole reason they are protocols.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import timedelta

import pytest

from rn_agent.errors import ToolBlocked
from rn_agent.tools.base import (
    Effect,
    ToolArgs,
    ToolOutcome,
    ToolReply,
    ToolRuntime,
    ToolServices,
)
from rn_agent.tools.dispatch import dispatch_tool_call
from rn_agent.tools.registry import ToolRegistry
from rn_core.errors import (
    ApplicationError,
    ConflictError,
    InvariantViolation,
    NotFoundError,
    ProviderError,
    RateLimitError,
    TransientError,
    ValidationError,
)
from rn_core.ids import new_id
from rn_domain.identifiers import AgentVersionId, OrganizationId, UserId
from rn_domain.tenancy import TenantContext
from rn_services.agents import KnowledgeBaseSummary

pytestmark = [pytest.mark.unit]

_ORG = OrganizationId(new_id())
_VERSION = AgentVersionId(new_id())
_PERMISSION = "org:knowledge:read"


class _Scope:
    """A minimal `AgentToolScope`. Three facts is all the dispatcher needs."""

    def __init__(
        self,
        *,
        enabled: frozenset[str],
        organization_id: OrganizationId = _ORG,
        agent_version_id: AgentVersionId = _VERSION,
    ) -> None:
        self.enabled_tools = enabled
        self.organization_id = organization_id
        self.agent_version_id = agent_version_id


class _FakeCatalog:
    """In-memory `KnowledgeCatalog`."""

    def __init__(self, names: Sequence[str] = ()) -> None:
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


class _Args(ToolArgs):
    query: str
    count: int = 1


def _runtime(
    *,
    permissions: frozenset[str] = frozenset({_PERMISSION}),
    organization_id: OrganizationId = _ORG,
    agent_version_id: AgentVersionId = _VERSION,
    catalog: _FakeCatalog | None = None,
) -> ToolRuntime:
    return ToolRuntime(
        context=TenantContext(
            organization_id=organization_id,
            actor_id=UserId(new_id()),
            permissions=permissions,
        ),
        agent_version_id=agent_version_id,
        services=ToolServices(knowledge=catalog),
    )


def _registry_with(
    handler: object, *, name: str = "a_tool", timeout_seconds: float = 3
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.tool(
        name=name,
        description="Looks something up for the caller and returns what it found.",
        args=_Args,
        effect=Effect.READ,
        permission=_PERMISSION,
        timeout=timedelta(seconds=timeout_seconds),
    )(handler)  # type: ignore[arg-type]
    return registry


async def _ok_handler(args: _Args, rt: ToolRuntime) -> ToolReply:
    return ToolReply(data={"echo": args.query, "count": args.count}, message="Done.")


async def _dispatch(
    registry: ToolRegistry,
    *,
    scope: _Scope | None = None,
    runtime: ToolRuntime | None = None,
    name: str = "a_tool",
    arguments_json: str = '{"query": "hello"}',
) -> object:
    return await dispatch_tool_call(
        registry=registry,
        scope=scope or _Scope(enabled=frozenset({"a_tool"})),
        runtime=runtime or _runtime(),
        name=name,
        arguments_json=arguments_json,
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_permitted_enabled_tool_runs() -> None:
    envelope = await _dispatch(_registry_with(_ok_handler))
    assert envelope.outcome is ToolOutcome.OK  # type: ignore[attr-defined]
    assert envelope.data == {"echo": "hello", "count": 1}  # type: ignore[attr-defined]


async def test_defaults_apply_and_the_runtime_reaches_the_handler() -> None:
    seen: dict[str, object] = {}

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        seen["organization_id"] = rt.organization_id
        seen["agent_version_id"] = rt.agent_version_id
        return ToolReply(data={})

    await _dispatch(_registry_with(handler))
    assert seen == {"organization_id": _ORG, "agent_version_id": _VERSION}


# ---------------------------------------------------------------------------
# Authorization: two independent checks
# ---------------------------------------------------------------------------


async def test_an_unenabled_tool_returns_a_structured_refusal_not_an_exception() -> None:
    """A Phase-2 definition-of-done gate, stated exactly as the roadmap states it."""
    envelope = await _dispatch(
        _registry_with(_ok_handler),
        scope=_Scope(enabled=frozenset()),
    )
    assert envelope.outcome is ToolOutcome.DENIED  # type: ignore[attr-defined]
    assert not envelope.retryable  # type: ignore[attr-defined]


async def test_a_tool_the_organization_may_not_use_is_denied() -> None:
    """Separate from enablement: tenant entitlements change without a new version."""
    envelope = await _dispatch(
        _registry_with(_ok_handler),
        runtime=_runtime(permissions=frozenset()),
    )
    assert envelope.outcome is ToolOutcome.DENIED  # type: ignore[attr-defined]


async def test_an_unknown_tool_name_is_denied() -> None:
    envelope = await _dispatch(_registry_with(_ok_handler), name="no_such_tool")
    assert envelope.outcome is ToolOutcome.DENIED  # type: ignore[attr-defined]


async def test_every_refusal_reads_identically_to_the_model() -> None:
    """A model that can distinguish "no such tool" from "not permitted" is an
    enumeration oracle, and it is reachable by anyone who can phone the number."""
    unknown = await _dispatch(_registry_with(_ok_handler), name="no_such_tool")
    unenabled = await _dispatch(_registry_with(_ok_handler), scope=_Scope(enabled=frozenset()))
    unpermitted = await _dispatch(
        _registry_with(_ok_handler), runtime=_runtime(permissions=frozenset())
    )
    messages = {e.message for e in (unknown, unenabled, unpermitted)}  # type: ignore[attr-defined]
    assert len(messages) == 1


async def test_an_unenabled_tool_is_never_executed() -> None:
    executed = False

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        nonlocal executed
        executed = True
        return ToolReply(data={})

    await _dispatch(_registry_with(handler), scope=_Scope(enabled=frozenset()))
    assert not executed


# ---------------------------------------------------------------------------
# The cross-tenant guard: a programming error, not an envelope
# ---------------------------------------------------------------------------


async def test_a_tenant_mismatch_raises_rather_than_softening_into_unavailable() -> None:
    """A scope and a runtime that disagree about the tenant is a wiring bug.

    Enveloping it would run a tool against one tenant's data under another tenant's
    configuration and report "unavailable" to the caller. It must crash.
    """
    with pytest.raises(InvariantViolation):
        await _dispatch(
            _registry_with(_ok_handler),
            scope=_Scope(enabled=frozenset({"a_tool"}), organization_id=OrganizationId(new_id())),
        )


async def test_an_agent_version_mismatch_raises() -> None:
    with pytest.raises(InvariantViolation):
        await _dispatch(
            _registry_with(_ok_handler),
            scope=_Scope(enabled=frozenset({"a_tool"}), agent_version_id=AgentVersionId(new_id())),
        )


# ---------------------------------------------------------------------------
# Forged trusted context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forged_key",
    ["organization_id", "Organization_ID", "_call_id", "agent_version_id", "actor_id", "context"],
)
async def test_forged_trusted_context_is_discarded(forged_key: str) -> None:
    """The single most important behaviour in the package.

    `extra="forbid"` would already reject these — stripping them first is what
    preserves the *signal*, so a prompt-injection attempt does not arrive as an
    ordinary validation error with nothing recorded.
    """
    seen: dict[str, object] = {}

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        seen["organization_id"] = rt.organization_id
        return ToolReply(data={})

    envelope = await _dispatch(
        _registry_with(handler),
        arguments_json=json.dumps({"query": "hi", forged_key: str(new_id())}),
    )
    # The call still succeeds — the forged field is simply not there any more.
    assert envelope.outcome is ToolOutcome.OK  # type: ignore[attr-defined]
    assert seen["organization_id"] == _ORG


async def test_a_forged_organization_id_never_reaches_the_arguments() -> None:
    captured: dict[str, object] = {}

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        captured["fields"] = set(args.model_dump())
        return ToolReply(data={})

    await _dispatch(
        _registry_with(handler),
        arguments_json='{"query": "hi", "organization_id": "00000000-0000-0000-0000-000000000000"}',
    )
    assert captured["fields"] == {"query", "count"}


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments_json",
    ["not json at all", "[1, 2, 3]", "null", "7", '{"query": '],
)
async def test_unusable_argument_text_becomes_invalid_arguments(arguments_json: str) -> None:
    envelope = await _dispatch(_registry_with(_ok_handler), arguments_json=arguments_json)
    assert envelope.outcome is ToolOutcome.INVALID_ARGUMENTS  # type: ignore[attr-defined]


async def test_an_empty_argument_string_is_treated_as_no_arguments() -> None:
    """Providers send "" and "{}" inconsistently for an argument-less call."""
    envelope = await _dispatch(_registry_with(_ok_handler), arguments_json="")
    # `query` is required, so this is still a validation failure — but a *field*
    # failure, which means the empty string was parsed rather than rejected outright.
    assert envelope.outcome is ToolOutcome.INVALID_ARGUMENTS  # type: ignore[attr-defined]
    assert any("query" in error for error in envelope.field_errors)  # type: ignore[attr-defined]


async def test_a_numeric_string_is_coerced_rather_than_refused() -> None:
    """A deliberate choice, not an accident of Pydantic's defaults.

    Models routinely send `"5"` for an integer field. On a phone call a rejected
    argument costs a retry round-trip the caller hears as silence, so coercion is the
    kinder behaviour — and safety comes from each field's own bounds, not from
    refusing to coerce. See the note on `ToolArgs`.
    """
    envelope = await _dispatch(
        _registry_with(_ok_handler), arguments_json='{"query": "hi", "count": "5"}'
    )
    assert envelope.outcome is ToolOutcome.OK  # type: ignore[attr-defined]
    assert envelope.data == {"echo": "hi", "count": 5}  # type: ignore[attr-defined]


async def test_a_wrong_type_reports_the_field_but_not_the_value() -> None:
    """Field names are our schema, which the model was already shown. The submitted
    value is model-chosen content and might be something a caller just said, so it
    must not be echoed into the next prompt or into any log of it."""
    envelope = await _dispatch(
        _registry_with(_ok_handler),
        arguments_json='{"query": "hi", "count": "+91 98765 43210"}',
    )
    assert envelope.outcome is ToolOutcome.INVALID_ARGUMENTS  # type: ignore[attr-defined]
    joined = " ".join(envelope.field_errors)  # type: ignore[attr-defined]
    assert "count" in joined
    assert "43210" not in joined


async def test_an_invented_field_is_rejected() -> None:
    envelope = await _dispatch(
        _registry_with(_ok_handler),
        arguments_json='{"query": "hi", "invented": true}',
    )
    assert envelope.outcome is ToolOutcome.INVALID_ARGUMENTS  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotFoundError("gone"), ToolOutcome.NOT_FOUND),
        (ConflictError("taken"), ToolOutcome.CONFLICT),
        (ToolBlocked("opted out"), ToolOutcome.BLOCKED),
        (ValidationError("bad"), ToolOutcome.INVALID_ARGUMENTS),
        (RateLimitError("slow down"), ToolOutcome.RATE_LIMITED),
        (ProviderError("upstream 500"), ToolOutcome.UNAVAILABLE),
        (TransientError("dropped"), ToolOutcome.UNAVAILABLE),
    ],
)
async def test_typed_errors_map_to_outcomes(error: ApplicationError, expected: ToolOutcome) -> None:
    """Order matters in the mapping table: `RateLimitError` is a `ProviderError` and
    `ToolBlocked` is a `ConflictError`, so a reordering silently changes what the
    model is told. This is what would catch it."""

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        raise error

    envelope = await _dispatch(_registry_with(handler))
    assert envelope.outcome is expected  # type: ignore[attr-defined]


async def test_an_unanticipated_exception_becomes_an_envelope() -> None:
    """The backstop. An exception escaping into the session loop would end a call."""

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        raise RuntimeError("something nobody planned for")

    envelope = await _dispatch(_registry_with(handler))
    assert envelope.outcome is ToolOutcome.UNAVAILABLE  # type: ignore[attr-defined]


async def test_an_invariant_violation_is_re_raised_not_swallowed() -> None:
    """A broken domain rule must surface and be fixed, not be softened into
    "I couldn't complete that just now" and disappear."""

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        raise InvariantViolation("a rule that should have been impossible to break")

    with pytest.raises(InvariantViolation):
        await _dispatch(_registry_with(handler))


async def test_a_slow_tool_times_out() -> None:
    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        await asyncio.sleep(5)
        return ToolReply(data={})

    envelope = await _dispatch(_registry_with(handler, timeout_seconds=1))
    assert envelope.outcome is ToolOutcome.TIMEOUT  # type: ignore[attr-defined]
    assert envelope.retryable  # type: ignore[attr-defined]


async def test_no_envelope_leaks_internal_detail() -> None:
    """Sweeps every failure path at once for the things that must never be spoken."""
    forbidden = ("Traceback", "SELECT", "sqlalchemy", "asyncpg", str(_ORG), str(_VERSION))

    async def raiser(args: _Args, rt: ToolRuntime) -> ToolReply:
        raise ProviderError(
            "safe for a user",
            detail={"provider_body": "upstream said 500 for org " + str(_ORG)},
        )

    envelopes = [
        await _dispatch(_registry_with(raiser)),
        await _dispatch(_registry_with(_ok_handler), name="no_such_tool"),
        await _dispatch(_registry_with(_ok_handler), arguments_json="not json"),
    ]
    for envelope in envelopes:
        rendered = envelope.model_dump_json()  # type: ignore[attr-defined]
        for needle in forbidden:
            assert needle not in rendered, needle


# ---------------------------------------------------------------------------
# The built-in tools, through the real pipeline
# ---------------------------------------------------------------------------


async def test_the_builtin_lookup_returns_not_found_for_a_missing_topic() -> None:
    from rn_agent.tools.builtin import register_builtin_tools

    registry = ToolRegistry()
    register_builtin_tools(registry)
    envelope = await dispatch_tool_call(
        registry=registry,
        scope=_Scope(enabled=frozenset({"find_knowledge_base"})),
        runtime=_runtime(catalog=_FakeCatalog(["Pricing"])),
        name="find_knowledge_base",
        arguments_json='{"name": "Nothing Like This"}',
    )
    assert envelope.outcome is ToolOutcome.NOT_FOUND


async def test_the_builtin_list_respects_its_declared_bounds() -> None:
    from rn_agent.tools.builtin import register_builtin_tools

    registry = ToolRegistry()
    register_builtin_tools(registry)
    envelope = await dispatch_tool_call(
        registry=registry,
        scope=_Scope(enabled=frozenset({"list_knowledge_bases"})),
        runtime=_runtime(catalog=_FakeCatalog(["Pricing", "Support"])),
        name="list_knowledge_bases",
        arguments_json='{"limit": 999}',
    )
    assert envelope.outcome is ToolOutcome.INVALID_ARGUMENTS


async def test_a_missing_service_handle_is_a_wiring_bug_not_a_refusal() -> None:
    """Telling the model "unavailable" here would hide a broken deployment behind
    something that looks like an outage."""
    from rn_agent.tools.builtin import register_builtin_tools

    registry = ToolRegistry()
    register_builtin_tools(registry)
    with pytest.raises(InvariantViolation):
        await dispatch_tool_call(
            registry=registry,
            scope=_Scope(enabled=frozenset({"list_knowledge_bases"})),
            runtime=_runtime(catalog=None),
            name="list_knowledge_bases",
            arguments_json="{}",
        )


async def test_cancellation_propagates_rather_than_becoming_an_envelope() -> None:
    """A caller hanging up mid-tool must not be swallowed.

    `asyncio.CancelledError` derives from `BaseException`, so the `except Exception`
    backstop does not catch it — but that is a property of the exception hierarchy, and
    a future `except BaseException` or a bare `except:` would silently break it. Then a
    cancelled call would report "I couldn't complete that just now" and the task would
    keep running.
    """
    started = asyncio.Event()

    async def slow(args: _Args, rt: ToolRuntime) -> ToolReply:
        started.set()
        await asyncio.sleep(30)
        return ToolReply(data={})

    task = asyncio.create_task(_dispatch(_registry_with(slow, timeout_seconds=10)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_envelope_does_not_share_the_handler_s_mapping() -> None:
    """A handler that keeps a reference to what it returned must not be able to edit
    the envelope afterwards — the envelope is what gets recorded and spoken."""
    shared: dict[str, object] = {"value": "original"}

    async def handler(args: _Args, rt: ToolRuntime) -> ToolReply:
        return ToolReply(data=shared)

    envelope = await _dispatch(_registry_with(handler))
    shared["value"] = "mutated after the call returned"
    assert envelope.data == {"value": "original"}  # type: ignore[attr-defined]
