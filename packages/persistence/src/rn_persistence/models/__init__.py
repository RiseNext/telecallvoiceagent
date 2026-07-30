"""ORM models — the 21 tables of the Phase 1 schema.

Importing this package registers every model on `Base.metadata`, which is what
Alembic's autogenerate compares against. A model that is not imported here is
invisible to migrations, so new modules must be added to this list.

**There is no vector column and no `document_chunks` table.** Knowledge is
metadata only until open decision **D-8** settles the embedding model, width,
column type and index strategy in Phase 3 (ADR-010).
"""

from rn_persistence.models.catalog import (
    AgentModel,
    AgentToolConfigModel,
    AgentVersionKnowledgeBaseModel,
    AgentVersionModel,
    KnowledgeBaseModel,
)
from rn_persistence.models.engagement import (
    CallEventModel,
    CallModel,
    CallToolExecutionModel,
    CampaignContactModel,
    CampaignModel,
)
from rn_persistence.models.ops import AuditLogModel, DeadLetterJobModel, OutboxEventModel
from rn_persistence.models.people import (
    ConsentRecordModel,
    ContactModel,
    LeadModel,
    SuppressionModel,
)
from rn_persistence.models.tenancy import (
    OrganizationMemberModel,
    OrganizationModel,
    RoleModel,
    UserModel,
)

#: Tables that carry a tenant key and must therefore only ever be reached
#: through a tenant-scoped repository. Asserted by a test, so adding a
#: tenant-owned table without updating this list fails CI.
TENANT_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "organization_members",
        "agents",
        "agent_versions",
        "agent_tool_configs",
        "agent_version_knowledge_bases",
        "knowledge_bases",
        "contacts",
        "leads",
        "consent_records",
        "campaigns",
        "campaign_contacts",
        "calls",
        "call_events",
        "call_tool_executions",
    }
)

#: Tables with a *nullable* tenant key, where NULL has an explicit meaning:
#: platform-wide suppression, platform-level outbox event, platform audit entry,
#: a job that failed before its tenant was known.
NULLABLE_TENANT_TABLES: frozenset[str] = frozenset(
    {"suppressions", "outbox", "audit_logs", "dead_letter_jobs", "roles"}
)

#: Tables with no tenant key at all.
PLATFORM_TABLES: frozenset[str] = frozenset({"organizations", "users"})

__all__ = [
    "NULLABLE_TENANT_TABLES",
    "PLATFORM_TABLES",
    "TENANT_OWNED_TABLES",
    "AgentModel",
    "AgentToolConfigModel",
    "AgentVersionKnowledgeBaseModel",
    "AgentVersionModel",
    "AuditLogModel",
    "CallEventModel",
    "CallModel",
    "CallToolExecutionModel",
    "CampaignContactModel",
    "CampaignModel",
    "ConsentRecordModel",
    "ContactModel",
    "DeadLetterJobModel",
    "KnowledgeBaseModel",
    "LeadModel",
    "OrganizationMemberModel",
    "OrganizationModel",
    "OutboxEventModel",
    "RoleModel",
    "SuppressionModel",
    "UserModel",
]
