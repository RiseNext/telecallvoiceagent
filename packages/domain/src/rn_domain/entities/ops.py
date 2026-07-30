"""Platform and operations entities.

`OutboxEvent` is the one that shapes the architecture. The voice gateway must not
dual-write to Postgres and a broker: a crash between the two either loses a
call-completion event or duplicates it. Instead the state change and the
intent-to-publish are written in **one transaction**, and a relay publishes
later (ADR-008).

Phase 1 builds the durable half only. The relay, the broker and the dead-letter
middleware are Phase 7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rn_core.clock import now_utc
from rn_core.errors import InvariantViolation
from rn_domain.identifiers import (
    AuditLogId,
    DeadLetterJobId,
    OrganizationId,
    OutboxEventId,
    UserId,
)

__all__ = ["AuditLogEntry", "DeadLetterJob", "OutboxEvent"]

_MAX_EVENT_TYPE_LENGTH = 100


@dataclass(slots=True)
class OutboxEvent:
    """A domain event awaiting publication.

    `id` is a UUIDv7, so `ORDER BY id` **is** insertion order and the relay needs
    no separate sequence. Ordering is per-aggregate, not global: the relay claims
    with `FOR UPDATE SKIP LOCKED`, so two relay workers can make progress
    concurrently and strict global ordering is explicitly not offered.

    `organization_id` is nullable — a platform-level event has no tenant.

    Delivery is **at-least-once**. Consumers must be idempotent; that is a
    property of the consumer, not something this row can provide.
    """

    id: OutboxEventId
    event_type: str
    payload: dict[str, Any]
    organization_id: OrganizationId | None = None
    #: The entity this event is about, for per-aggregate ordering and debugging.
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    published_at: datetime | None = None
    attempt_count: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise InvariantViolation("Outbox event type must not be blank.")
        if len(self.event_type) > _MAX_EVENT_TYPE_LENGTH:
            raise InvariantViolation("Outbox event type is too long.")
        if self.attempt_count < 0:
            raise InvariantViolation("attempt_count cannot be negative.")

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    def mark_published(self, *, at: datetime | None = None) -> None:
        self.published_at = at or now_utc()
        self.last_error = None


@dataclass(slots=True)
class DeadLetterJob:
    """A job that exhausted its retries.

    Platform-global: a job may fail before its tenant is even known, so forcing
    an `organization_id` would mean inventing one.

    **Diagnostics, not a dump.** `payload` and `error_message` are written by the
    job system, which means they can carry whatever a failing job was holding —
    including a phone number or a provider token. Redaction is applied before
    writing, and the retention window is short. The temptation to "just store the
    whole exception context" is how a debugging table becomes an unmonitored PII
    store.
    """

    id: DeadLetterJobId
    task_name: str
    error_message: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    organization_id: OrganizationId | None = None
    failed_at: datetime = field(default_factory=now_utc)
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not self.task_name.strip():
            raise InvariantViolation("Dead letter job requires a task name.")


@dataclass(slots=True)
class AuditLogEntry:
    """An append-only record of a security-relevant action.

    Written for: authorization denials, cross-tenant access attempts, exports,
    consent and suppression changes, agent publishes, and membership changes.

    Never updated, never deleted outside retention. `actor_user_id` is nullable
    because the actor may be the platform itself (a scheduled job) rather than a
    person — and "nobody" is a meaningful answer that `NULL` states honestly.
    """

    id: AuditLogId
    action: str
    organization_id: OrganizationId | None = None
    actor_user_id: UserId | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    #: Safe context only: counts, filters, decisions. Never request bodies,
    #: never transcript text, never a full phone number.
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise InvariantViolation("Audit log entry requires an action.")
