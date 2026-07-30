"""RiseNext application layer — use cases and business services shared by API, voice gateway and workers.

Phase 1 delivered the **tenancy and authorization seam**. Phase 2 adds the agent
use cases the agent core needs: reading a published version's configuration, and the
knowledge catalog a tool may query. The remaining business use cases (campaign import
and dispatch, knowledge ingestion, call lifecycle, exports) arrive with the phases
that need them.

Framework-free by contract: no FastAPI, no LangChain, no broker client. This
package is called from an HTTP handler, from a WebSocket session and from a job,
so it must not assume any of them.

Note the direction of the agent seam. `rn_services` returns **domain data** —
`PublishedAgentConfiguration`, `KnowledgeBaseSummary` — and never an `AgentSnapshot`,
which lives a layer above. Capabilities a tool needs are exposed as protocols so
`rn_agent` depends on the capability rather than on a repository.

Those protocols and DTOs live in `rn_services.contracts`, which imports no
persistence, and the layers above import them **from there** rather than from this
package root. Importing `rn_services` pulls in the ORM; importing
`rn_services.contracts` does not, and `rn_agent` must not (see the note in
`contracts.py`).
"""

from rn_services.agents import (
    AgentConfigurationService,
    KnowledgeCatalogService,
    load_published_agent_configuration,
)
from rn_services.authorization import (
    Principal,
    build_platform_context,
    build_tenant_context,
)
from rn_services.contracts import (
    AgentConfigurationSource,
    KnowledgeBaseSummary,
    KnowledgeCatalog,
    PublishedAgentConfiguration,
)
from rn_services.policies import (
    ensure_can_export_calls,
    ensure_can_publish_agent,
    ensure_can_start_campaign,
    ensure_organization_active,
    ensure_same_tenant,
)

__version__ = "0.1.0"

__all__ = [
    "AgentConfigurationService",
    "AgentConfigurationSource",
    "KnowledgeBaseSummary",
    "KnowledgeCatalog",
    "KnowledgeCatalogService",
    "Principal",
    "PublishedAgentConfiguration",
    "build_platform_context",
    "build_tenant_context",
    "ensure_can_export_calls",
    "ensure_can_publish_agent",
    "ensure_can_start_campaign",
    "ensure_organization_active",
    "ensure_same_tenant",
    "load_published_agent_configuration",
]
