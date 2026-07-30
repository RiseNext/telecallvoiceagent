"""Concrete repositories.

Each tenant-owned repository is a thin subclass of `TenantScopedRepository`: it
declares its model and adds the queries that table actually needs. The tenant
predicate comes from the base class, so a subclass cannot forget it — provided
every query starts from `self._scoped()`.

Repositories return **ORM models**, not domain entities. Mapping to the domain is
the caller's decision, because a list view wants rows and a state transition
wants an entity, and forcing every read through a mapper would make the cheap
case pay for the expensive one. Every model exposes `to_domain()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rn_core.errors import InvariantViolation
from rn_domain.enums import CampaignContactStatus, ConsentStatus
from rn_domain.tenancy import TenantContext
from rn_persistence.models import (
    AgentModel,
    AgentVersionModel,
    AuditLogModel,
    CallEventModel,
    CallModel,
    CallToolExecutionModel,
    CampaignContactModel,
    CampaignModel,
    ConsentRecordModel,
    ContactModel,
    KnowledgeBaseModel,
    LeadModel,
    OrganizationMemberModel,
    OrganizationModel,
    OutboxEventModel,
    RoleModel,
    SuppressionModel,
    UserModel,
)
from rn_persistence.repositories.base import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    PlatformRepository,
    TenantScopedRepository,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "AgentRepository",
    "AgentVersionRepository",
    "AuditLogRepository",
    "CallEventRepository",
    "CallRepository",
    "CallToolExecutionRepository",
    "CampaignContactRepository",
    "CampaignRepository",
    "ConsentRecordRepository",
    "ContactRepository",
    "KnowledgeBaseRepository",
    "LeadRepository",
    "OrganizationRepository",
    "OutboxRepository",
    "Page",
    "PlatformRepository",
    "RoleRepository",
    "SuppressionRepository",
    "TenantScopedRepository",
    "UserRepository",
]


# ---------------------------------------------------------------------------
# Tenant-scoped
# ---------------------------------------------------------------------------


class AgentRepository(TenantScopedRepository[AgentModel]):
    model = AgentModel

    async def find_by_name(self, name: str) -> AgentModel | None:
        result = await self._session.execute(self._scoped().where(AgentModel.name == name))
        return result.scalar_one_or_none()


class AgentVersionRepository(TenantScopedRepository[AgentVersionModel]):
    model = AgentVersionModel

    async def list_for_agent(self, agent_id: uuid.UUID) -> Sequence[AgentVersionModel]:
        result = await self._session.execute(
            self._scoped()
            .where(AgentVersionModel.agent_id == agent_id)
            .order_by(AgentVersionModel.version_number.desc())
        )
        return list(result.scalars().all())

    async def next_version_number(self, agent_id: uuid.UUID) -> int:
        """The next version number for an agent.

        Racy on its own — two concurrent publishes could read the same value.
        The unique constraint on `(agent_id, version_number)` is what actually
        prevents a duplicate; this just picks the usual next value, and the
        caller retries on the resulting conflict.
        """
        result = await self._session.execute(
            self._scoped()
            .where(AgentVersionModel.agent_id == agent_id)
            .order_by(AgentVersionModel.version_number.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        return 1 if latest is None else latest.version_number + 1


class KnowledgeBaseRepository(TenantScopedRepository[KnowledgeBaseModel]):
    model = KnowledgeBaseModel


class ContactRepository(TenantScopedRepository[ContactModel]):
    model = ContactModel

    async def find_by_phone_hash(self, phone_hash: str) -> ContactModel | None:
        """Import dedup and "do we already know this number" lookups.

        Uses the hash rather than the plaintext so the query, its parameters and
        any slow-query log line stay free of a phone number.
        """
        result = await self._session.execute(
            self._scoped().where(ContactModel.phone_hash == phone_hash)
        )
        return result.scalar_one_or_none()

    async def find_existing_phone_hashes(self, phone_hashes: Sequence[str]) -> set[str]:
        """Which of these numbers this tenant already has.

        Set-based on purpose: a CSV import checking 5,000 contacts one at a time
        is 5,000 round trips, and that is the N+1 that makes imports feel broken.
        """
        if not phone_hashes:
            return set()
        result = await self._session.execute(
            select(ContactModel.phone_hash)
            .where(ContactModel.organization_id == self.organization_id)
            .where(ContactModel.phone_hash.in_(list(phone_hashes)))
        )
        return set(result.scalars().all())


class LeadRepository(TenantScopedRepository[LeadModel]):
    model = LeadModel

    async def list_for_contact(self, contact_id: uuid.UUID) -> Sequence[LeadModel]:
        result = await self._session.execute(
            self._scoped()
            .where(LeadModel.contact_id == contact_id)
            .order_by(LeadModel.created_at.desc())
        )
        return list(result.scalars().all())


class ConsentRecordRepository(TenantScopedRepository[ConsentRecordModel]):
    model = ConsentRecordModel

    async def find_effective(self, phone_hash: str) -> ConsentRecordModel | None:
        """The most recent unrevoked consent this tenant holds for a number."""
        result = await self._session.execute(
            self._scoped()
            .where(ConsentRecordModel.phone_hash == phone_hash)
            .where(ConsentRecordModel.status == ConsentStatus.GRANTED.value)
            .where(ConsentRecordModel.revoked_at.is_(None))
            .order_by(ConsentRecordModel.captured_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class CampaignRepository(TenantScopedRepository[CampaignModel]):
    model = CampaignModel


class CampaignContactRepository(TenantScopedRepository[CampaignContactModel]):
    model = CampaignContactModel

    async def list_due(
        self, campaign_id: uuid.UUID, *, now: datetime, limit: int = DEFAULT_PAGE_SIZE
    ) -> Sequence[CampaignContactModel]:
        """Contacts in this campaign that are due to be dialled.

        Matches the partial index `ix_campaign_contacts_due`. Bounded, because
        the dispatcher must take a batch it can actually issue within its
        concurrency budget — never the whole campaign.
        """
        effective = max(1, min(limit, MAX_PAGE_SIZE))
        result = await self._session.execute(
            self._scoped()
            .where(CampaignContactModel.campaign_id == campaign_id)
            .where(
                CampaignContactModel.status.in_(
                    [CampaignContactStatus.PENDING.value, CampaignContactStatus.ELIGIBLE.value]
                )
            )
            .where(
                (CampaignContactModel.next_attempt_at.is_(None))
                | (CampaignContactModel.next_attempt_at <= now)
            )
            .order_by(CampaignContactModel.created_at)
            .limit(effective)
        )
        return list(result.scalars().all())


class CallRepository(TenantScopedRepository[CallModel]):
    model = CallModel

    async def find_by_provider_sid(self, provider: str, sid: str) -> CallModel | None:
        """Resolve a provider callback to a call.

        Tenant-scoped like everything else. A provider callback names a call but
        carries no trustworthy tenant, so the caller resolves the tenant first
        and then looks the call up inside it.
        """
        result = await self._session.execute(
            self._scoped()
            .where(CallModel.provider == provider)
            .where(CallModel.provider_call_sid == sid)
        )
        return result.scalar_one_or_none()

    async def list_recent(
        self, *, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE
    ) -> Page[CallModel]:
        """The dashboard call list. Matches `ix_calls_organization_id_queued_at`."""
        return await self.list(offset=offset, limit=limit, order_by=CallModel.queued_at.desc())


class CallEventRepository(TenantScopedRepository[CallEventModel]):
    model = CallEventModel

    async def list_for_call(self, call_id: uuid.UUID) -> Sequence[CallEventModel]:
        result = await self._session.execute(
            self._scoped()
            .where(CallEventModel.call_id == call_id)
            .order_by(CallEventModel.occurred_at)
        )
        return list(result.scalars().all())


class CallToolExecutionRepository(TenantScopedRepository[CallToolExecutionModel]):
    model = CallToolExecutionModel

    async def list_for_call(self, call_id: uuid.UUID) -> Sequence[CallToolExecutionModel]:
        result = await self._session.execute(
            self._scoped()
            .where(CallToolExecutionModel.call_id == call_id)
            .order_by(CallToolExecutionModel.started_at)
        )
        return list(result.scalars().all())


class AuditLogRepository:
    """Tenant-scoped reads and appends over the audit trail.

    Deliberately **not** a `TenantScopedRepository`. `audit_logs.organization_id`
    is nullable, because a platform-level action genuinely has no tenant, so the
    table is not `TenantOwnedBase` and the generic base would not accept it.

    That nullability is exactly why this class is written out rather than
    inherited: a tenant reading its audit trail must never see the platform rows
    where `organization_id IS NULL`, and an equality predicate against a nullable
    column is the kind of thing that silently does the right thing until someone
    "simplifies" it. Here the intent is on the page.

    Append-only: there is no update and no delete, by design.
    """

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        if not isinstance(context, TenantContext):
            raise InvariantViolation(
                "A tenant-scoped repository requires a TenantContext.",
                detail={"received": type(context).__name__},
            )
        self._session = session
        self._context = context

    async def list_recent(self, *, limit: int = DEFAULT_PAGE_SIZE) -> Sequence[AuditLogModel]:
        effective = max(1, min(limit, MAX_PAGE_SIZE))
        result = await self._session.execute(
            select(AuditLogModel)
            .where(AuditLogModel.organization_id == self._context.organization_id)
            .order_by(AuditLogModel.created_at.desc())
            .limit(effective)
        )
        return list(result.scalars().all())

    def record(self, entry: AuditLogModel) -> AuditLogModel:
        """Append an entry, forcing it into this tenant."""
        entry.organization_id = self._context.organization_id
        self._session.add(entry)
        return entry


# ---------------------------------------------------------------------------
# Platform-global. Every class below crosses or ignores the tenant boundary.
# ---------------------------------------------------------------------------


class OrganizationRepository(PlatformRepository[OrganizationModel]):
    """Organizations are the tenants; the table itself is platform-global."""

    model = OrganizationModel

    async def find_by_slug(self, slug: str) -> OrganizationModel | None:
        result = await self._session.execute(
            select(OrganizationModel).where(OrganizationModel.slug == slug)
        )
        return result.scalar_one_or_none()

    async def find_by_external_auth_id(self, external_id: str) -> OrganizationModel | None:
        result = await self._session.execute(
            select(OrganizationModel).where(OrganizationModel.external_auth_id == external_id)
        )
        return result.scalar_one_or_none()


class UserRepository(PlatformRepository[UserModel]):
    """Users are platform-global: one person may belong to several organizations."""

    model = UserModel

    async def find_by_email(self, email: str) -> UserModel | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.email == email.strip().lower())
        )
        return result.scalar_one_or_none()

    async def list_memberships(self, user_id: uuid.UUID) -> Sequence[OrganizationMemberModel]:
        """Which organizations this user belongs to, and with which role.

        This is what turns an authenticated identity into a set of tenants the
        subject may act in. Membership *is* the grant: a subject with no active
        row here has no access, whatever an external token claims.
        """
        result = await self._session.execute(
            select(OrganizationMemberModel)
            .where(OrganizationMemberModel.user_id == user_id)
            .where(OrganizationMemberModel.deleted_at.is_(None))
        )
        return list(result.scalars().all())


class RoleRepository(PlatformRepository[RoleModel]):
    """Roles: platform catalog rows plus tenants' own custom roles."""

    model = RoleModel

    async def find_platform_role(self, key: str) -> RoleModel | None:
        result = await self._session.execute(
            select(RoleModel).where(RoleModel.key == key).where(RoleModel.organization_id.is_(None))
        )
        return result.scalar_one_or_none()


class SuppressionRepository(PlatformRepository[SuppressionModel]):
    """The blocklist. Platform-scoped **because a suppression may be**.

    `organization_id IS NULL` means "never call me from this platform again",
    which cannot be expressed by a tenant-scoped repository. The pre-dial gate
    therefore asks this one question across both scopes at once.
    """

    model = SuppressionModel

    async def is_suppressed(self, phone_hash: str, organization_id: uuid.UUID) -> bool:
        """Whether this number is blocked for this organization.

        One indexed lookup covering the platform-wide and org-scoped rows
        together — this runs before every single dial, so it must not be two
        round trips.
        """
        result = await self._session.execute(
            select(SuppressionModel.id)
            .where(SuppressionModel.phone_hash == phone_hash)
            .where(
                (SuppressionModel.organization_id.is_(None))
                | (SuppressionModel.organization_id == organization_id)
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


class OutboxRepository(PlatformRepository[OutboxEventModel]):
    """The transactional outbox.

    Platform-scoped because the relay is a platform process that publishes every
    tenant's events, and because a platform-level event has no tenant at all.
    Writing a row, by contrast, happens inside whatever transaction the Unit of
    Work is already running — see `UnitOfWork.record_event`.
    """

    model = OutboxEventModel

    async def claim_unpublished(self, *, limit: int = 100) -> Sequence[OutboxEventModel]:
        """Claim a batch of unpublished events for delivery.

        Ordered by `(created_at, id)`: an explicit timestamp carries the temporal
        meaning and the UUIDv7 id is only a deterministic tiebreak. Ordering on
        the id alone would make delivery order depend on a property of the id
        generator rather than on a recorded fact.

        `FOR UPDATE SKIP LOCKED` lets several relay workers make progress at
        once, which means there is **no global ordering guarantee** — ordering
        holds per aggregate, and consumers must be idempotent regardless.

        The relay itself is Phase 7; this exists so the schema and the claim
        query are proven together now.
        """
        effective = max(1, min(limit, MAX_PAGE_SIZE))
        result = await self._session.execute(
            select(OutboxEventModel)
            .where(OutboxEventModel.published_at.is_(None))
            .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
            .limit(effective)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())
