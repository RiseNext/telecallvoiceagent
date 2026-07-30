"""Domain enumerations.

These are the canonical value lists. The database stores `text` with a `CHECK`
constraint generated from them rather than a native Postgres enum type: native
enums cannot be reordered, values cannot be removed, and `ALTER TYPE ... ADD
VALUE` has awkward transaction rules. A `CHECK` can be replaced with
`ADD CONSTRAINT ... NOT VALID` followed by `VALIDATE CONSTRAINT`, which never
blocks writes (DATA_MODEL §1).

What belongs here: stable *domain* concepts whose values the product reasons
about. What does not: provider-specific vocabularies. Exotel's call-status
strings and OpenAI's event names are adapter concerns — translating them at the
seam is the adapter's job, and lifting them into the domain would make our state
machine a function of a vendor's release notes.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AgentStatus",
    "AgentVersionStatus",
    "CallDirection",
    "CallEndReason",
    "CallStatus",
    "CampaignContactStatus",
    "CampaignStatus",
    "ConsentSource",
    "ConsentStatus",
    "ContactStatus",
    "LeadQualification",
    "LeadStatus",
    "OrganizationStatus",
    "OutboxStatus",
    "SuppressionReason",
    "SuppressionScope",
    "ToolExecutionStatus",
]


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    """Retained and readable, but may not dial. Used for non-payment and for a
    compliance hold — the distinction is recorded in an audit log, not here."""
    ARCHIVED = "archived"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AgentVersionStatus(StrEnum):
    """A version's lifecycle. Behaviour columns freeze at `published`."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ContactStatus(StrEnum):
    ACTIVE = "active"
    INVALID_NUMBER = "invalid_number"
    """Provider reported the number as unreachable/invalid. Not a suppression —
    suppression is a decision by a person, this is a fact about the line."""
    DO_NOT_CALL = "do_not_call"
    """Denormalised convenience flag. The authoritative blocklist is
    `suppressions`; this exists so a dashboard list does not need a join."""


class LeadStatus(StrEnum):
    OPEN = "open"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    CONVERTED = "converted"
    LOST = "lost"


class LeadQualification(StrEnum):
    """How interested the prospect is, as judged from a conversation.

    Deliberately coarse. A finer scale invites the model to express confidence it
    does not have, and post-call analysis is schema-constrained precisely so the
    dashboard is not parsing shades of enthusiasm.
    """

    UNKNOWN = "unknown"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    FOLLOW_UP = "follow_up"
    NOT_ELIGIBLE = "not_eligible"


class ConsentSource(StrEnum):
    """Where an opt-in came from. Evidence quality varies and callers must know."""

    WEB_FORM = "web_form"
    INBOUND_CALL = "inbound_call"
    WHATSAPP = "whatsapp"
    IMPORT = "import"
    """Asserted by a tenant during a contact import. The weakest form: the
    platform has no independent evidence, and D-3 governs who is liable."""
    VERBAL_ON_CALL = "verbal_on_call"
    OTHER = "other"


class ConsentStatus(StrEnum):
    GRANTED = "granted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SuppressionScope(StrEnum):
    ORGANIZATION = "organization"
    PLATFORM = "platform"
    """"Never call me from this platform again." A per-organization design cannot
    express this, which is why `suppressions.organization_id` is nullable."""


class SuppressionReason(StrEnum):
    USER_REQUEST = "user_request"
    REGULATOR_DND = "regulator_dnd"
    COMPLAINT = "complaint"
    BOUNCED = "bounced"
    MANUAL = "manual"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CampaignContactStatus(StrEnum):
    """The dispatch state machine for one contact within one campaign."""

    PENDING = "pending"
    ELIGIBLE = "eligible"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"
    EXCLUDED = "excluded"
    """Failed the pre-dial compliance gate. `excluded_reason` says which check,
    which makes exclusion counts a compliance signal rather than a mystery."""


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    """Our belief about a call. Not a provider's vocabulary.

    `QUEUED → DIALING → RINGING → IN_PROGRESS → COMPLETED` is the happy path;
    every other terminal value is a way it did not happen.
    """

    QUEUED = "queued"
    DIALING = "dialing"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_CALL_STATUSES


_TERMINAL_CALL_STATUSES = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.FAILED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.CANCELLED,
    }
)


class CallEndReason(StrEnum):
    CALLER_HUNG_UP = "caller_hung_up"
    AGENT_ENDED = "agent_ended"
    OPT_OUT = "opt_out"
    PROVIDER_DISCONNECT = "provider_disconnect"
    MAX_DURATION = "max_duration"
    ERROR = "error"


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    """Refused by the platform: the tool was not enabled for this agent or
    organization, or an argument failed validation. Distinct from `FAILED`
    because a denial is a security-relevant event, not an outage."""
    TIMED_OUT = "timed_out"


class OutboxStatus(StrEnum):
    """Derived from `published_at`; kept as an enum for readable queries."""

    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
