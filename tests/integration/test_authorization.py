"""Authorization: deriving authority from verified identity plus membership.

The property under test is the one that makes the whole tenancy story work:
**the requested `organization_id` is checked against a membership row, never
trusted.** A caller asks to act in an organization; it does not get to assert
that it may.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from rn_core.errors import AuthorizationError
from rn_domain.identifiers import OrganizationId, UserId
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_persistence.repositories import RoleRepository, UserRepository
from rn_services.authorization import (
    Principal,
    accessible_organization_ids,
    build_platform_context,
    build_tenant_context,
)
from tests.integration import factories

pytestmark = [pytest.mark.integration]

_READER = frozenset({"org:call:read", "org:contact:read"})


async def _context_for(
    session: AsyncSession, principal: Principal, organization_id: OrganizationId
) -> TenantContext:
    return await build_tenant_context(
        principal=principal,
        organization_id=organization_id,
        users=UserRepository(session, PlatformContext()),
        roles=RoleRepository(session, PlatformContext()),
    )


async def test_membership_grants_exactly_the_role_permissions(
    session: AsyncSession,
) -> None:
    org = await factories.create_organization(session)
    user = await factories.create_user(session)
    role = await factories.create_role(session, permissions=_READER)
    await factories.create_membership(session, user=user, organization=org, role=role)
    await session.commit()

    context = await _context_for(
        session, Principal(user_id=UserId(user.id)), OrganizationId(org.id)
    )

    assert context.organization_id == org.id
    assert context.actor_id == user.id
    assert context.permissions == _READER
    assert context.has("org:call:read")
    assert not context.has("org:call:export")


async def test_requesting_an_organization_you_do_not_belong_to_is_refused(
    session: AsyncSession,
) -> None:
    """The core check. Knowing the organization's id is not authority to enter it."""
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    user = await factories.create_user(session)
    role = await factories.create_role(session, permissions=_READER)
    await factories.create_membership(session, user=user, organization=org_a, role=role)
    await session.commit()

    with pytest.raises(AuthorizationError):
        await _context_for(session, Principal(user_id=UserId(user.id)), OrganizationId(org_b.id))


async def test_removed_membership_revokes_access(session: AsyncSession) -> None:
    """A soft-deleted membership is not a membership."""
    from rn_core.clock import now_utc

    org = await factories.create_organization(session)
    user = await factories.create_user(session)
    role = await factories.create_role(session, permissions=_READER)
    membership = await factories.create_membership(session, user=user, organization=org, role=role)
    await session.commit()

    membership.deleted_at = now_utc()
    await session.commit()

    with pytest.raises(AuthorizationError):
        await _context_for(session, Principal(user_id=UserId(user.id)), OrganizationId(org.id))


async def test_anonymous_principal_is_refused(session: AsyncSession) -> None:
    org = await factories.create_organization(session)
    await session.commit()

    with pytest.raises(AuthorizationError):
        await _context_for(session, Principal(), OrganizationId(org.id))


async def test_denial_messages_do_not_confirm_the_organization_exists(
    session: AsyncSession,
) -> None:
    """A different message for "exists but not yours" would be free reconnaissance."""
    from rn_core.ids import new_id

    org = await factories.create_organization(session)
    user = await factories.create_user(session)
    role = await factories.create_role(session, permissions=_READER)
    await factories.create_membership(session, user=user, organization=org, role=role)
    await session.commit()

    principal = Principal(user_id=UserId(user.id))
    other = await factories.create_organization(session)
    await session.commit()

    with pytest.raises(AuthorizationError) as real_org:
        await _context_for(session, principal, OrganizationId(other.id))
    with pytest.raises(AuthorizationError) as fake_org:
        await _context_for(session, principal, OrganizationId(new_id()))

    assert real_org.value.message == fake_org.value.message


async def test_platform_permissions_on_a_tenant_role_are_stripped(
    session: AsyncSession,
) -> None:
    """Defence in depth against a row written before the domain rule existed.

    The domain refuses to construct such a role, and `TenantContext` refuses to
    carry a platform permission — but a legacy or hand-edited row must degrade to
    "fewer permissions", not to a cross-tenant grant.
    """
    org = await factories.create_organization(session)
    user = await factories.create_user(session)
    # Written straight to the table, bypassing the domain entity.
    role = await factories.create_role(
        session, permissions=frozenset({"org:call:read", "platform:call:read"})
    )
    await factories.create_membership(session, user=user, organization=org, role=role)
    await session.commit()

    context = await _context_for(
        session, Principal(user_id=UserId(user.id)), OrganizationId(org.id)
    )
    assert context.permissions == frozenset({"org:call:read"})
    assert not context.has("platform:call:read")


# The cross-tenant role invariant lives in `test_role_ownership.py`.
#
# It started here, asserting that `build_tenant_context` refuses a membership
# pointing at another tenant's role. That check still exists and still matters —
# but enforcement has since moved earlier: a database trigger now rejects the
# corrupt row at write time, so this test could no longer *create* the state it
# was asserting about. Rather than weaken it to accommodate the stronger guard,
# the whole invariant moved to its own suite: both layers, plus the case where
# the trigger is disabled and only the read boundary is left.


async def test_platform_context_requires_the_staff_flag() -> None:
    with pytest.raises(AuthorizationError):
        build_platform_context(Principal(user_id=UserId(factories.new_id())))  # type: ignore[attr-defined]


async def test_platform_context_carries_its_permissions() -> None:
    from rn_core.ids import new_id

    context = build_platform_context(
        Principal(
            user_id=UserId(new_id()),
            is_platform_staff=True,
            platform_permissions=frozenset({"platform:call:read"}),
        )
    )
    assert isinstance(context, PlatformContext)
    assert context.has("platform:call:read")


async def test_accessible_organizations_lists_only_active_memberships(
    session: AsyncSession,
) -> None:
    from rn_core.clock import now_utc

    user = await factories.create_user(session)
    role = await factories.create_role(session, permissions=_READER)
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    await factories.create_membership(session, user=user, organization=org_a, role=role)
    removed = await factories.create_membership(session, user=user, organization=org_b, role=role)
    removed.deleted_at = now_utc()
    await session.commit()

    memberships = await UserRepository(session, PlatformContext()).list_memberships(user.id)
    assert accessible_organization_ids(memberships) == [org_a.id]
