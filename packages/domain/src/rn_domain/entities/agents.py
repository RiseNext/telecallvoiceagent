"""Agent configuration entities.

The distinction the whole runtime rests on (ARCHITECTURE §4.4):

* **`Agent`** — the definition. Mutable metadata only: name, status, which
  version is current.
* **`AgentVersion`** — everything that changes behaviour. **Immutable once
  published.** A call pins one of these forever.
* **Session** — no entity here. A session is in-process state in the voice
  gateway that dies with the call; its durable projection is the `Call` row.

Tool enablement and knowledge-base bindings belong to the *version*, not the
agent. If they lived on the agent, "which configuration handled this call" would
be answerable for the prompt but not for what the agent could do to the world —
which is worse than not answerable at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rn_core.clock import now_utc
from rn_core.errors import ConflictError, InvariantViolation
from rn_domain.enums import AgentStatus, AgentVersionStatus
from rn_domain.identifiers import (
    AgentId,
    AgentVersionId,
    KnowledgeBaseId,
    OrganizationId,
    UserId,
)
from rn_domain.values import LanguagePolicy, LanguageTag

__all__ = ["Agent", "AgentToolConfig", "AgentVersion", "KnowledgeBase"]

_MIN_INSTRUCTIONS = 20


@dataclass(slots=True)
class Agent:
    """An agent definition. Configuration, not a running process."""

    id: AgentId
    organization_id: OrganizationId
    name: str
    description: str | None = None
    status: AgentStatus = AgentStatus.DRAFT
    #: Set after the first version exists. `agents` and `agent_versions` form a
    #: cycle; it is resolved with a nullable column written in the same
    #: transaction rather than a DEFERRABLE constraint, whose failures surface at
    #: COMMIT with unhelpful diagnostics.
    current_version_id: AgentVersionId | None = None
    created_at: datetime = field(default_factory=now_utc)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvariantViolation("Agent name must not be blank.")

    @property
    def is_dialable(self) -> bool:
        """Whether this agent can serve a call right now."""
        return (
            self.status is AgentStatus.ACTIVE
            and self.current_version_id is not None
            and self.deleted_at is None
        )


@dataclass(slots=True)
class AgentVersion:
    """An immutable behavioural snapshot.

    Everything that changes how the agent behaves lives here so that a call can
    pin exactly one row and the question "what was this agent doing in March"
    stays answerable forever.

    `turn_policy` and `voice_map` are JSONB in storage: their shape is genuinely
    open (VAD parameters differ per provider; the voice map is
    `language -> (provider, voice_id)`), and nothing filters or sorts on them.
    """

    id: AgentVersionId
    organization_id: OrganizationId
    agent_id: AgentId
    version_number: int
    instructions: str
    #: **The single source of truth for this version's languages.** Stored as
    #: JSONB; `agent_versions.languages` is a projection of `allowed`, enforced by
    #: a database CHECK. See `LanguagePolicy` and the `languages` property below.
    language_policy: LanguagePolicy
    status: AgentVersionStatus = AgentVersionStatus.DRAFT
    voice_map: dict[str, Any] = field(default_factory=dict)
    turn_policy: dict[str, Any] = field(default_factory=dict)
    guardrail_config: dict[str, Any] = field(default_factory=dict)
    realtime_model: str | None = None
    #: Telephony sample rate for calls served by this version. Per-agent because
    #: it is chosen per call at dial time and trades resampling cost against
    #: chunk latency (ADR-003). Nullable until the realtime phase sets it.
    telephony_sample_rate: int | None = None
    published_at: datetime | None = None
    created_by_user_id: UserId | None = None
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if self.version_number < 1:
            raise InvariantViolation("Agent version numbers start at 1.")
        if len(self.instructions.strip()) < _MIN_INSTRUCTIONS:
            raise InvariantViolation(
                "Agent instructions are too short to define behaviour.",
                detail={"length": len(self.instructions.strip())},
            )
        # "at least one language" is a `LanguagePolicy` invariant, checked at its
        # own construction. Re-checking it here would be a second place to keep
        # right, and the policy cannot exist in a violating state anyway.
        if self.status is AgentVersionStatus.PUBLISHED and self.published_at is None:
            raise InvariantViolation("A published version must carry published_at.")

    @property
    def languages(self) -> tuple[LanguageTag, ...]:
        """The languages this version allows.

        A **read-only projection** of `language_policy.allowed`, not a field. That
        is what makes dual source of truth impossible in the domain: there is
        nothing to set, so nothing can disagree with the policy. The storage layer
        writes the same projection into the `languages` array column, which a
        database CHECK ties back to the JSONB policy.
        """
        return self.language_policy.allowed

    @property
    def is_published(self) -> bool:
        return self.status is AgentVersionStatus.PUBLISHED and self.published_at is not None

    @property
    def is_frozen(self) -> bool:
        """Whether behaviour columns may still change.

        Also enforced by a database trigger — this property is the readable
        statement of the rule, the trigger is what makes it true regardless of
        which code path attempts the write.
        """
        return self.published_at is not None

    def publish(self, *, at: datetime | None = None) -> None:
        """Publish this version, freezing its behaviour.

        Raises `ConflictError` rather than `InvariantViolation`: republishing is
        a legitimate thing for a user to attempt twice (a double-clicked button),
        not a broken invariant.
        """
        if self.status is AgentVersionStatus.ARCHIVED:
            raise ConflictError("An archived agent version cannot be published.")
        if self.is_published:
            raise ConflictError("This agent version is already published.")
        self.published_at = at or now_utc()
        self.status = AgentVersionStatus.PUBLISHED

    def archive(self) -> None:
        """Archive. Never deleted — a version that served a call must persist."""
        self.status = AgentVersionStatus.ARCHIVED


@dataclass(slots=True)
class AgentToolConfig:
    """One tool enabled on one agent version, with its per-agent configuration.

    Enablement is per *version* deliberately: turning on a tool that can message
    a customer changes what the agent can do to the world, and that must be
    pinned to the call the same way the prompt is.
    """

    organization_id: OrganizationId
    agent_version_id: AgentVersionId
    tool_name: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise InvariantViolation("Tool name must not be blank.")


@dataclass(slots=True)
class KnowledgeBase:
    """A knowledge base. **Metadata only in Phase 1.**

    Documents and chunks arrive in Phase 3, once open decision **D-8** settles
    the embedding model, width, column type and index strategy (ADR-010). The
    binding between a version and a knowledge base exists now because it is part
    of an agent's behaviour and therefore part of the version.
    """

    id: KnowledgeBaseId
    organization_id: OrganizationId
    name: str
    description: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvariantViolation("Knowledge base name must not be blank.")
