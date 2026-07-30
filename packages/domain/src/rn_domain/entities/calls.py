"""Call entities.

`Call` is the durable record of one conversation, inbound or outbound. It is
also the durable projection of an agent *session* — there is no session table,
because a session is in-process state that dies with the call.

Two timing rules that cost a day each if broken:

1. **`started_at` / `answered_at` / `ended_at` are supplied by the application**
   from measured clocks, never by the database's `now()`. `now()` in Postgres is
   transaction-start time, and the finalize transaction begins well after the
   call actually ended.
2. **A call that outlives a provider's session cap and rolls over is still one
   `Call`.** The rollover is a `CallEvent`, not a second row. Creating a second
   row would break every count in the product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rn_core.clock import now_utc
from rn_core.errors import ConflictError, InvariantViolation
from rn_domain.enums import (
    CallDirection,
    CallEndReason,
    CallStatus,
    ToolExecutionStatus,
)
from rn_domain.identifiers import (
    AgentVersionId,
    CallEventId,
    CallId,
    CampaignContactId,
    ContactId,
    OrganizationId,
    ToolExecutionId,
)
from rn_domain.values import LanguageTag, PhoneNumber

__all__ = ["Call", "CallEvent", "CallToolExecution"]

#: Legal call status transitions. Anything absent is rejected, which is what
#: stops an out-of-order or replayed provider callback rewriting our belief
#: about a call — provider callbacks are unsigned and may arrive late (HC-11).
_CALL_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.QUEUED: frozenset({CallStatus.DIALING, CallStatus.CANCELLED, CallStatus.FAILED}),
    CallStatus.DIALING: frozenset(
        {
            CallStatus.RINGING,
            CallStatus.IN_PROGRESS,
            CallStatus.NO_ANSWER,
            CallStatus.BUSY,
            CallStatus.FAILED,
            CallStatus.CANCELLED,
        }
    ),
    CallStatus.RINGING: frozenset(
        {
            CallStatus.IN_PROGRESS,
            CallStatus.NO_ANSWER,
            CallStatus.BUSY,
            CallStatus.FAILED,
            CallStatus.CANCELLED,
        }
    ),
    CallStatus.IN_PROGRESS: frozenset({CallStatus.COMPLETED, CallStatus.FAILED}),
    CallStatus.COMPLETED: frozenset(),
    CallStatus.FAILED: frozenset(),
    CallStatus.NO_ANSWER: frozenset(),
    CallStatus.BUSY: frozenset(),
    CallStatus.CANCELLED: frozenset(),
}


@dataclass(slots=True)
class Call:
    """One phone conversation.

    `agent_version_id` is required and never changes: it is resolved once, before
    any media flows, and pins exactly which configuration served the call.
    """

    id: CallId
    organization_id: OrganizationId
    agent_version_id: AgentVersionId
    direction: CallDirection
    #: The customer's number. The organization's own number is `platform_phone`.
    counterparty_phone: PhoneNumber
    platform_phone: PhoneNumber | None = None
    status: CallStatus = CallStatus.QUEUED
    contact_id: ContactId | None = None
    campaign_contact_id: CampaignContactId | None = None
    #: The telephony provider's own call identifier. Idempotency key for
    #: callbacks and the join key for reconciliation.
    provider_call_sid: str | None = None
    provider: str | None = None
    queued_at: datetime = field(default_factory=now_utc)
    started_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    end_reason: CallEndReason | None = None
    #: Languages actually observed. A set, because a call has no single language.
    languages: tuple[LanguageTag, ...] = ()
    error_code: str | None = None
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if self.direction is CallDirection.OUTBOUND and self.contact_id is None:
            raise InvariantViolation("An outbound call must reference a contact.")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise InvariantViolation("Call duration cannot be negative.")
        if self.answered_at and self.started_at and self.answered_at < self.started_at:
            raise InvariantViolation("A call cannot be answered before it started.")
        if self.ended_at and self.started_at and self.ended_at < self.started_at:
            raise InvariantViolation("A call cannot end before it started.")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def was_answered(self) -> bool:
        return self.answered_at is not None

    @property
    def talk_time_ms(self) -> int | None:
        """Billable conversation time: answer to hangup, not dial to hangup."""
        if self.answered_at is None or self.ended_at is None:
            return None
        return int((self.ended_at - self.answered_at).total_seconds() * 1000)

    def transition_to(self, target: CallStatus) -> None:
        """Move to a new status, refusing illegal transitions.

        Idempotent for a repeat of the current status — a duplicate provider
        callback is expected, not an error.
        """
        if target is self.status:
            return
        if target not in _CALL_TRANSITIONS[self.status]:
            raise ConflictError(
                "Illegal call status transition.",
                detail={"from": self.status.value, "to": target.value},
            )
        self.status = target

    def mark_answered(self, at: datetime) -> None:
        """Record the answer instant, measured by the caller."""
        self.transition_to(CallStatus.IN_PROGRESS)
        if self.answered_at is None:
            self.answered_at = at
        if self.started_at is None:
            self.started_at = at

    def finalize(
        self,
        *,
        status: CallStatus,
        ended_at: datetime,
        reason: CallEndReason | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Close the call.

        Deliberately does not compute `duration_ms` from wall-clock timestamps by
        default: the media plane measures it monotonically, and wall-clock
        subtraction can produce a negative duration across a clock step.
        """
        if not status.is_terminal:
            raise InvariantViolation(
                "finalize requires a terminal status.", detail={"status": status.value}
            )
        self.transition_to(status)
        self.ended_at = ended_at
        self.end_reason = reason
        if duration_ms is not None:
            if duration_ms < 0:
                raise InvariantViolation("Call duration cannot be negative.")
            self.duration_ms = duration_ms
        elif self.started_at is not None:
            self.duration_ms = max(int((ended_at - self.started_at).total_seconds() * 1000), 0)


@dataclass(slots=True)
class CallEvent:
    """A journal entry in *our* state machine for a call.

    Distinct from the raw provider webhook ledger, which records what a provider
    claimed. This records what we believe. Collapsing the two would let a
    replayed provider callback rewrite our state.

    Append-only, and **never per audio frame** — lifecycle transitions only.
    """

    id: CallEventId
    organization_id: OrganizationId
    call_id: CallId
    event_type: str
    occurred_at: datetime = field(default_factory=now_utc)
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise InvariantViolation("Call event type must not be blank.")


@dataclass(slots=True)
class CallToolExecution:
    """One tool invocation during a call — arguments, result, latency, outcome.

    Persisted for audit, evaluation and debugging. `DENIED` is a distinct outcome
    from `FAILED` on purpose: a denial means the platform refused a tool the
    agent was not permitted to use, which is a security event rather than an
    outage, and the two must be countable separately.

    Arguments and results are JSONB and may contain customer data, so this table
    is in scope for erasure and its contents never reach a span attribute.
    """

    id: ToolExecutionId
    organization_id: OrganizationId
    call_id: CallId
    tool_name: str
    status: ToolExecutionStatus
    started_at: datetime = field(default_factory=now_utc)
    duration_ms: int | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise InvariantViolation("Tool name must not be blank.")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise InvariantViolation("Tool execution duration cannot be negative.")
        if self.status is ToolExecutionStatus.SUCCEEDED and self.error_message:
            raise InvariantViolation("A successful tool execution must not carry an error.")
