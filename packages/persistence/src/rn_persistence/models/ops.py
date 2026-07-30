"""Platform and operations tables: outbox, dead-letter, audit.

The outbox is the one that shapes the architecture (ADR-008). Phase 1 builds the
durable half only — the relay, the broker and the dead-letter middleware are
Phase 7. What matters now is that an outbox row can be written **in the same
transaction** as the state change it describes.

Ordering note: the relay claims work ordered by **`(created_at, id)`**, not by
`id` alone. `id` is a UUIDv7 and is therefore time-ordered in practice, but
temporal business semantics must rest on an explicit timestamp — UUID ordering
is an implementation property of the generator, not a guarantee we want
correctness to depend on. `id` remains as a deterministic tiebreak so the order
is total and reproducible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from rn_domain.entities.ops import AuditLogEntry, DeadLetterJob, OutboxEvent
from rn_domain.identifiers import (
    AuditLogId,
    DeadLetterJobId,
    OrganizationId,
    OutboxEventId,
    UserId,
)
from rn_persistence.base import (
    Base,
    created_at_column,
    json_column,
    nullable_organization_fk,
)

__all__ = ["AuditLogModel", "DeadLetterJobModel", "OutboxEventModel"]


class OutboxEventModel(Base):
    """A domain event awaiting publication."""

    __tablename__ = "outbox"

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = json_column()
    # Nullable: a platform-level event has no tenant.
    organization_id: Mapped[uuid.UUID | None] = nullable_organization_fk()
    aggregate_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        # THE relay query. Partial on the unpublished rows so the index stays
        # proportional to the backlog rather than to everything ever published —
        # which, on a table that only grows, is the difference between an index
        # that fits in memory and one that does not.
        Index(
            "ix_outbox_unpublished",
            "created_at",
            "id",
            postgresql_where="published_at IS NULL",
        ),
    )

    def to_domain(self) -> OutboxEvent:
        return OutboxEvent(
            id=OutboxEventId(self.id),
            event_type=self.event_type,
            payload=dict(self.payload),
            organization_id=(
                OrganizationId(self.organization_id) if self.organization_id else None
            ),
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            created_at=self.created_at,
            published_at=self.published_at,
            attempt_count=self.attempt_count,
            last_error=self.last_error,
        )

    @classmethod
    def from_domain(cls, entity: OutboxEvent) -> OutboxEventModel:
        return cls(
            id=entity.id,
            event_type=entity.event_type,
            payload=dict(entity.payload),
            organization_id=entity.organization_id,
            aggregate_type=entity.aggregate_type,
            aggregate_id=entity.aggregate_id,
            created_at=entity.created_at,
            published_at=entity.published_at,
            attempt_count=entity.attempt_count,
            last_error=entity.last_error,
        )


class DeadLetterJobModel(Base):
    """A job that exhausted its retries. Platform-global.

    Diagnostics, not a dump. Whatever a failing job was holding can end up in
    `payload`, so the writer redacts before inserting and retention is short.
    """

    __tablename__ = "dead_letter_jobs"

    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = json_column()
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Nullable: a job can fail before its tenant is known, and inventing one
    # would be worse than recording that we do not know.
    organization_id: Mapped[uuid.UUID | None] = nullable_organization_fk()
    failed_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        Index("ix_dead_letter_jobs_failed_at", "failed_at"),
        Index("ix_dead_letter_jobs_task_name_failed_at", "task_name", "failed_at"),
    )

    def to_domain(self) -> DeadLetterJob:
        return DeadLetterJob(
            id=DeadLetterJobId(self.id),
            task_name=self.task_name,
            error_message=self.error_message,
            payload=dict(self.payload),
            attempt_count=self.attempt_count,
            organization_id=(
                OrganizationId(self.organization_id) if self.organization_id else None
            ),
            failed_at=self.failed_at,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: DeadLetterJob) -> DeadLetterJobModel:
        return cls(
            id=entity.id,
            task_name=entity.task_name,
            error_message=entity.error_message,
            payload=dict(entity.payload),
            attempt_count=entity.attempt_count,
            organization_id=entity.organization_id,
            failed_at=entity.failed_at,
            created_at=entity.created_at,
        )


class AuditLogModel(Base):
    """Append-only record of security-relevant actions.

    Never updated, never deleted outside retention. `organization_id` is nullable
    because a platform action has no tenant, and `actor_user_id` is nullable
    because the actor may be the platform itself rather than a person — "nobody"
    is a meaningful answer that NULL states honestly.
    """

    __tablename__ = "audit_logs"

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = nullable_organization_fk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Safe context only: counts, filters, decisions. Never request bodies, never
    # transcript text, never a full phone number.
    audit_metadata: Mapped[dict[str, Any]] = json_column()
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # The audit trail view: one org, newest first.
        Index("ix_audit_logs_organization_id_created_at", "organization_id", "created_at"),
        # "Everything this person did" — the question asked during an incident.
        Index("ix_audit_logs_actor_user_id_created_at", "actor_user_id", "created_at"),
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
    )

    def to_domain(self) -> AuditLogEntry:
        return AuditLogEntry(
            id=AuditLogId(self.id),
            action=self.action,
            organization_id=(
                OrganizationId(self.organization_id) if self.organization_id else None
            ),
            actor_user_id=UserId(self.actor_user_id) if self.actor_user_id else None,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            metadata=dict(self.audit_metadata),
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: AuditLogEntry) -> AuditLogModel:
        return cls(
            id=entity.id,
            action=entity.action,
            organization_id=entity.organization_id,
            actor_user_id=entity.actor_user_id,
            resource_type=entity.resource_type,
            resource_id=entity.resource_id,
            audit_metadata=dict(entity.metadata),
            created_at=entity.created_at,
        )
