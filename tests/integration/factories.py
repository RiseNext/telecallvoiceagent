"""Test data builders.

Synthetic only. **No real customer data, ever** — the phone numbers here are
from India's `+91 999xxxxxxx` reserved-for-fiction style range and are never
dialled by anything in this suite, because nothing in Phase 1 can dial.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from rn_core.clock import now_utc
from rn_core.ids import new_id
from rn_domain.enums import (
    AgentStatus,
    AgentVersionStatus,
    CallDirection,
    CallStatus,
    CampaignContactStatus,
    CampaignStatus,
    ContactStatus,
    LeadQualification,
    LeadStatus,
    OrganizationStatus,
)
from rn_domain.values import PhoneNumber
from rn_persistence.models import (
    AgentModel,
    AgentVersionModel,
    CallModel,
    CampaignContactModel,
    CampaignModel,
    ContactModel,
    LeadModel,
    OrganizationMemberModel,
    OrganizationModel,
    RoleModel,
    UserModel,
)

TEST_PEPPER = "test-pepper-not-a-real-secret"

_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter}-{uuid.uuid4().hex[:8]}"


def make_phone(national: str = "9990000001") -> PhoneNumber:
    return PhoneNumber(f"+91{national}")


async def create_organization(
    session: AsyncSession, *, name: str | None = None, status: OrganizationStatus | None = None
) -> OrganizationModel:
    org = OrganizationModel(
        id=new_id(),
        name=name or _unique("Org"),
        slug=_unique("org"),
        status=(status or OrganizationStatus.ACTIVE).value,
        timezone="Asia/Kolkata",
        created_at=now_utc(),
    )
    session.add(org)
    await session.flush()
    return org


async def create_user(session: AsyncSession) -> UserModel:
    user = UserModel(id=new_id(), email=f"{_unique('user')}@example.test", created_at=now_utc())
    session.add(user)
    await session.flush()
    return user


async def create_role(
    session: AsyncSession,
    *,
    permissions: frozenset[str],
    organization_id: uuid.UUID | None = None,
) -> RoleModel:
    role = RoleModel(
        id=new_id(),
        key=_unique("role"),
        name="Test Role",
        permissions=sorted(permissions),
        organization_id=organization_id,
        created_at=now_utc(),
    )
    session.add(role)
    await session.flush()
    return role


async def create_membership(
    session: AsyncSession,
    *,
    user: UserModel,
    organization: OrganizationModel,
    role: RoleModel,
) -> OrganizationMemberModel:
    membership = OrganizationMemberModel(
        id=new_id(),
        organization_id=organization.id,
        user_id=user.id,
        role_id=role.id,
        created_at=now_utc(),
    )
    session.add(membership)
    await session.flush()
    return membership


async def create_contact(
    session: AsyncSession, *, organization_id: uuid.UUID, phone: PhoneNumber | None = None
) -> ContactModel:
    number = phone or make_phone(f"999{_counter:07d}"[:10])
    contact = ContactModel(
        id=new_id(),
        organization_id=organization_id,
        phone_e164=number.e164,
        phone_hash=number.hashed(TEST_PEPPER),
        full_name="Test Person",
        status=ContactStatus.ACTIVE.value,
        attributes={},
        created_at=now_utc(),
    )
    session.add(contact)
    await session.flush()
    return contact


async def create_agent_version(
    session: AsyncSession, *, organization_id: uuid.UUID, published: bool = True
) -> AgentVersionModel:
    agent = AgentModel(
        id=new_id(),
        organization_id=organization_id,
        name=_unique("Agent"),
        status=AgentStatus.ACTIVE.value,
        created_at=now_utc(),
    )
    session.add(agent)
    await session.flush()

    version = AgentVersionModel(
        id=new_id(),
        organization_id=organization_id,
        agent_id=agent.id,
        version_number=1,
        instructions="You are a helpful assistant for integration tests.",
        languages=["en", "hi-IN"],
        status=(AgentVersionStatus.PUBLISHED if published else AgentVersionStatus.DRAFT).value,
        voice_map={},
        turn_policy={},
        guardrail_config={},
        published_at=now_utc() if published else None,
        created_at=now_utc(),
    )
    session.add(version)
    await session.flush()

    agent.current_version_id = version.id
    await session.flush()
    return version


async def create_campaign(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    status: CampaignStatus = CampaignStatus.DRAFT,
) -> CampaignModel:
    campaign = CampaignModel(
        id=new_id(),
        organization_id=organization_id,
        name=_unique("Campaign"),
        agent_version_id=agent_version_id,
        status=status.value,
        max_concurrent_calls=1,
        max_attempts_per_contact=2,
        created_at=now_utc(),
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def create_campaign_contact(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    status: CampaignContactStatus = CampaignContactStatus.PENDING,
) -> CampaignContactModel:
    row = CampaignContactModel(
        id=new_id(),
        organization_id=organization_id,
        campaign_id=campaign_id,
        contact_id=contact_id,
        status=status.value,
        attempt_count=0,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def create_call(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    contact_id: uuid.UUID,
    status: CallStatus = CallStatus.QUEUED,
) -> CallModel:
    call = CallModel(
        id=new_id(),
        organization_id=organization_id,
        agent_version_id=agent_version_id,
        direction=CallDirection.OUTBOUND.value,
        counterparty_phone_e164=make_phone().e164,
        status=status.value,
        contact_id=contact_id,
        queued_at=now_utc(),
        languages=[],
        created_at=now_utc(),
    )
    session.add(call)
    await session.flush()
    return call


async def create_lead(
    session: AsyncSession, *, organization_id: uuid.UUID, contact_id: uuid.UUID
) -> LeadModel:
    lead = LeadModel(
        id=new_id(),
        organization_id=organization_id,
        contact_id=contact_id,
        status=LeadStatus.OPEN.value,
        qualification=LeadQualification.UNKNOWN.value,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(lead)
    await session.flush()
    return lead
