"""Tool contracts: arguments, runtime context, specification, outcome envelope.

The load-bearing decision in this file is a *shape*, not a check:

    async def handler(args: SomeArgs, rt: ToolRuntime) -> ToolReply

`ToolRuntime` is the **second parameter**, not a field of `SomeArgs`. That is why
`organization_id`, `call_id` and `agent_version_id` cannot appear in the generated
JSON schema — not because a filter removes them, but because they were never in
the model whose schema is generated. A filter is something a future contributor
can forget; a parameter that lives outside the schema-bearing type is not.

Tenant identity is not a tool parameter. There is no code path by which a model
token becomes an `organization_id` (AGENT_ARCHITECTURE §4.3).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from rn_core.errors import InvariantViolation
from rn_domain.identifiers import AgentVersionId, CallId, OrganizationId, UserId
from rn_domain.permissions import Permission
from rn_domain.tenancy import TenantContext

# From `rn_services.contracts`, not `rn_services.agents`: the contracts module holds
# protocols and DTOs and imports no persistence, so the agent layer names the
# capability without loading SQLAlchemy. Asserted by
# `tests/unit/test_framework_independence.py`.
from rn_services.contracts import KnowledgeCatalog

__all__ = [
    "INJECTED_CONTEXT_KEYS",
    "AgentToolScope",
    "Effect",
    "ToolArgs",
    "ToolEnvelope",
    "ToolHandler",
    "ToolOutcome",
    "ToolReply",
    "ToolRuntime",
    "ToolServices",
    "ToolSpec",
]


class ToolArgs(BaseModel):
    """Base for every tool's argument model.

    `extra="forbid"` is not tidiness. It is what turns a model inventing a field —
    including a field named `organization_id` — into a validation failure instead
    of a value that quietly travels somewhere.

    `frozen=True` because a validated argument set is a fact about one tool call.
    A handler that mutates its own arguments makes the persisted audit record a
    description of something that did not happen.

    **`strict` is deliberately left off.** Pydantic's lax mode coerces `"5"` to `5`,
    and that is the behaviour we want here: models routinely emit a numeric string
    for an integer field, and on a phone call a rejected argument costs a retry
    round-trip the caller hears as silence. The safety does not come from refusing
    coercion — it comes from every field carrying its own bound (`ge`, `le`,
    `max_length`, an enum), so a coerced value still has to be in range. A tool that
    genuinely cannot tolerate coercion sets `strict=True` on that field and says why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Effect(StrEnum):
    """What a tool does to the world. Drives which gates apply.

    **Phase 2 declares only `READ` tools.** `EXTERNAL` is defined because the
    vocabulary would otherwise be incomplete, but nothing may declare it until the
    idempotency, rate-limit and compliance machinery of Phase 9/10 exists —
    `ToolRegistry` refuses the declaration for exactly that reason.
    """

    READ = "read"
    """Reads tenant data. No side effect. Safe to retry, safe to run concurrently."""

    INTERNAL = "internal"
    """Writes our own data — a note, a lead update. Needs authorization; needs no
    external idempotency key, because Postgres constraints are the dedup."""

    EXTERNAL = "external"
    """Reaches a third party or a human: a message, a booking, a callback. Needs a
    server-derived idempotency key, rate limits and a compliance gate (Phase 9/10)."""


class ToolOutcome(StrEnum):
    """The model-facing result vocabulary (AGENT_ARCHITECTURE §4.5).

    Closed on purpose. Every internal failure maps onto one of these, so the model
    sees a small stable set it can be instructed about, and never a provider's
    error taxonomy.
    """

    OK = "ok"
    DENIED = "denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


class ToolEnvelope(BaseModel):
    """What the model is given back. The only thing the model is ever given back.

    `message` is written for a human ear, because the agent will say it out loud:
    *"I couldn't reach the calendar just now"* is something a caller can respond
    to. It must therefore contain no identifier, no field name, no provider name
    and no exception text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ToolOutcome
    #: Present only on `OK`. Everything here is spoken or reasoned over, so it
    #: carries tenant data and is never logged.
    data: Mapping[str, Any] | None = None
    message: str = ""
    #: Whether the model may usefully try the same call again. Never true for
    #: `DENIED` or `INVALID_ARGUMENTS` retried identically.
    retryable: bool = False
    #: Field-level detail for `INVALID_ARGUMENTS`. Models correct these well, and
    #: it is the one case where naming our own fields back is safe — they are our
    #: schema, which the model was already shown.
    field_errors: tuple[str, ...] = ()

    @property
    def is_ok(self) -> bool:
        return self.outcome is ToolOutcome.OK


@dataclass(frozen=True, slots=True)
class ToolReply:
    """What a handler returns on success.

    A handler signals failure by **raising** a typed `rn_core` error, which the
    dispatcher maps to an outcome. That keeps handlers free of envelope
    construction, and keeps the error→outcome mapping in one tested place rather
    than repeated per tool with small differences.
    """

    data: Mapping[str, Any]
    message: str = ""


@dataclass(frozen=True, slots=True)
class ToolServices:
    """Business-service handles a tool may use.

    Handles are protocols from `rn_services`, never repositories: `rn_agent` may
    not import `rn_persistence` at all, and an import-linter contract enforces it.
    Every handle is optional so a caller can wire only what its tools need — a
    unit test for the guardrails should not have to construct a knowledge catalog.

    Absence is a **wiring bug**, not a model-facing failure, which is why
    `require_knowledge()` raises rather than returning an envelope: the model did
    nothing wrong and telling it "unavailable" would hide a broken deployment.
    """

    knowledge: KnowledgeCatalog | None = None

    def require_knowledge(self) -> KnowledgeCatalog:
        if self.knowledge is None:
            raise InvariantViolation(
                "This tool needs a knowledge catalog and none was wired into ToolServices."
            )
        return self.knowledge


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """Server-side execution context. **Never populated from model output.**

    `context` is the single source of tenant authority — a `TenantContext` derived
    from a verified identity by `rn_services.build_tenant_context`. There is
    deliberately no separate `organization_id` field: two fields could disagree,
    and a `ToolRuntime` whose tenant differs from its authority is exactly the bug
    this type exists to make unrepresentable.
    """

    context: TenantContext
    agent_version_id: AgentVersionId
    #: `None` outside a call — an evaluation run, a dashboard preview.
    call_id: CallId | None = None
    #: BCP-47-ish tag the caller is currently being served in. Advisory: it shapes
    #: tool output language, never authorization.
    locale: str = "en"
    services: ToolServices = field(default_factory=ToolServices)

    @property
    def organization_id(self) -> OrganizationId:
        return OrganizationId(self.context.organization_id)

    @property
    def actor_id(self) -> UserId | None:
        return self.context.actor_id


#: Argument names that are supplied by the server and must never be accepted from
#: model output. Checked by the dispatcher, and forbidden as tool argument names
#: at registration so a future tool cannot quietly claim one.
#:
#: Wider than the three identifiers that strictly matter. Over-refusing costs a
#: tool author one rename at import time; under-refusing costs a security event
#: nobody notices, because `extra="forbid"` would have reported the forged key as
#: an ordinary validation error and the *signal* would be gone.
INJECTED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "organization_id",
        "organisation_id",
        "org_id",
        "tenant_id",
        "call_id",
        "agent_version_id",
        "agent_id",
        "actor_id",
        "user_id",
        "permissions",
        "context",
        "runtime",
        "services",
    }
)


#: A tool implementation. Async because every real tool does I/O through
#: `rn_services`; a sync tool would block the event loop that is also pumping
#: audio in a live call.
type ToolHandler = Callable[[Any, ToolRuntime], Awaitable[ToolReply]]


class AgentToolScope(Protocol):
    """What the dispatcher needs to know about the agent version it is serving.

    A protocol rather than `AgentSnapshot` itself, for two reasons.

    **Direction.** The snapshot is *composed* configuration and sits above the tool
    mechanism; `rn_agent.snapshot` imports this package, so this package must not
    import it back. A narrow protocol keeps the dependency pointing one way instead
    of introducing a cycle that a deferred import would only hide.

    **Honesty.** The dispatcher needs exactly three facts. Declaring them makes the
    requirement legible and makes it testable with a three-line stub, rather than
    obliging every dispatch test to build a whole snapshot.

    Declared as read-only properties on purpose: nothing here may be assigned
    through the protocol.
    """

    @property
    def organization_id(self) -> OrganizationId:
        """The owning tenant. Checked against `ToolRuntime` before anything runs."""
        ...

    @property
    def agent_version_id(self) -> AgentVersionId:
        """The pinned version. Checked against `ToolRuntime` for the same reason."""
        ...

    @property
    def enabled_tools(self) -> frozenset[str]:
        """Tool names this agent version may call."""
        ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool, fully declared. Immutable once registered."""

    name: str
    description: str
    args: type[ToolArgs]
    effect: Effect
    permission: Permission
    timeout: timedelta
    handler: ToolHandler
    #: The flat provider spec as canonical JSON text, built once at registration.
    #:
    #: Prebuilt because a live session must not generate JSON Schema, and because a
    #: schema that cannot be built should fail at import rather than at
    #: `session.update`.
    #:
    #: **Text, not a `dict`.** One registry is read by every concurrent session, so a
    #: shared `dict` handed to callers is shared mutable state: one caller editing the
    #: spec it was given would change what every other live call is told about that
    #: tool. Text is immutable, and `realtime_spec` below parses a fresh copy per
    #: caller — so a caller that does mutate its copy harms nobody.
    realtime_spec_json: str

    @property
    def realtime_spec(self) -> dict[str, Any]:
        """A fresh, mutable copy of the flat provider spec.

        Parsed on each access, deliberately. The cost is paid at session open, not per
        turn, and it buys the property above: no two callers ever hold the same object.
        """
        parsed: dict[str, Any] = json.loads(self.realtime_spec_json)
        return parsed

    @property
    def timeout_seconds(self) -> float:
        return self.timeout.total_seconds()
