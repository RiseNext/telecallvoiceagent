"""RiseNext application layer — use cases and business services shared by API, voice gateway and workers.

Phase 1 delivers the **tenancy and authorization seam** only. Business use cases
(campaign import and dispatch, knowledge ingestion, call lifecycle, exports)
arrive with the phases that need them.

Framework-free by contract: no FastAPI, no LangChain, no broker client. This
package is called from an HTTP handler, from a WebSocket session and from a job,
so it must not assume any of them.
"""

from rn_services.authorization import (
    Principal,
    build_platform_context,
    build_tenant_context,
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
    "Principal",
    "build_platform_context",
    "build_tenant_context",
    "ensure_can_export_calls",
    "ensure_can_publish_agent",
    "ensure_can_start_campaign",
    "ensure_organization_active",
    "ensure_same_tenant",
]
