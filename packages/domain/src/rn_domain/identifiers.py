"""Typed identifiers.

Every id is a `UUID` at runtime and a *distinct type* to the type checker. This
costs nothing and buys the one class of bug that is most expensive here: passing
a `CallId` where an `OrganizationId` was expected. In a multi-tenant system that
is not a crash, it is a cross-tenant read that returns plausible data.

`NewType` rather than a wrapper class on purpose — zero runtime overhead, and
the values stay ordinary UUIDs for SQLAlchemy, JSON and logging.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

__all__ = [
    "AgentId",
    "AgentVersionId",
    "AuditLogId",
    "CallEventId",
    "CallId",
    "CampaignContactId",
    "CampaignId",
    "ConsentRecordId",
    "ContactId",
    "DeadLetterJobId",
    "KnowledgeBaseId",
    "LeadId",
    "OrganizationId",
    "OutboxEventId",
    "RoleId",
    "SuppressionId",
    "ToolExecutionId",
    "UserId",
]

OrganizationId = NewType("OrganizationId", UUID)
UserId = NewType("UserId", UUID)
RoleId = NewType("RoleId", UUID)

AgentId = NewType("AgentId", UUID)
AgentVersionId = NewType("AgentVersionId", UUID)
KnowledgeBaseId = NewType("KnowledgeBaseId", UUID)

ContactId = NewType("ContactId", UUID)
LeadId = NewType("LeadId", UUID)
ConsentRecordId = NewType("ConsentRecordId", UUID)
SuppressionId = NewType("SuppressionId", UUID)

CampaignId = NewType("CampaignId", UUID)
CampaignContactId = NewType("CampaignContactId", UUID)

CallId = NewType("CallId", UUID)
CallEventId = NewType("CallEventId", UUID)
ToolExecutionId = NewType("ToolExecutionId", UUID)

OutboxEventId = NewType("OutboxEventId", UUID)
DeadLetterJobId = NewType("DeadLetterJobId", UUID)
AuditLogId = NewType("AuditLogId", UUID)
