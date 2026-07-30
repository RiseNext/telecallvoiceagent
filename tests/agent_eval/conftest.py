"""Tier-1 evaluation fixtures: a published agent, a tool registry, no network.

Tier 1 is the **deterministic, offline** tier. Scripted caller utterances, a scripted
provider, in-memory tool services, and hard-fail assertions on the compliance
dimensions. It runs in CI on every change, so it has to be fast and it must never
touch a paid API — `pytest`'s `addopts` already excludes `live`, and there is nothing
here that could reach a network even if it did not.

What Tier 1 is not: a judge. Rubric-scored dimensions (objective completion, language
correctness, hallucination) need a model as a judge and belong to the evaluation runner
in `rn_orchestration` (Phase 11, AGENT_ARCHITECTURE §11). Claiming a quality score from
a scripted tape would be claiming something not measured.

The compliance dimensions *are* checkable deterministically, and per
AGENT_ARCHITECTURE §11.1 they are **gates, not scores**: a version that fails one
cannot be published, whatever its average.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rn_agent.snapshot import AgentSnapshot, build_snapshot
from rn_agent.tools import REGISTRY
from rn_agent.tools.base import ToolRuntime, ToolServices
from rn_core.clock import now_utc
from rn_core.errors import NotFoundError
from rn_core.ids import new_id
from rn_domain.entities.agents import AgentVersion
from rn_domain.enums import AgentVersionStatus
from rn_domain.identifiers import AgentId, AgentVersionId, CallId, OrganizationId, UserId
from rn_domain.tenancy import TenantContext
from rn_domain.values import LanguagePolicy, LanguageTag
from rn_services.contracts import KnowledgeBaseSummary

#: The evaluation tenant. Synthetic, and there is no way for anything in this suite to
#: reach a real one: no database session exists here at all.
EVAL_ORGANIZATION_ID = OrganizationId(new_id())
EVAL_AGENT_VERSION_ID = AgentVersionId(new_id())

#: Seeded knowledge-base names. Metadata only — Phase 2 retrieves no content, because
#: there is no `documents` table and no vector column (open decision D-8, ADR-010).
SEEDED_TOPICS = ("Pricing", "Website Development", "Support Hours")

#: The agent brief under evaluation. A scenario's expectations are only meaningful
#: against a specific brief, so it lives here rather than being inlined per test.
EVAL_AGENT_INSTRUCTIONS = """\
You are Aira, a sales assistant for a software services company in India.
Greet the caller, disclose that you are an AI in your first turn, and find out what
they need. Use your tools to check which topics you can help with before claiming you
can help with something. Keep every turn to one or two sentences.
"""


class StubKnowledgeCatalog:
    """An in-memory `KnowledgeCatalog`. No database, no network."""

    def __init__(self, topics: Sequence[str] = SEEDED_TOPICS) -> None:
        self._topics = list(topics)

    async def list_knowledge_bases(self, *, limit: int) -> Sequence[KnowledgeBaseSummary]:
        return [
            KnowledgeBaseSummary(id=new_id(), name=name, description=None)  # type: ignore[arg-type]
            for name in self._topics[:limit]
        ]

    async def find_knowledge_base(self, *, name: str) -> KnowledgeBaseSummary:
        if name not in self._topics:
            raise NotFoundError("Knowledge base not found.", detail={"name": name})
        return KnowledgeBaseSummary(id=new_id(), name=name, description=None)  # type: ignore[arg-type]


@pytest.fixture
def eval_snapshot() -> AgentSnapshot:
    """The agent version under evaluation: trilingual, both built-in tools enabled."""
    version = AgentVersion(
        id=EVAL_AGENT_VERSION_ID,
        organization_id=EVAL_ORGANIZATION_ID,
        agent_id=AgentId(new_id()),
        version_number=1,
        instructions=EVAL_AGENT_INSTRUCTIONS,
        language_policy=LanguagePolicy(
            primary=LanguageTag("en"),
            allowed=(LanguageTag("en"), LanguageTag("hi-IN"), LanguageTag("te-IN")),
        ),
        status=AgentVersionStatus.PUBLISHED,
        published_at=now_utc(),
    )
    return build_snapshot(
        version=version,
        enabled_tool_names=["list_knowledge_bases", "find_knowledge_base"],
        knowledge_base_ids=(),
        registry=REGISTRY,
    )


@pytest.fixture
def eval_runtime() -> ToolRuntime:
    """Server-side tool context for the evaluation tenant."""
    return ToolRuntime(
        context=TenantContext(
            organization_id=EVAL_ORGANIZATION_ID,
            actor_id=UserId(new_id()),
            permissions=frozenset({"org:knowledge:read"}),
        ),
        agent_version_id=EVAL_AGENT_VERSION_ID,
        call_id=CallId(new_id()),
        services=ToolServices(knowledge=StubKnowledgeCatalog()),
    )
