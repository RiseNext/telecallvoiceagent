"""Agent use cases: loading a published version's configuration, and the knowledge
catalog a tool may read.

The types this module returns live in `rn_services.contracts`, which imports no
persistence. That split is deliberate and tested: it lets `rn_agent` name a capability
without importing this module, so the agent layer does not load SQLAlchemy just to
compose a prompt. See the note at the top of `contracts.py`.

Everything here is tenant-scoped through a server-derived `TenantContext`. The
repositories cannot be constructed without one and have no `organization_id` parameter
on any read, so there is nothing to forget.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from rn_core.errors import NotFoundError
from rn_domain.identifiers import KnowledgeBaseId
from rn_persistence.models import KnowledgeBaseModel
from rn_persistence.repositories import (
    AgentToolConfigRepository,
    AgentVersionKnowledgeBaseRepository,
    AgentVersionRepository,
    KnowledgeBaseRepository,
)
from rn_services.contracts import (
    AgentConfigurationSource,
    KnowledgeBaseSummary,
    KnowledgeCatalog,
    PublishedAgentConfiguration,
)

__all__ = [
    "AgentConfigurationService",
    "AgentConfigurationSource",
    "KnowledgeBaseSummary",
    "KnowledgeCatalog",
    "KnowledgeCatalogService",
    "PublishedAgentConfiguration",
    "load_published_agent_configuration",
]


async def load_published_agent_configuration(
    *,
    agent_version_id: uuid.UUID,
    versions: AgentVersionRepository,
    tool_configs: AgentToolConfigRepository,
    knowledge_bindings: AgentVersionKnowledgeBaseRepository,
) -> PublishedAgentConfiguration:
    """Read one agent version and everything bound to it, within one tenant.

    Three queries — the version, its tool configuration, its knowledge bindings —
    each bounded and each independent of how much data the tenant has. That is a
    constant, not an N+1: an N+1 here would be one query *per tool*, which is what
    lazy relationship loading would have produced.

    Raises:
        NotFoundError: if the version does not exist **or belongs to another
            tenant**. The two are deliberately indistinguishable; the repository
            raises it, and this function does not soften it.
    """
    row = await versions.get(agent_version_id)
    version = row.to_domain()

    configs = await tool_configs.list_for_version(agent_version_id)
    bindings = await knowledge_bindings.list_for_version(agent_version_id)

    return PublishedAgentConfiguration(
        version=version,
        # Sorted so the same stored configuration always produces the same order,
        # which the snapshot content hash downstream depends on.
        enabled_tool_names=tuple(sorted(c.tool_name for c in configs if c.enabled)),
        knowledge_base_ids=tuple(
            sorted((KnowledgeBaseId(b.knowledge_base_id) for b in bindings), key=str)
        ),
        organization_instructions=None,
    )


class AgentConfigurationService:
    """`AgentConfigurationSource` over the database, scoped to one tenant.

    Holds the three repositories the read needs, each already bound to a
    server-derived `TenantContext`. Constructed by whichever composition root owns
    the session — the API, a worker, a test.
    """

    __slots__ = ("_knowledge_bindings", "_tool_configs", "_versions")

    def __init__(
        self,
        *,
        versions: AgentVersionRepository,
        tool_configs: AgentToolConfigRepository,
        knowledge_bindings: AgentVersionKnowledgeBaseRepository,
    ) -> None:
        self._versions = versions
        self._tool_configs = tool_configs
        self._knowledge_bindings = knowledge_bindings

    async def load_published(self, agent_version_id: uuid.UUID) -> PublishedAgentConfiguration:
        return await load_published_agent_configuration(
            agent_version_id=agent_version_id,
            versions=self._versions,
            tool_configs=self._tool_configs,
            knowledge_bindings=self._knowledge_bindings,
        )


class KnowledgeCatalogService:
    """`KnowledgeCatalog` over the database, scoped to one tenant."""

    __slots__ = ("_repository",)

    def __init__(self, knowledge_bases: KnowledgeBaseRepository) -> None:
        self._repository = knowledge_bases

    async def list_knowledge_bases(self, *, limit: int) -> Sequence[KnowledgeBaseSummary]:
        page = await self._repository.list(limit=limit)
        return [_summarise(row) for row in page.items if row.deleted_at is None]

    async def find_knowledge_base(self, *, name: str) -> KnowledgeBaseSummary:
        row = await self._repository.find_by_name(name)
        if row is None or row.deleted_at is not None:
            # Same error whether it does not exist or belongs to someone else. A
            # distinguishable "exists but not yours" would let a caller on the
            # phone enumerate another tenant's knowledge bases by name.
            raise NotFoundError(
                "Knowledge base not found.",
                detail={"name": name, "organization_id": str(self._repository.organization_id)},
            )
        return _summarise(row)


def _summarise(row: KnowledgeBaseModel) -> KnowledgeBaseSummary:
    entity = row.to_domain()
    return KnowledgeBaseSummary(
        id=entity.id,
        name=entity.name,
        description=entity.description,
    )
