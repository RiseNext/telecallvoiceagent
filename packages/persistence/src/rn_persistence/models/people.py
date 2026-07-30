"""Contact, lead and compliance tables.

Note the deliberate asymmetry between the two compliance tables:

* `consent_records` keeps **both** the peppered hash and the plaintext E.164,
  because opt-in evidence must be producible to a regulator within 24 hours
  (HC-14) and a hash cannot be shown to anyone.
* `suppressions` keeps **only** the hash. A blocklist should not also be a
  phone-number database, and this one is queried before every single dial.

`suppressions.organization_id` is nullable: `NULL` means platform-wide, which a
strictly per-tenant design cannot express.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from rn_domain.entities.people import ConsentRecord, Contact, Lead, Suppression
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
from rn_persistence.base import (
    Base,
    TenantOwnedBase,
    created_at_column,
    enum_check,
    json_column,
    nullable_organization_fk,
)

__all__ = ["ConsentRecordModel", "ContactModel", "LeadModel", "SuppressionModel"]

#: Hex SHA-256.
_HASH_LENGTH = 64


class ContactModel(TenantOwnedBase):
    """A person an organization may dial."""

    __tablename__ = "contacts"

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attributes: Mapped[dict[str, Any]] = json_column()
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_contacts_organization_id_id"),
        # The dedup key. The same number in two tenants is two contacts: consent
        # and suppression are asserted per tenant and must not leak between them.
        UniqueConstraint(
            "organization_id", "phone_e164", name="uq_contacts_organization_id_phone_e164"
        ),
        enum_check("status", ContactStatus, "status"),
        CheckConstraint("phone_e164 LIKE '+%'", name="phone_e164_format"),
        # Import dedup and the "is this number already here" lookup.
        Index("ix_contacts_organization_id_phone_hash", "organization_id", "phone_hash"),
        # The dashboard list.
        Index("ix_contacts_organization_id_created_at", "organization_id", "created_at"),
    )

    def to_domain(self) -> Contact:
        return Contact(
            id=ContactId(self.id),
            organization_id=OrganizationId(self.organization_id),
            phone=PhoneNumber(self.phone_e164),
            full_name=self.full_name,
            email=self.email,
            status=ContactStatus(self.status),
            attributes=dict(self.attributes),
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, entity: Contact, *, phone_hash: str) -> ContactModel:
        """Build a row.

        `phone_hash` is passed in rather than computed here: hashing needs the
        pepper, which is configuration, and persistence must not read settings.
        """
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            phone_e164=entity.phone.e164,
            phone_hash=phone_hash,
            full_name=entity.full_name,
            email=entity.email,
            status=entity.status.value,
            attributes=dict(entity.attributes),
            created_at=entity.created_at,
            deleted_at=entity.deleted_at,
        )

    def apply(self, entity: Contact) -> None:
        self.full_name = entity.full_name
        self.email = entity.email
        self.status = entity.status.value
        self.attributes = dict(entity.attributes)
        self.deleted_at = entity.deleted_at


class LeadModel(TenantOwnedBase):
    """A commercial opportunity discovered in a conversation."""

    __tablename__ = "leads"

    contact_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    qualification: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_service: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "contact_id"],
            ["contacts.organization_id", "contacts.id"],
            name="fk_leads_organization_id_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        # The call this lead came out of. Composite, so a lead can never cite
        # another tenant's call as its source. Nullable because a lead can be
        # created outside a call (an import, a manual entry); with MATCH SIMPLE
        # a NULL `source_call_id` skips the constraint, which is the behaviour
        # we want.
        ForeignKeyConstraint(
            ["organization_id", "source_call_id"],
            ["calls.organization_id", "calls.id"],
            name="fk_leads_organization_id_source_call_id_calls",
            ondelete="SET NULL",
        ),
        UniqueConstraint("organization_id", "id", name="uq_leads_organization_id_id"),
        enum_check("status", LeadStatus, "status"),
        enum_check("qualification", LeadQualification, "qualification"),
        # The pipeline view: one org's leads filtered by status, newest first.
        Index(
            "ix_leads_organization_id_status_created_at",
            "organization_id",
            "status",
            "created_at",
        ),
        Index("ix_leads_contact_id", "contact_id"),
    )

    def to_domain(self) -> Lead:
        return Lead(
            id=LeadId(self.id),
            organization_id=OrganizationId(self.organization_id),
            contact_id=ContactId(self.contact_id),
            status=LeadStatus(self.status),
            qualification=LeadQualification(self.qualification),
            requested_service=self.requested_service,
            notes=self.notes,
            source_call_id=CallId(self.source_call_id) if self.source_call_id else None,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, entity: Lead) -> LeadModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            contact_id=entity.contact_id,
            status=entity.status.value,
            qualification=entity.qualification.value,
            requested_service=entity.requested_service,
            notes=entity.notes,
            source_call_id=entity.source_call_id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    def apply(self, entity: Lead) -> None:
        self.status = entity.status.value
        self.qualification = entity.qualification.value
        self.requested_service = entity.requested_service
        self.notes = entity.notes
        self.updated_at = entity.updated_at


class ConsentRecordModel(TenantOwnedBase):
    """Append-only opt-in evidence."""

    __tablename__ = "consent_records"

    phone_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)
    # Plaintext on purpose: evidence must be producible (HC-14). Protected by
    # storage-level encryption, not application-level column encryption, which
    # would break the exact-match lookups this table exists for.
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    captured_at: Mapped[datetime] = mapped_column(nullable=False)
    evidence: Mapped[dict[str, Any]] = json_column()
    captured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "contact_id"],
            ["contacts.organization_id", "contacts.id"],
            name="fk_consent_records_organization_id_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        enum_check("source", ConsentSource, "source"),
        enum_check("status", ConsentStatus, "status"),
        CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="revoked_at",
        ),
        # The tenant-scoped consent check, newest first.
        Index(
            "ix_consent_records_organization_id_phone_hash",
            "organization_id",
            "phone_hash",
            "captured_at",
        ),
        # The 24-hour evidence-production path. Deliberately hash-first and
        # tenant-less: a regulator asks about a *number*, and we may not know
        # which tenant asserted the consent when the question arrives.
        Index("ix_consent_records_phone_hash_captured_at", "phone_hash", "captured_at"),
    )

    def to_domain(self) -> ConsentRecord:
        return ConsentRecord(
            id=ConsentRecordId(self.id),
            organization_id=OrganizationId(self.organization_id),
            phone_hash=self.phone_hash,
            phone=PhoneNumber(self.phone_e164),
            source=ConsentSource(self.source),
            status=ConsentStatus(self.status),
            contact_id=ContactId(self.contact_id) if self.contact_id else None,
            captured_at=self.captured_at,
            evidence=dict(self.evidence),
            captured_by_user_id=(
                UserId(self.captured_by_user_id) if self.captured_by_user_id else None
            ),
            revoked_at=self.revoked_at,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: ConsentRecord) -> ConsentRecordModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            phone_hash=entity.phone_hash,
            phone_e164=entity.phone.e164,
            source=entity.source.value,
            status=entity.status.value,
            contact_id=entity.contact_id,
            captured_at=entity.captured_at,
            evidence=dict(entity.evidence),
            captured_by_user_id=entity.captured_by_user_id,
            revoked_at=entity.revoked_at,
            created_at=entity.created_at,
        )

    def apply(self, entity: ConsentRecord) -> None:
        """Only revocation may change. The evidence itself is append-only."""
        self.status = entity.status.value
        self.revoked_at = entity.revoked_at


class SuppressionModel(Base):
    """The blocklist. Hash only — no plaintext number is stored."""

    __tablename__ = "suppressions"

    phone_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = nullable_organization_fk()
    # DELIBERATELY not a foreign key, composite or otherwise.
    #
    # `suppressions.organization_id` is nullable (NULL = platform-wide), so a
    # composite FK `(organization_id, source_call_id) -> calls` would be skipped
    # under MATCH SIMPLE for exactly the platform-wide rows — the ones that
    # matter most — and MATCH FULL would forbid them entirely. A plain FK to
    # `calls(id)` would enforce existence but not tenancy, which is the wrong
    # half of the guarantee.
    #
    # More importantly, a suppression must OUTLIVE the call that produced it: if
    # someone says "never call me again" and the call record is later erased
    # under retention, the block must remain. Any FK here would either cascade
    # the block away or block the erasure. This is provenance, not a relationship.
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        enum_check("scope", SuppressionScope, "scope"),
        enum_check("reason", SuppressionReason, "reason"),
        # Scope and organization_id must agree. Without this a "platform-wide"
        # row scoped to one tenant would silently fail to block anyone else.
        CheckConstraint(
            "(scope = 'platform' AND organization_id IS NULL) "
            "OR (scope = 'organization' AND organization_id IS NOT NULL)",
            name="scope_organization",
        ),
        # THE pre-dial hot path. Covers both the org-scoped and platform-wide
        # lookup in one index, because the dial gate asks for both at once.
        Index("ix_suppressions_phone_hash_organization_id", "phone_hash", "organization_id"),
    )

    def to_domain(self) -> Suppression:
        return Suppression(
            id=SuppressionId(self.id),
            phone_hash=self.phone_hash,
            scope=SuppressionScope(self.scope),
            reason=SuppressionReason(self.reason),
            organization_id=(
                OrganizationId(self.organization_id) if self.organization_id else None
            ),
            source_call_id=CallId(self.source_call_id) if self.source_call_id else None,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: Suppression) -> SuppressionModel:
        return cls(
            id=entity.id,
            phone_hash=entity.phone_hash,
            scope=entity.scope.value,
            reason=entity.reason.value,
            organization_id=entity.organization_id,
            source_call_id=entity.source_call_id,
            created_at=entity.created_at,
        )
