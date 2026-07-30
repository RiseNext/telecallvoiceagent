"""Unit of Work: transaction boundaries, outbox atomicity, error translation.

The property that matters most: **a state change and its outbox event either
both land or neither does.** That is what makes the voice gateway's
`finalize_call` safe without a broker client, and it is the whole reason the
outbox exists rather than a direct publish (ADR-008).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rn_core.errors import ConflictError, InvariantViolation
from rn_core.ids import new_id
from rn_domain.events import EventType, build_outbox_event
from rn_domain.identifiers import OrganizationId
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_persistence.models import ContactModel, OrganizationModel, OutboxEventModel
from rn_persistence.repositories import ContactRepository, OutboxRepository
from rn_persistence.unit_of_work import UnitOfWork
from tests.integration import factories

pytestmark = [pytest.mark.integration]

Factory = async_sessionmaker[AsyncSession]


async def _seed_org(factory: Factory) -> OrganizationId:
    async with factory() as session:
        org = await factories.create_organization(session)
        await session.commit()
        return OrganizationId(org.id)


async def _count(factory: Factory, model: type) -> int:
    async with factory() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


async def test_commit_persists_state_and_events_together(session_factory: Factory) -> None:
    org_id = await _seed_org(session_factory)
    phone = factories.make_phone("9995551111")

    async with UnitOfWork(session_factory) as uow:
        repo = ContactRepository(uow.session, TenantContext(organization_id=org_id))
        repo.add(
            ContactModel(
                id=new_id(),
                phone_e164=phone.e164,
                phone_hash=phone.hashed(factories.TEST_PEPPER),
                status="active",
                attributes={},
            )
        )
        uow.record_event(
            build_outbox_event(
                event_type=EventType.CALL_COMPLETED,
                payload={"call_id": str(new_id())},
                organization_id=org_id,
            )
        )
        await uow.commit()

    assert await _count(session_factory, ContactModel) == 1
    assert await _count(session_factory, OutboxEventModel) == 1


async def test_rollback_discards_state_and_events_together(session_factory: Factory) -> None:
    """The atomicity guarantee, from the failing side.

    If the state change rolls back but the event survives, a consumer acts on a
    call completion that never happened.
    """
    org_id = await _seed_org(session_factory)
    phone = factories.make_phone("9995552222")

    with pytest.raises(RuntimeError):
        async with UnitOfWork(session_factory) as uow:
            repo = ContactRepository(uow.session, TenantContext(organization_id=org_id))
            repo.add(
                ContactModel(
                    id=new_id(),
                    phone_e164=phone.e164,
                    phone_hash=phone.hashed(factories.TEST_PEPPER),
                    status="active",
                    attributes={},
                )
            )
            uow.record_event(
                build_outbox_event(
                    event_type=EventType.CALL_COMPLETED,
                    payload={"call_id": str(new_id())},
                    organization_id=org_id,
                )
            )
            await uow.flush()
            raise RuntimeError("something failed after the write")

    assert await _count(session_factory, ContactModel) == 0
    assert await _count(session_factory, OutboxEventModel) == 0


async def test_exiting_without_commit_rolls_back(session_factory: Factory) -> None:
    """Committing must be an act, never something a block ending does for you."""
    org_id = await _seed_org(session_factory)
    phone = factories.make_phone("9995553333")

    async with UnitOfWork(session_factory) as uow:
        ContactRepository(uow.session, TenantContext(organization_id=org_id)).add(
            ContactModel(
                id=new_id(),
                phone_e164=phone.e164,
                phone_hash=phone.hashed(factories.TEST_PEPPER),
                status="active",
                attributes={},
            )
        )
        await uow.flush()
        # No commit — an early return in real code looks exactly like this.

    assert await _count(session_factory, ContactModel) == 0


async def test_a_failure_late_in_the_transaction_undoes_earlier_writes(
    session_factory: Factory,
) -> None:
    """Multi-step operations are atomic, which is the point of the UoW."""
    org_id = await _seed_org(session_factory)
    phone = factories.make_phone("9995554444")

    with pytest.raises(ConflictError):
        async with UnitOfWork(session_factory) as uow:
            repo = ContactRepository(uow.session, TenantContext(organization_id=org_id))
            for _ in range(2):
                repo.add(
                    ContactModel(
                        id=new_id(),
                        phone_e164=phone.e164,  # same number twice -> unique violation
                        phone_hash=phone.hashed(factories.TEST_PEPPER),
                        status="active",
                        attributes={},
                    )
                )
            await uow.commit()

    assert await _count(session_factory, ContactModel) == 0


async def test_duplicate_key_becomes_a_typed_conflict(session_factory: Factory) -> None:
    """A driver exception must never escape the persistence layer."""
    org_id = await _seed_org(session_factory)
    phone = factories.make_phone("9995555555")

    async def _insert() -> None:
        async with UnitOfWork(session_factory) as uow:
            ContactRepository(uow.session, TenantContext(organization_id=org_id)).add(
                ContactModel(
                    id=new_id(),
                    phone_e164=phone.e164,
                    phone_hash=phone.hashed(factories.TEST_PEPPER),
                    status="active",
                    attributes={},
                )
            )
            await uow.commit()

    await _insert()
    with pytest.raises(ConflictError) as caught:
        await _insert()
    assert caught.value.code == "conflict"
    # The constraint name is diagnostic context, not something a user sees.
    assert "constraint" in caught.value.detail


async def test_check_violation_becomes_an_invariant_violation(
    session_factory: Factory,
) -> None:
    """A CHECK failure means the application allowed what the schema forbids."""
    org_id = await _seed_org(session_factory)

    with pytest.raises(InvariantViolation):
        async with UnitOfWork(session_factory) as uow:
            uow.session.add(
                ContactModel(
                    id=new_id(),
                    organization_id=org_id,
                    phone_e164="not-e164",  # violates ck_contacts_phone_e164_format
                    phone_hash="h" * 64,
                    status="active",
                    attributes={},
                )
            )
            await uow.commit()


async def test_committing_twice_is_refused(session_factory: Factory) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.commit()
        with pytest.raises(InvariantViolation):
            await uow.commit()


async def test_recording_an_event_after_commit_is_refused(
    session_factory: Factory,
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.commit()
        with pytest.raises(InvariantViolation):
            uow.record_event(build_outbox_event(event_type=EventType.CALL_COMPLETED, payload={}))


async def test_using_the_unit_of_work_outside_its_context_is_refused() -> None:
    uow = UnitOfWork()
    with pytest.raises(InvariantViolation):
        _ = uow.session


async def test_outbox_claim_orders_by_created_at_then_id(
    session_factory: Factory, session: AsyncSession
) -> None:
    """Ordering rests on an explicit timestamp, with the id only as a tiebreak.

    `id` is a UUIDv7 and is time-ordered in practice, but temporal meaning must
    come from a recorded fact rather than from a property of the id generator.
    """
    from rn_core.clock import now_utc

    base = now_utc()
    org_id = await _seed_org(session_factory)

    async with session_factory() as writer:
        for offset in (2, 0, 1):
            writer.add(
                OutboxEventModel(
                    id=new_id(),
                    event_type=EventType.CALL_COMPLETED,
                    payload={},
                    organization_id=org_id,
                    created_at=base.replace(microsecond=0)
                    + __import__("datetime").timedelta(seconds=offset),
                )
            )
        await writer.commit()

    async with session_factory() as reader:
        claimed = await OutboxRepository(reader, PlatformContext()).claim_unpublished(limit=10)
        timestamps = [row.created_at for row in claimed]

    assert timestamps == sorted(timestamps)
    assert len(claimed) == 3


async def test_outbox_starts_unpublished(session_factory: Factory) -> None:
    org_id = await _seed_org(session_factory)
    async with UnitOfWork(session_factory) as uow:
        uow.record_event(
            build_outbox_event(
                event_type=EventType.CALL_COMPLETED,
                payload={"call_id": str(new_id())},
                organization_id=org_id,
            )
        )
        await uow.commit()

    async with session_factory() as reader:
        rows = await OutboxRepository(reader, PlatformContext()).claim_unpublished()
        assert len(rows) == 1
        assert rows[0].published_at is None
        assert rows[0].attempt_count == 0


async def test_platform_events_may_have_no_tenant(session_factory: Factory) -> None:
    async with UnitOfWork(session_factory) as uow:
        uow.record_event(build_outbox_event(event_type=EventType.CAMPAIGN_COMPLETED, payload={}))
        await uow.commit()

    async with session_factory() as reader:
        rows = await OutboxRepository(reader, PlatformContext()).claim_unpublished()
        assert rows[0].organization_id is None


async def test_organization_delete_is_restricted_not_cascaded(
    session_factory: Factory,
) -> None:
    """Removing a tenant must be a deliberate, ordered erasure.

    A cascade here would make one mistaken delete silently remove call history —
    which is also regulated data we may be required to retain.
    """
    org_id = await _seed_org(session_factory)
    async with session_factory() as session:
        await factories.create_contact(session, organization_id=org_id)
        await session.commit()

    with pytest.raises(InvariantViolation):
        async with UnitOfWork(session_factory) as uow:
            org = await uow.session.get(OrganizationModel, org_id)
            assert org is not None
            await uow.session.delete(org)
            await uow.commit()
