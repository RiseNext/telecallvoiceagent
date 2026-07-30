"""The types layers above `rn_services` depend on. **No persistence imports here.**

Protocols and data-transfer objects only, so that a caller can name a capability
without loading the machinery behind it.

That is not tidiness — it is a measured property with a test. `rn_agent` depends on
`rn_services`, and `rn_services` depends on `rn_persistence`. If the agent layer
imported a module that pulls in a repository, then importing `rn_agent` would load
SQLAlchemy and the whole ORM — in the voice gateway, for a package whose job is to
compose a prompt and validate tool arguments. `rn_core.telemetry` defers its SDK
import for the same reason; this is the same discipline one layer up.

The import-linter contract permits `rn_services` to import `rn_persistence`, so this
split hides nothing. It is about *when* the cost is paid, not about whether the
dependency is allowed.

`tests/unit/test_framework_independence.py` asserts that importing this module, and
importing `rn_agent`, loads no database driver.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rn_domain.entities.agents import AgentVersion
from rn_domain.identifiers import KnowledgeBaseId

__all__ = [
    "AgentConfigurationSource",
    "KnowledgeBaseSummary",
    "KnowledgeCatalog",
    "PublishedAgentConfiguration",
]


@dataclass(frozen=True, slots=True)
class PublishedAgentConfiguration:
    """Everything stored about one agent version, as domain data.

    The handover type between the layer that reads the database and the layer that
    builds the runtime object. Deliberately not an ORM model and deliberately not an
    `AgentSnapshot` — the snapshot lives in `rn_agent`, which is *above* this layer,
    so returning one from here would be an upward import.

    Frozen, so nothing downstream can edit what was read.
    """

    version: AgentVersion
    #: Tool names with `enabled = true`. Disabled rows are dropped by the loader
    #: rather than passed along with a flag: "enabled but disabled" is not a state
    #: anything above needs to reason about.
    enabled_tool_names: tuple[str, ...]
    knowledge_base_ids: tuple[KnowledgeBaseId, ...]
    #: Instruction layer 2. Always `None` today — no column stores organization-level
    #: instructions yet; they arrive with organization settings. Present so this type
    #: does not change shape when they do.
    organization_instructions: str | None = None


class AgentConfigurationSource(Protocol):
    """Where an agent version's stored configuration comes from.

    A protocol so `rn_agent` can resolve a snapshot without naming a repository.
    `rn_agent` is forbidden from importing `rn_persistence` — an import-linter
    contract enforces it — and that is what keeps the agent layer testable with no
    database at all.
    """

    async def load_published(self, agent_version_id: uuid.UUID) -> PublishedAgentConfiguration:
        """Load one published version's configuration, within the caller's tenant."""
        ...


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSummary:
    """One knowledge base, as a tool may see it.

    A separate type from `KnowledgeBaseModel` on purpose: an ORM model is a
    persistence structure, and handing one to a tool would put lazy loading and a live
    session inside something whose result is read out loud on a phone call.
    """

    id: KnowledgeBaseId
    name: str
    description: str | None


class KnowledgeCatalog(Protocol):
    """What a tool may ask about a tenant's knowledge bases.

    Metadata only. Document content and retrieval are Phase 3, blocked on open
    decision **D-8** (ADR-010) — there is no `documents` table, no `document_chunks`
    table and no vector column anywhere in this schema.
    """

    async def list_knowledge_bases(self, *, limit: int) -> Sequence[KnowledgeBaseSummary]:
        """This tenant's knowledge bases, bounded by `limit`."""
        ...

    async def find_knowledge_base(self, *, name: str) -> KnowledgeBaseSummary:
        """One knowledge base by exact name.

        Raises `NotFoundError` when there is none — including when another tenant has
        one by that name, because a distinguishable answer would let a caller on the
        phone enumerate another tenant's configuration.
        """
        ...
