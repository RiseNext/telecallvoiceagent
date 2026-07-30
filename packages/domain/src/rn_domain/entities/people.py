"""People and compliance entities.

`Contact` and `Lead` are sharply separated and stay that way. A contact is *a
phone number this organization may dial* — the dedup and suppression key. A lead
is *a qualified commercial opportunity* found in a conversation. One contact can
produce several leads over a year, and merging them would make "interest level"
a permanent property of a human being, which is both wrong and gets worse as the
platform ages.

`ConsentRecord` and `Suppression` are also two things, with opposite lifecycles
and opposite privacy postures:

* consent is **evidence**, append-only, and must be *producible* — it keeps the
  plaintext number because a hash cannot be shown to a regulator (HC-14).
* suppression is a **blocklist**, checked before every dial, and keeps **no
  plaintext at all**. A blocklist should not also be a phone-number database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rn_core.clock import now_utc
from rn_core.errors import ConflictError, InvariantViolation
from rn_domain.enums import (
    ConsentSource,
    ConsentStatus,
    ContactStatus,
    LeadQualification,
    LeadStatus,
    SuppressionReason,
    SuppressionScope,
)
from rn_domain.identifiers import (
    CallId,
    ConsentRecordId,
    ContactId,
    LeadId,
    OrganizationId,
    SuppressionId,
    UserId,
)
from rn_domain.values import PhoneNumber

__all__ = ["ConsentRecord", "Contact", "Lead", "Suppression"]


@dataclass(slots=True)
class Contact:
    """A person an organization may dial, identified by phone number.

    Uniqueness is `(organization_id, phone_e164)`: the same number appearing in
    two tenants is two contacts, because consent and suppression are asserted per
    tenant and must not leak between them.
    """

    id: ContactId
    organization_id: OrganizationId
    phone: PhoneNumber
    full_name: str | None = None
    email: str | None = None
    status: ContactStatus = ContactStatus.ACTIVE
    #: Open-shaped tenant-supplied fields from an import. Bounded at the
    #: application boundary — an unbounded blob from a CSV column is a denial-of-
    #: service vector and an accidental PII store nobody knows about.
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)
    deleted_at: datetime | None = None

    @property
    def is_dialable(self) -> bool:
        """Cheap local check. **Not** the compliance gate.

        The authoritative pre-dial decision consults `suppressions`, consent, the
        calling window and campaign policy. This only avoids obviously pointless
        work.
        """
        return self.status is ContactStatus.ACTIVE and self.deleted_at is None

    def mark_do_not_call(self) -> None:
        """Flag locally. The durable, cross-campaign block is a `Suppression`."""
        self.status = ContactStatus.DO_NOT_CALL


@dataclass(slots=True)
class Lead:
    """A commercial opportunity discovered in a conversation.

    `contact_id` is required — leads never float free. A lead without the person
    it belongs to cannot be followed up, deduplicated, or erased on request.
    """

    id: LeadId
    organization_id: OrganizationId
    contact_id: ContactId
    status: LeadStatus = LeadStatus.OPEN
    qualification: LeadQualification = LeadQualification.UNKNOWN
    #: What the prospect asked about. Free-form because the service catalog is
    #: per-tenant; it becomes a foreign key when the catalog lands in Phase 3.
    requested_service: str | None = None
    notes: str | None = None
    source_call_id: CallId | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def qualify(self, qualification: LeadQualification, *, at: datetime | None = None) -> None:
        if self.status in {LeadStatus.CONVERTED, LeadStatus.LOST}:
            raise ConflictError(
                "A closed lead cannot be re-qualified.",
                detail={"status": self.status.value},
            )
        self.qualification = qualification
        self.status = (
            LeadStatus.QUALIFIED
            if qualification is LeadQualification.INTERESTED
            else LeadStatus.OPEN
            if qualification is LeadQualification.FOLLOW_UP
            else LeadStatus.DISQUALIFIED
        )
        self.updated_at = at or now_utc()


@dataclass(slots=True)
class ConsentRecord:
    """Append-only evidence that a person opted in.

    Keeps **both** the peppered hash (for lookup, including lookup without tenant
    context) and the plaintext E.164, because the telephony provider
    contractually requires producing this evidence within 24 hours (HC-14) and a
    hash cannot be shown to a regulator.

    The evidence *payload* shape is deliberately open: open decision **D-3** has
    not settled what artifact counts, how long it is retained, or whether the
    tenant or the platform is liable when a tenant asserts consent it does not
    have. Encoding a legal assumption now would be worse than leaving it open.
    """

    id: ConsentRecordId
    organization_id: OrganizationId
    phone_hash: str
    phone: PhoneNumber
    source: ConsentSource
    status: ConsentStatus = ConsentStatus.GRANTED
    contact_id: ContactId | None = None
    captured_at: datetime = field(default_factory=now_utc)
    #: Whatever the tenant supplied as proof: a form submission id, a recording
    #: reference, an import filename. Shape settles with D-3.
    evidence: dict[str, Any] = field(default_factory=dict)
    captured_by_user_id: UserId | None = None
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not self.phone_hash:
            raise InvariantViolation("A consent record requires a phone hash for lookup.")
        if self.status is ConsentStatus.REVOKED and self.revoked_at is None:
            raise InvariantViolation("A revoked consent record must carry revoked_at.")

    @property
    def is_effective(self) -> bool:
        return self.status is ConsentStatus.GRANTED and self.revoked_at is None

    def revoke(self, *, at: datetime | None = None) -> None:
        """Revoke. The row is never deleted — the evidence trail must survive."""
        self.revoked_at = at or now_utc()
        self.status = ConsentStatus.REVOKED


@dataclass(slots=True)
class Suppression:
    """A blocklist entry. Checked before every single dial.

    **Stores no plaintext number**, only the peppered hash. `organization_id is
    None` means platform-wide — "never call me from this platform again" — which
    a strictly per-tenant design cannot express.

    Rotating the pepper invalidates every entry here. It is effectively part of
    the schema, which is why deployed environments must set it once and keep it.
    """

    id: SuppressionId
    phone_hash: str
    scope: SuppressionScope
    reason: SuppressionReason
    organization_id: OrganizationId | None = None
    source_call_id: CallId | None = None
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not self.phone_hash:
            raise InvariantViolation("A suppression requires a phone hash.")
        if self.scope is SuppressionScope.PLATFORM and self.organization_id is not None:
            raise InvariantViolation(
                "A platform-wide suppression must not be scoped to an organization."
            )
        if self.scope is SuppressionScope.ORGANIZATION and self.organization_id is None:
            raise InvariantViolation(
                "An organization-scoped suppression requires an organization_id."
            )

    def blocks(self, organization_id: OrganizationId) -> bool:
        """Whether this entry blocks dialling for the given organization."""
        if self.scope is SuppressionScope.PLATFORM:
            return True
        return self.organization_id == organization_id
