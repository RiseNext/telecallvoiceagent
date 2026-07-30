"""Cross-tenant access tests — the security suite.

This is the mandatory one. PRD §13 makes "zero cross-tenant access in an
adversarial test" a V1 success criterion, and these tests are that criterion in
executable form.

The property under test is deliberately narrow and strong: **a caller holding a
`TenantContext` for organization A cannot reach organization B's rows, even when
it knows B's exact primary keys.** Knowing an id is the realistic attacker
position — ids appear in URLs, in exports, in logs — so every test here hands
org A the real id of an org B row and asserts it gets nothing.

Note what is *not* claimed. Phase 1 has **no row-level security**. Isolation here
comes from the repository/context architecture plus the schema's composite
foreign keys. RLS is defence in depth scheduled for Phase 15; these tests would
not detect its absence, and nothing in this file should be read as covering it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rn_core.errors import InvariantViolation, NotFoundError
from rn_domain.identifiers import OrganizationId
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_persistence.models import CallModel, ContactModel
from rn_persistence.repositories import (
    AgentVersionRepository,
    CallRepository,
    CampaignRepository,
    ContactRepository,
    LeadRepository,
)
from tests.integration import factories

pytestmark = [pytest.mark.integration]


class _TwoTenants:
    """Two organizations with an identical set of resources in each."""

    def __init__(self) -> None:
        self.a_id: OrganizationId
        self.b_id: OrganizationId


async def _seed(session: AsyncSession) -> dict[str, object]:
    """Create matching resources under two organizations."""
    org_a = await factories.create_organization(session, name="Tenant A")
    org_b = await factories.create_organization(session, name="Tenant B")

    version_a = await factories.create_agent_version(session, organization_id=org_a.id)
    version_b = await factories.create_agent_version(session, organization_id=org_b.id)

    contact_a = await factories.create_contact(session, organization_id=org_a.id)
    contact_b = await factories.create_contact(session, organization_id=org_b.id)

    campaign_a = await factories.create_campaign(
        session, organization_id=org_a.id, agent_version_id=version_a.id
    )
    campaign_b = await factories.create_campaign(
        session, organization_id=org_b.id, agent_version_id=version_b.id
    )

    call_a = await factories.create_call(
        session,
        organization_id=org_a.id,
        agent_version_id=version_a.id,
        contact_id=contact_a.id,
    )
    call_b = await factories.create_call(
        session,
        organization_id=org_b.id,
        agent_version_id=version_b.id,
        contact_id=contact_b.id,
    )

    lead_a = await factories.create_lead(session, organization_id=org_a.id, contact_id=contact_a.id)
    lead_b = await factories.create_lead(session, organization_id=org_b.id, contact_id=contact_b.id)

    await session.commit()
    return {
        "org_a": org_a,
        "org_b": org_b,
        "version_a": version_a,
        "version_b": version_b,
        "contact_a": contact_a,
        "contact_b": contact_b,
        "campaign_a": campaign_a,
        "campaign_b": campaign_b,
        "call_a": call_a,
        "call_b": call_b,
        "lead_a": lead_a,
        "lead_b": lead_b,
    }


def _ctx(organization_id: object) -> TenantContext:
    return TenantContext(organization_id=OrganizationId(organization_id))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get by id — the attacker knows the exact primary key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repository", "target_key"),
    [
        (ContactRepository, "contact_b"),
        (CallRepository, "call_b"),
        (CampaignRepository, "campaign_b"),
        (LeadRepository, "lead_b"),
        (AgentVersionRepository, "version_b"),
    ],
)
async def test_get_by_known_id_across_tenants_raises_not_found(
    session: AsyncSession, repository: type, target_key: str
) -> None:
    """Org A cannot fetch org B's row even with B's exact id.

    `NotFoundError`, never an authorization error: distinguishing "does not
    exist" from "exists but is not yours" confirms the row exists somewhere,
    which is the fact tenant isolation is meant to hide.
    """
    seeded = await _seed(session)
    repo = repository(session, _ctx(seeded["org_a"].id))  # type: ignore[attr-defined]

    with pytest.raises(NotFoundError):
        await repo.get(seeded[target_key].id)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("repository", "target_key"),
    [
        (ContactRepository, "contact_b"),
        (CallRepository, "call_b"),
        (CampaignRepository, "campaign_b"),
        (LeadRepository, "lead_b"),
    ],
)
async def test_find_by_known_id_across_tenants_returns_none(
    session: AsyncSession, repository: type, target_key: str
) -> None:
    seeded = await _seed(session)
    repo = repository(session, _ctx(seeded["org_a"].id))  # type: ignore[attr-defined]

    assert await repo.find(seeded[target_key].id) is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# list and count — no leakage through collection endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repository",
    [ContactRepository, CallRepository, CampaignRepository, LeadRepository],
)
async def test_list_returns_only_own_tenant(session: AsyncSession, repository: type) -> None:
    seeded = await _seed(session)
    repo_a = repository(session, _ctx(seeded["org_a"].id))  # type: ignore[attr-defined]
    repo_b = repository(session, _ctx(seeded["org_b"].id))  # type: ignore[attr-defined]

    page_a = await repo_a.list()
    page_b = await repo_b.list()

    assert len(page_a) == 1
    assert len(page_b) == 1
    assert {row.organization_id for row in page_a} == {seeded["org_a"].id}  # type: ignore[attr-defined]
    assert {row.organization_id for row in page_b} == {seeded["org_b"].id}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "repository",
    [ContactRepository, CallRepository, CampaignRepository, LeadRepository],
)
async def test_count_is_tenant_scoped(session: AsyncSession, repository: type) -> None:
    seeded = await _seed(session)
    repo_a = repository(session, _ctx(seeded["org_a"].id))  # type: ignore[attr-defined]
    assert await repo_a.count() == 1


# ---------------------------------------------------------------------------
# bespoke query methods — the ones most likely to forget the scope
# ---------------------------------------------------------------------------


async def test_find_by_phone_hash_does_not_cross_tenants(session: AsyncSession) -> None:
    """The same number in two tenants is two contacts, and each sees only its own.

    This is the query used for import dedup, so a leak here would let one tenant
    discover whether another tenant holds a given number.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    shared = factories.make_phone("9995550000")
    contact_a = await factories.create_contact(session, organization_id=org_a.id, phone=shared)
    contact_b = await factories.create_contact(session, organization_id=org_b.id, phone=shared)
    await session.commit()

    repo_a = ContactRepository(session, _ctx(org_a.id))
    found = await repo_a.find_by_phone_hash(shared.hashed(factories.TEST_PEPPER))

    assert found is not None
    assert found.id == contact_a.id
    assert found.id != contact_b.id


async def test_bulk_phone_hash_lookup_does_not_cross_tenants(session: AsyncSession) -> None:
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    shared = factories.make_phone("9995550001")
    await factories.create_contact(session, organization_id=org_b.id, phone=shared)
    await session.commit()

    repo_a = ContactRepository(session, _ctx(org_a.id))
    existing = await repo_a.find_existing_phone_hashes([shared.hashed(factories.TEST_PEPPER)])

    # Org B holds the number; org A must not learn that.
    assert existing == set()


async def test_list_for_contact_does_not_cross_tenants(session: AsyncSession) -> None:
    seeded = await _seed(session)
    repo_a = LeadRepository(session, _ctx(seeded["org_a"].id))  # type: ignore[attr-defined]

    leaked = await repo_a.list_for_contact(seeded["contact_b"].id)  # type: ignore[attr-defined]
    assert leaked == []


async def test_find_by_provider_sid_does_not_cross_tenants(session: AsyncSession) -> None:
    """A provider callback names a call but carries no trustworthy tenant."""
    seeded = await _seed(session)
    call_b: CallModel = seeded["call_b"]  # type: ignore[assignment]
    call_b.provider = "exotel"
    call_b.provider_call_sid = "SID-B-123"
    await session.commit()

    repo_a = CallRepository(session, _ctx(seeded["org_a"].id))  # type: ignore[attr-defined]
    assert await repo_a.find_by_provider_sid("exotel", "SID-B-123") is None


# ---------------------------------------------------------------------------
# writes — a caller cannot plant a row in another tenant
# ---------------------------------------------------------------------------


async def test_add_forces_the_context_tenant(session: AsyncSession) -> None:
    """A model arriving with someone else's tenant is corrected, not trusted."""
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    await session.commit()

    repo_a = ContactRepository(session, _ctx(org_a.id))
    phone = factories.make_phone("9995551234")
    # Deliberately hostile: claims to belong to org B.
    smuggled = ContactModel(
        organization_id=org_b.id,
        phone_e164=phone.e164,
        phone_hash=phone.hashed(factories.TEST_PEPPER),
        status="active",
        attributes={},
    )
    repo_a.add(smuggled)
    await session.commit()

    assert smuggled.organization_id == org_a.id
    repo_b = ContactRepository(session, _ctx(org_b.id))
    assert await repo_b.count() == 0


async def test_delete_across_tenants_raises_not_found(session: AsyncSession) -> None:
    seeded = await _seed(session)
    # Read the ids out before the rollback: a rolled-back session expires its
    # objects, and touching one afterwards would trigger a lazy refresh.
    org_a_id = seeded["org_a"].id  # type: ignore[attr-defined]
    org_b_id = seeded["org_b"].id  # type: ignore[attr-defined]
    contact_b_id = seeded["contact_b"].id  # type: ignore[attr-defined]

    repo_a = ContactRepository(session, _ctx(org_a_id))
    with pytest.raises(NotFoundError):
        await repo_a.delete(contact_b_id)

    await session.rollback()
    repo_b = ContactRepository(session, _ctx(org_b_id))
    assert await repo_b.count() == 1, "org B's contact must survive org A's delete attempt"


# ---------------------------------------------------------------------------
# structural defence — the database refuses cross-tenant parentage
# ---------------------------------------------------------------------------


async def test_composite_foreign_key_rejects_cross_tenant_parent(
    session: AsyncSession,
) -> None:
    """A lead in org A cannot point at a contact in org B.

    This is the defence that survives a bug in the application layer: the
    composite foreign key `(organization_id, contact_id)` has no matching row,
    so Postgres refuses the write regardless of what the code intended.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    contact_b = await factories.create_contact(session, organization_id=org_b.id)
    await session.commit()

    from rn_core.ids import new_id
    from rn_persistence.models import LeadModel

    session.add(
        LeadModel(
            id=new_id(),
            organization_id=org_a.id,
            contact_id=contact_b.id,
            status="open",
            qualification="unknown",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_call_cannot_reference_another_tenants_agent_version(
    session: AsyncSession,
) -> None:
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    contact_a = await factories.create_contact(session, organization_id=org_a.id)
    version_b = await factories.create_agent_version(session, organization_id=org_b.id)
    await session.commit()

    from rn_core.ids import new_id

    session.add(
        CallModel(
            id=new_id(),
            organization_id=org_a.id,
            agent_version_id=version_b.id,
            direction="outbound",
            counterparty_phone_e164=factories.make_phone().e164,
            status="queued",
            contact_id=contact_a.id,
            queued_at=__import__("rn_core.clock", fromlist=["now_utc"]).now_utc(),
            languages=[],
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


# ---------------------------------------------------------------------------
# type-level defence — the two context types are not interchangeable
# ---------------------------------------------------------------------------


async def test_platform_context_cannot_construct_a_tenant_repository(
    session: AsyncSession,
) -> None:
    """Cross-tenant access is a different type, not a boolean flag.

    There is deliberately no `bypass_tenant=True` anywhere; reaching across
    tenants requires a `PlatformContext` and a differently-named repository, both
    of which are greppable in a security review.
    """
    with pytest.raises(InvariantViolation):
        ContactRepository(session, PlatformContext())  # type: ignore[arg-type]
