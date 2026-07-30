"""Domain entities, grouped by aggregate.

Every entity here is pure: no I/O, no ORM, no framework. They validate their own
invariants at construction and expose explicit state transitions rather than
public mutable status fields — an entity that can be moved into an impossible
state by assignment is not protecting anything.
"""

from rn_domain.entities.agents import Agent, AgentToolConfig, AgentVersion, KnowledgeBase
from rn_domain.entities.calls import Call, CallEvent, CallToolExecution
from rn_domain.entities.campaigns import Campaign, CampaignContact
from rn_domain.entities.ops import AuditLogEntry, DeadLetterJob, OutboxEvent
from rn_domain.entities.people import ConsentRecord, Contact, Lead, Suppression
from rn_domain.entities.tenancy import Organization, OrganizationMember, Role, User

__all__ = [
    "Agent",
    "AgentToolConfig",
    "AgentVersion",
    "AuditLogEntry",
    "Call",
    "CallEvent",
    "CallToolExecution",
    "Campaign",
    "CampaignContact",
    "ConsentRecord",
    "Contact",
    "DeadLetterJob",
    "KnowledgeBase",
    "Lead",
    "Organization",
    "OrganizationMember",
    "OutboxEvent",
    "Role",
    "Suppression",
    "User",
]
