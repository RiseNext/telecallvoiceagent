"""Domain event names and payload construction.

Events exist where they buy decoupling — not for every row change. Turning every
mutation into an event produces a system where nothing is traceable because
everything is an event.

An event name is a **contract**. Consumers subscribe to these strings, so they
may be added but never renamed or repurposed.

Phase 1 defines the vocabulary and the helper that builds a well-formed
`OutboxEvent`. Publication is Phase 7 (ADR-008).
"""

from __future__ import annotations

from typing import Any, Final

from rn_core.errors import ValidationError
from rn_core.ids import new_id
from rn_domain.entities.ops import OutboxEvent
from rn_domain.identifiers import OrganizationId, OutboxEventId

__all__ = ["KNOWN_EVENT_TYPES", "EventType", "build_outbox_event"]


class EventType:
    """The published event vocabulary."""

    CALL_STARTED: Final = "call.started"
    CALL_ANSWERED: Final = "call.answered"
    CALL_COMPLETED: Final = "call.completed"
    CALL_FAILED: Final = "call.failed"

    CAMPAIGN_STARTED: Final = "campaign.started"
    CAMPAIGN_COMPLETED: Final = "campaign.completed"

    LEAD_QUALIFIED: Final = "lead.qualified"
    CONTACT_OPTED_OUT: Final = "contact.opted_out"

    AGENT_VERSION_PUBLISHED: Final = "agent_version.published"


KNOWN_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        EventType.CALL_STARTED,
        EventType.CALL_ANSWERED,
        EventType.CALL_COMPLETED,
        EventType.CALL_FAILED,
        EventType.CAMPAIGN_STARTED,
        EventType.CAMPAIGN_COMPLETED,
        EventType.LEAD_QUALIFIED,
        EventType.CONTACT_OPTED_OUT,
        EventType.AGENT_VERSION_PUBLISHED,
    }
)

#: Payload keys that must never be published. An outbox row is read by workers,
#: forwarded to integrations, and retained — it is one of the easiest places for
#: PII to escape the platform without anyone deciding that it should.
_FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "phone",
        "phone_e164",
        "counterparty_phone",
        "transcript",
        "audio",
        "email",
        "full_name",
        "instructions",
    }
)


def build_outbox_event(
    *,
    event_type: str,
    payload: dict[str, Any],
    organization_id: OrganizationId | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
) -> OutboxEvent:
    """Build a validated outbox event.

    Rejects unknown event types and PII-shaped payload keys. Consumers resolve
    identifiers against the database, so a payload only ever needs ids — the
    moment it carries a phone number, that number is in every downstream
    integration.
    """
    if event_type not in KNOWN_EVENT_TYPES:
        raise ValidationError("Unknown domain event type.", detail={"event_type": event_type})

    leaked = {key for key in payload if key.lower() in _FORBIDDEN_PAYLOAD_KEYS}
    if leaked:
        raise ValidationError(
            "Outbox payloads carry identifiers, not personal data.",
            detail={"forbidden_keys": sorted(leaked)},
        )

    return OutboxEvent(
        id=OutboxEventId(new_id()),
        event_type=event_type,
        payload=payload,
        organization_id=organization_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
