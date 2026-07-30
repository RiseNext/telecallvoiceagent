"""Campaign and call tables — the operational core.

`campaign_contacts` is a dispatch state machine, not a join table. Each dial
attempt creates a `calls` row; retries are several calls pointing at one
`campaign_contacts` row. There is no separate attempt table because the attempt
*is* the call.

`call_events` is **our** state machine, deliberately separate from the raw
provider webhook ledger that arrives in Phase 8. One records what we believe, the
other what a provider claimed; collapsing them would let a replayed provider
callback rewrite our state.

Nothing here is written per audio frame. Ever.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from rn_domain.entities.calls import Call, CallEvent, CallToolExecution
from rn_domain.entities.campaigns import Campaign, CampaignContact
from rn_domain.enums import (
    CallDirection,
    CallEndReason,
    CallStatus,
    CampaignContactStatus,
    CampaignStatus,
    ToolExecutionStatus,
)
from rn_domain.identifiers import (
    AgentVersionId,
    CallEventId,
    CallId,
    CampaignContactId,
    CampaignId,
    ContactId,
    OrganizationId,
    ToolExecutionId,
)
from rn_domain.values import LanguageTag, PhoneNumber
from rn_persistence.base import (
    TenantOwnedBase,
    created_at_column,
    enum_check,
    json_column,
)

__all__ = [
    "CallEventModel",
    "CallModel",
    "CallToolExecutionModel",
    "CampaignContactModel",
    "CampaignModel",
]


class CampaignModel(TenantOwnedBase):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduled_start_at: Mapped[datetime | None] = mapped_column(nullable=True)
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts_per_contact: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.id"],
            name="fk_campaigns_organization_id_agent_version_id_agent_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id", name="uq_campaigns_organization_id_id"),
        enum_check("status", CampaignStatus, "status"),
        CheckConstraint("max_concurrent_calls >= 1", name="max_concurrent"),
        CheckConstraint("max_attempts_per_contact >= 1", name="max_attempts"),
        Index(
            "ix_campaigns_organization_id_status_created_at",
            "organization_id",
            "status",
            "created_at",
        ),
    )

    def to_domain(self) -> Campaign:
        return Campaign(
            id=CampaignId(self.id),
            organization_id=OrganizationId(self.organization_id),
            name=self.name,
            agent_version_id=AgentVersionId(self.agent_version_id),
            status=CampaignStatus(self.status),
            scheduled_start_at=self.scheduled_start_at,
            max_concurrent_calls=self.max_concurrent_calls,
            max_attempts_per_contact=self.max_attempts_per_contact,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, entity: Campaign) -> CampaignModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            name=entity.name,
            agent_version_id=entity.agent_version_id,
            status=entity.status.value,
            scheduled_start_at=entity.scheduled_start_at,
            max_concurrent_calls=entity.max_concurrent_calls,
            max_attempts_per_contact=entity.max_attempts_per_contact,
            started_at=entity.started_at,
            completed_at=entity.completed_at,
            created_at=entity.created_at,
            deleted_at=entity.deleted_at,
        )

    def apply(self, entity: Campaign) -> None:
        self.name = entity.name
        self.status = entity.status.value
        self.scheduled_start_at = entity.scheduled_start_at
        self.max_concurrent_calls = entity.max_concurrent_calls
        self.max_attempts_per_contact = entity.max_attempts_per_contact
        self.started_at = entity.started_at
        self.completed_at = entity.completed_at
        self.deleted_at = entity.deleted_at


class CampaignContactModel(TenantOwnedBase):
    __tablename__ = "campaign_contacts"

    campaign_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # DELIBERATELY not a foreign key. `calls` already carries a composite FK to
    # `campaign_contacts`, so adding the reverse would make a circular FK pair
    # between the two tables: neither row could be inserted first without a
    # DEFERRABLE constraint, and deferred violations surface at COMMIT with
    # diagnostics that point at the transaction rather than the statement.
    #
    # The tenancy guarantee is not lost — it comes from the other direction.
    # `calls(organization_id, campaign_contact_id) -> campaign_contacts` means a
    # call can only ever point at a campaign contact in its own tenant, so a
    # `last_call_id` written from that call is same-tenant by construction.
    last_call_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    excluded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "campaign_id"],
            ["campaigns.organization_id", "campaigns.id"],
            name="fk_campaign_contacts_organization_id_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "contact_id"],
            ["contacts.organization_id", "contacts.id"],
            name="fk_campaign_contacts_organization_id_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id", name="uq_campaign_contacts_organization_id_id"),
        # A contact appears at most once per campaign — the guard against an
        # import running twice and dialling everyone twice.
        UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_contacts_campaign_contact"),
        enum_check("status", CampaignContactStatus, "status"),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        CheckConstraint(
            "status <> 'excluded' OR excluded_reason IS NOT NULL",
            name="excluded_reason",
        ),
        # THE dispatcher query: which contacts in this campaign are due now.
        # Partial, because the dispatcher only ever looks at two statuses and the
        # completed rows are the overwhelming majority once a campaign has run.
        Index(
            "ix_campaign_contacts_due",
            "campaign_id",
            "next_attempt_at",
            postgresql_where="status IN ('pending', 'eligible')",
        ),
        Index("ix_campaign_contacts_contact_id", "contact_id"),
    )

    def to_domain(self) -> CampaignContact:
        return CampaignContact(
            id=CampaignContactId(self.id),
            organization_id=OrganizationId(self.organization_id),
            campaign_id=CampaignId(self.campaign_id),
            contact_id=ContactId(self.contact_id),
            status=CampaignContactStatus(self.status),
            attempt_count=self.attempt_count,
            next_attempt_at=self.next_attempt_at,
            last_call_id=CallId(self.last_call_id) if self.last_call_id else None,
            excluded_reason=self.excluded_reason,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, entity: CampaignContact) -> CampaignContactModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            campaign_id=entity.campaign_id,
            contact_id=entity.contact_id,
            status=entity.status.value,
            attempt_count=entity.attempt_count,
            next_attempt_at=entity.next_attempt_at,
            last_call_id=entity.last_call_id,
            excluded_reason=entity.excluded_reason,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    def apply(self, entity: CampaignContact) -> None:
        self.status = entity.status.value
        self.attempt_count = entity.attempt_count
        self.next_attempt_at = entity.next_attempt_at
        self.last_call_id = entity.last_call_id
        self.excluded_reason = entity.excluded_reason
        self.updated_at = entity.updated_at


class CallModel(TenantOwnedBase):
    """One phone conversation.

    Call timing columns (`started_at`, `answered_at`, `ended_at`) have **no
    server default**: they are supplied by the application from measured clocks.
    Postgres' `now()` is transaction-start time, and the finalizing transaction
    begins well after the call actually ended.
    """

    __tablename__ = "calls"

    agent_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    counterparty_phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    campaign_contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_call_sid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.id"],
            name="fk_calls_organization_id_agent_version_id_agent_versions",
            # An agent version that has served a call can never be deleted.
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "contact_id"],
            ["contacts.organization_id", "contacts.id"],
            name="fk_calls_organization_id_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "campaign_contact_id"],
            ["campaign_contacts.organization_id", "campaign_contacts.id"],
            name="fk_calls_organization_id_campaign_contact_id_campaign_contacts",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id", name="uq_calls_organization_id_id"),
        # Idempotency for provider callbacks, which are unsigned, may be
        # duplicated and may arrive out of order (HC-10, HC-11).
        UniqueConstraint("provider", "provider_call_sid", name="uq_calls_provider_sid"),
        enum_check("direction", CallDirection, "direction"),
        enum_check("status", CallStatus, "status"),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN "
            "('caller_hung_up','agent_ended','opt_out','provider_disconnect',"
            "'max_duration','error')",
            name="end_reason",
        ),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration"),
        CheckConstraint(
            "answered_at IS NULL OR started_at IS NULL OR answered_at >= started_at",
            name="answered_after_started",
        ),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ended_after_started",
        ),
        CheckConstraint(
            "direction <> 'outbound' OR contact_id IS NOT NULL",
            name="outbound_requires_contact",
        ),
        # The dashboard call list: one org, filtered by date. `queued_at` rather
        # than `created_at` because that is what the UI sorts on.
        Index("ix_calls_organization_id_queued_at", "organization_id", "queued_at"),
        # The filtered list: org + status + date.
        Index(
            "ix_calls_organization_id_status_queued_at",
            "organization_id",
            "status",
            "queued_at",
        ),
        Index("ix_calls_contact_id", "contact_id"),
        Index("ix_calls_campaign_contact_id", "campaign_contact_id"),
        # The reconciliation job: calls that never reached a terminal status.
        # Partial, so it stays small no matter how many calls have completed.
        Index(
            "ix_calls_unreconciled",
            "queued_at",
            postgresql_where=(
                "status NOT IN ('completed','failed','no_answer','busy','cancelled')"
            ),
        ),
    )

    def to_domain(self) -> Call:
        return Call(
            id=CallId(self.id),
            organization_id=OrganizationId(self.organization_id),
            agent_version_id=AgentVersionId(self.agent_version_id),
            direction=CallDirection(self.direction),
            counterparty_phone=PhoneNumber(self.counterparty_phone_e164),
            platform_phone=(
                PhoneNumber(self.platform_phone_e164) if self.platform_phone_e164 else None
            ),
            status=CallStatus(self.status),
            contact_id=ContactId(self.contact_id) if self.contact_id else None,
            campaign_contact_id=(
                CampaignContactId(self.campaign_contact_id) if self.campaign_contact_id else None
            ),
            provider_call_sid=self.provider_call_sid,
            provider=self.provider,
            queued_at=self.queued_at,
            started_at=self.started_at,
            answered_at=self.answered_at,
            ended_at=self.ended_at,
            duration_ms=self.duration_ms,
            end_reason=CallEndReason(self.end_reason) if self.end_reason else None,
            languages=tuple(LanguageTag(tag) for tag in self.languages),
            error_code=self.error_code,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: Call) -> CallModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            agent_version_id=entity.agent_version_id,
            direction=entity.direction.value,
            counterparty_phone_e164=entity.counterparty_phone.e164,
            platform_phone_e164=entity.platform_phone.e164 if entity.platform_phone else None,
            status=entity.status.value,
            contact_id=entity.contact_id,
            campaign_contact_id=entity.campaign_contact_id,
            provider=entity.provider,
            provider_call_sid=entity.provider_call_sid,
            queued_at=entity.queued_at,
            started_at=entity.started_at,
            answered_at=entity.answered_at,
            ended_at=entity.ended_at,
            duration_ms=entity.duration_ms,
            end_reason=entity.end_reason.value if entity.end_reason else None,
            languages=[str(tag) for tag in entity.languages],
            error_code=entity.error_code,
            created_at=entity.created_at,
        )

    def apply(self, entity: Call) -> None:
        self.status = entity.status.value
        self.provider = entity.provider
        self.provider_call_sid = entity.provider_call_sid
        self.started_at = entity.started_at
        self.answered_at = entity.answered_at
        self.ended_at = entity.ended_at
        self.duration_ms = entity.duration_ms
        self.end_reason = entity.end_reason.value if entity.end_reason else None
        self.languages = [str(tag) for tag in entity.languages]
        self.error_code = entity.error_code


class CallEventModel(TenantOwnedBase):
    """Our own call state journal. Append-only, lifecycle transitions only."""

    __tablename__ = "call_events"

    call_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = json_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.id"],
            name="fk_call_events_organization_id_call_id_calls",
            ondelete="CASCADE",
        ),
        # The call-detail timeline. `occurred_at` and not the id, because the
        # order that matters is when things happened, not when we recorded them.
        Index("ix_call_events_call_id_occurred_at", "call_id", "occurred_at"),
    )

    def to_domain(self) -> CallEvent:
        return CallEvent(
            id=CallEventId(self.id),
            organization_id=OrganizationId(self.organization_id),
            call_id=CallId(self.call_id),
            event_type=self.event_type,
            occurred_at=self.occurred_at,
            payload=dict(self.payload),
        )

    @classmethod
    def from_domain(cls, entity: CallEvent) -> CallEventModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            call_id=entity.call_id,
            event_type=entity.event_type,
            occurred_at=entity.occurred_at,
            payload=dict(entity.payload),
        )


class CallToolExecutionModel(TenantOwnedBase):
    """One tool invocation during a call.

    `arguments` and `result` are JSONB and may contain customer data, so this
    table is in scope for erasure and its contents never become span attributes.
    """

    __tablename__ = "call_tool_executions"

    call_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arguments: Mapped[dict[str, Any]] = json_column()
    result: Mapped[dict[str, Any]] = json_column()
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.id"],
            name="fk_call_tool_executions_organization_id_call_id_calls",
            ondelete="CASCADE",
        ),
        enum_check("status", ToolExecutionStatus, "status"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration"),
        Index("ix_call_tool_executions_call_id_started_at", "call_id", "started_at"),
        # "How often is this tool denied or failing?" — a security and quality
        # signal, and the reason `denied` is a distinct status from `failed`.
        Index(
            "ix_call_tool_executions_organization_id_tool_name_status",
            "organization_id",
            "tool_name",
            "status",
        ),
    )

    def to_domain(self) -> CallToolExecution:
        return CallToolExecution(
            id=ToolExecutionId(self.id),
            organization_id=OrganizationId(self.organization_id),
            call_id=CallId(self.call_id),
            tool_name=self.tool_name,
            status=ToolExecutionStatus(self.status),
            started_at=self.started_at,
            duration_ms=self.duration_ms,
            arguments=dict(self.arguments),
            result=dict(self.result),
            error_message=self.error_message,
        )

    @classmethod
    def from_domain(cls, entity: CallToolExecution) -> CallToolExecutionModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            call_id=entity.call_id,
            tool_name=entity.tool_name,
            status=entity.status.value,
            started_at=entity.started_at,
            duration_ms=entity.duration_ms,
            arguments=dict(entity.arguments),
            result=dict(entity.result),
            error_message=entity.error_message,
        )
