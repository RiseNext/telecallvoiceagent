"""Campaign entities.

`CampaignContact` is a **dispatch state machine**, not a join table. It carries
the attempt count, the next attempt time, why it was excluded, and which call
last touched it. Each dial attempt creates a `Call`; retries are several calls
pointing at one `CampaignContact` row. There is deliberately no separate
"attempt" entity — the attempt *is* the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rn_core.clock import now_utc
from rn_core.errors import ConflictError, InvariantViolation
from rn_domain.enums import CampaignContactStatus, CampaignStatus
from rn_domain.identifiers import (
    AgentVersionId,
    CallId,
    CampaignContactId,
    CampaignId,
    ContactId,
    OrganizationId,
)

__all__ = ["Campaign", "CampaignContact"]

#: Legal transitions. A campaign that could jump from `completed` back to
#: `running` would resurrect a finished dial list, so the map is explicit.
_CAMPAIGN_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset({CampaignStatus.SCHEDULED, CampaignStatus.CANCELLED}),
    CampaignStatus.SCHEDULED: frozenset(
        {CampaignStatus.RUNNING, CampaignStatus.PAUSED, CampaignStatus.CANCELLED}
    ),
    CampaignStatus.RUNNING: frozenset(
        {CampaignStatus.PAUSED, CampaignStatus.COMPLETED, CampaignStatus.CANCELLED}
    ),
    CampaignStatus.PAUSED: frozenset({CampaignStatus.RUNNING, CampaignStatus.CANCELLED}),
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.CANCELLED: frozenset(),
}


@dataclass(slots=True)
class Campaign:
    """An outbound calling campaign.

    `agent_version_id` pins the behaviour for the whole campaign. Publishing a
    new agent version mid-campaign does not change what in-flight calls do — the
    version is resolved once, at dial-enqueue time.
    """

    id: CampaignId
    organization_id: OrganizationId
    name: str
    agent_version_id: AgentVersionId
    status: CampaignStatus = CampaignStatus.DRAFT
    scheduled_start_at: datetime | None = None
    #: Ceiling on simultaneous calls for this campaign. Admission control, not a
    #: capacity claim — the dispatcher takes the minimum of this, the
    #: organization budget, the platform budget and the provider rate limit.
    max_concurrent_calls: int = 1
    max_attempts_per_contact: int = 1
    created_at: datetime = field(default_factory=now_utc)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvariantViolation("Campaign name must not be blank.")
        if self.max_concurrent_calls < 1:
            raise InvariantViolation("max_concurrent_calls must be at least 1.")
        if self.max_attempts_per_contact < 1:
            raise InvariantViolation("max_attempts_per_contact must be at least 1.")

    @property
    def is_dispatchable(self) -> bool:
        return self.status is CampaignStatus.RUNNING and self.deleted_at is None

    def transition_to(self, target: CampaignStatus, *, at: datetime | None = None) -> None:
        """Move to a new status, refusing illegal transitions."""
        allowed = _CAMPAIGN_TRANSITIONS[self.status]
        if target not in allowed:
            raise ConflictError(
                "Illegal campaign status transition.",
                detail={"from": self.status.value, "to": target.value},
            )
        moment = at or now_utc()
        if target is CampaignStatus.RUNNING and self.started_at is None:
            self.started_at = moment
        if target in {CampaignStatus.COMPLETED, CampaignStatus.CANCELLED}:
            self.completed_at = moment
        self.status = target


@dataclass(slots=True)
class CampaignContact:
    """One contact's dispatch state within one campaign."""

    id: CampaignContactId
    organization_id: OrganizationId
    campaign_id: CampaignId
    contact_id: ContactId
    status: CampaignContactStatus = CampaignContactStatus.PENDING
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    last_call_id: CallId | None = None
    #: Which pre-dial check rejected this contact. Populated only when excluded.
    #: Aggregating this column is a compliance signal, not just a debugging aid —
    #: a spike in consent exclusions means a tenant uploaded a bad list.
    excluded_reason: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise InvariantViolation("attempt_count cannot be negative.")
        if self.status is CampaignContactStatus.EXCLUDED and not self.excluded_reason:
            raise InvariantViolation("An excluded campaign contact must record a reason.")

    def exclude(self, reason: str, *, at: datetime | None = None) -> None:
        """Reject this contact before dialling. Terminal."""
        if not reason.strip():
            raise InvariantViolation("An exclusion must state a reason.")
        self.status = CampaignContactStatus.EXCLUDED
        self.excluded_reason = reason
        self.updated_at = at or now_utc()

    def mark_in_flight(self, call_id: CallId, *, at: datetime | None = None) -> None:
        """Record that a dial has been issued.

        Increments the attempt count here rather than on completion so that a
        crash between dialling and the callback cannot produce an infinite retry
        loop against a real phone number.
        """
        if self.status is CampaignContactStatus.EXCLUDED:
            raise ConflictError("An excluded campaign contact must not be dialled.")
        if self.status is CampaignContactStatus.IN_FLIGHT:
            raise ConflictError(
                "This campaign contact already has a call in flight.",
                detail={"last_call_id": str(self.last_call_id)},
            )
        self.status = CampaignContactStatus.IN_FLIGHT
        self.attempt_count += 1
        self.last_call_id = call_id
        self.next_attempt_at = None
        self.updated_at = at or now_utc()

    def mark_completed(self, *, at: datetime | None = None) -> None:
        self.status = CampaignContactStatus.COMPLETED
        self.next_attempt_at = None
        self.updated_at = at or now_utc()

    def schedule_retry(self, when: datetime, *, at: datetime | None = None) -> None:
        """Queue another attempt. The caller has already checked the policy."""
        self.status = CampaignContactStatus.ELIGIBLE
        self.next_attempt_at = when
        self.updated_at = at or now_utc()

    def mark_failed(self, *, at: datetime | None = None) -> None:
        self.status = CampaignContactStatus.FAILED
        self.next_attempt_at = None
        self.updated_at = at or now_utc()

    def has_attempts_remaining(self, max_attempts: int) -> bool:
        return self.attempt_count < max_attempts
