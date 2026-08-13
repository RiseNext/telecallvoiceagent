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
    "KnowledgeRetriever",
    "PublishedAgentConfiguration",
    "RetrievalResult",
    "RetrievedChunk",
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


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One retrieved unit of knowledge, as the layer above may see it.

    `score` is a similarity — **higher is better** — and is deliberately not called a
    distance. The two are inverses, they are trivially confused, and a ranker that
    sorts the wrong way round produces plausible-looking output rather than an error.

    `chunk_id` is opaque and exists for logging and de-duplication by the caller. It is
    never given to a model: an identifier in a model's context is one more thing that
    can be spoken out loud or echoed into a later tool call.
    """

    chunk_id: str
    knowledge_base_id: KnowledgeBaseId
    knowledge_base_name: str
    content: str
    score: float
    #: What embedded this chunk. Carried per chunk, not per result, because during a
    #: rolling re-embed one tenant's index can legitimately hold two models at once.
    embedding_model: str
    #: `IngestionFlag` values raised when this chunk was indexed, as plain strings so
    #: nothing above has to import the domain enum to read them. Instruction-shaped
    #: chunks never reach here — they are withheld at index time — so in practice this
    #: carries the price-shaped flag, which a caller may want to act on.
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """The outcome of one retrieval, with enough context to know it was healthy.

    `requested_k` is carried so `underfilled` is answerable from the result alone.
    Under-return is the **only** observable symptom of HC-25 — a filtered approximate
    scan post-filters and quietly returns fewer rows than asked for — and it presents
    as "the agent forgot our knowledge base" rather than as an error. A result type
    that dropped the request size would make the one detectable symptom undetectable.
    """

    chunks: tuple[RetrievedChunk, ...]
    requested_k: int
    embedding_model: str

    @property
    def underfilled(self) -> bool:
        """Whether fewer chunks came back than were asked for."""
        return len(self.chunks) < self.requested_k


class KnowledgeRetriever(Protocol):
    """Tenant-scoped semantic retrieval over a tenant's indexed knowledge.

    **There is no `organization_id` parameter, and that is the point.** An
    implementation is constructed with a server-derived `TenantContext` and filters by
    it internally, exactly as `KnowledgeCatalog`'s implementation does — so there is no
    argument a caller can pass wrongly and no value a model could influence.

    Separate from `KnowledgeCatalog` rather than added to it: the catalog answers
    "which topics exist" from metadata, this answers "what do we say about X" from
    content, and the two have different costs, different failure modes and different
    backing stores. One protocol with both would oblige every caller that wants a topic
    list to be wired with an embedding provider.
    """

    async def search(
        self,
        *,
        query: str,
        knowledge_base_ids: Sequence[KnowledgeBaseId] | None = None,
        k: int,
    ) -> RetrievalResult:
        """Retrieve up to `k` chunks for `query`, within the caller's tenant.

        Args:
            query: Caller-derived text. Untrusted — it is embedded and compared, never
                interpreted.
            knowledge_base_ids: Restrict to these knowledge bases. `None` means every
                knowledge base this tenant has indexed. Present in the protocol from the
                start because binding retrieval to an agent version's knowledge bases is
                a scoping decision, and adding the parameter later would mean every
                existing implementation silently searched wider than its caller expected.
            k: How many chunks are wanted. An implementation may clamp it to a
                configured ceiling; the returned `requested_k` reports what was actually
                asked of the index.

        Returns:
            A `RetrievalResult`, possibly empty. An empty result is a normal outcome for
            a query nothing matches, not an error.
        """
        ...
