"""The role-ownership invariant, at every layer that enforces it.

**The rule.** A membership may reference a role only if:

  A) the role is a platform catalog role (`roles.organization_id IS NULL`),
     which is explicitly assignable by any organization; or
  B) the role belongs to the *same* organization as the membership.

Cross-tenant custom-role assignment must never become an authorization path:
organization B must not be able to decide what organization A's members may do.

**Where it is enforced.** Two independent layers, deliberately:

1. **Database (write boundary)** — the `organization_members_role_scope`
   trigger rejects the INSERT/UPDATE outright. This holds for every writer,
   including raw SQL, a psql session and any future service, so corrupt data
   cannot be created in the first place. A companion trigger makes
   `roles.organization_id` immutable, closing the "re-home the role afterwards"
   path that a write-time check alone would miss.

2. **`build_tenant_context` (read boundary)** — refuses to mint a context from a
   cross-tenant role. This is defence in depth for data that predates the
   trigger, or that arrived through a path with the trigger disabled.

The last test proves layer 2 alone is sufficient by disabling layer 1, which is
the property that matters: *authorization integrity must not depend on the
database having been correct.*
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from rn_core.clock import now_utc
from rn_core.errors import AuthorizationError
from rn_core.ids import new_id
from rn_domain.identifiers import OrganizationId, UserId
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_persistence.models import OrganizationMemberModel
from rn_persistence.repositories import RoleRepository, UserRepository
from rn_services.authorization import Principal, build_tenant_context
from tests.integration import factories

pytestmark = [pytest.mark.integration]

_PERMS = frozenset({"org:call:read", "org:contact:read"})


async def _context_for(
    session: AsyncSession, user_id: object, organization_id: object
) -> TenantContext:
    return await build_tenant_context(
        principal=Principal(user_id=UserId(user_id)),  # type: ignore[arg-type]
        organization_id=OrganizationId(organization_id),  # type: ignore[arg-type]
        users=UserRepository(session, PlatformContext()),
        roles=RoleRepository(session, PlatformContext()),
    )


# ---------------------------------------------------------------------------
# Case A and B — the two legitimate assignments
# ---------------------------------------------------------------------------


async def test_org_member_with_own_custom_role_is_allowed(session: AsyncSession) -> None:
    """Case B: a role owned by the same organization."""
    org = await factories.create_organization(session)
    user = await factories.create_user(session)
    own_role = await factories.create_role(session, permissions=_PERMS, organization_id=org.id)
    await factories.create_membership(session, user=user, organization=org, role=own_role)
    await session.commit()

    context = await _context_for(session, user.id, org.id)
    assert context.organization_id == org.id
    assert context.permissions == _PERMS


async def test_org_member_with_platform_catalog_role_is_allowed(
    session: AsyncSession,
) -> None:
    """Case A: a shared platform role, assignable by any organization."""
    org = await factories.create_organization(session)
    user = await factories.create_user(session)
    platform_role = await factories.create_role(session, permissions=_PERMS)
    assert platform_role.organization_id is None
    await factories.create_membership(session, user=user, organization=org, role=platform_role)
    await session.commit()

    context = await _context_for(session, user.id, org.id)
    assert context.permissions == _PERMS


# ---------------------------------------------------------------------------
# The rejected case, at the write boundary
# ---------------------------------------------------------------------------


async def test_assigning_another_orgs_custom_role_is_rejected_by_the_database(
    session: AsyncSession,
) -> None:
    """Layer 1: the corrupt row cannot be written at all."""
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    user = await factories.create_user(session)
    foreign_role = await factories.create_role(
        session, permissions=_PERMS, organization_id=org_b.id
    )
    await session.commit()

    session.add(
        OrganizationMemberModel(
            id=new_id(),
            organization_id=org_a.id,
            user_id=user.id,
            role_id=foreign_role.id,
            created_at=now_utc(),
        )
    )
    with pytest.raises(DBAPIError) as caught:
        await session.commit()
    assert "belongs to organization" in str(caught.value)
    await session.rollback()


async def test_updating_a_membership_to_a_foreign_role_is_rejected(
    session: AsyncSession,
) -> None:
    """The trigger covers UPDATE, not just INSERT.

    A write-time check that only guards creation leaves the obvious second move:
    create a legal membership, then repoint it.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    user = await factories.create_user(session)
    own_role = await factories.create_role(session, permissions=_PERMS, organization_id=org_a.id)
    foreign_role = await factories.create_role(
        session, permissions=_PERMS, organization_id=org_b.id
    )
    membership = await factories.create_membership(
        session, user=user, organization=org_a, role=own_role
    )
    await session.commit()

    membership.role_id = foreign_role.id
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()


async def test_a_role_cannot_be_re_homed_to_another_organization(
    session: AsyncSession,
) -> None:
    """Closes the flank a write-time check alone would leave open.

    Validate the membership at write time, then move the role afterwards, and the
    membership becomes retroactively illegal. `roles.organization_id` is
    therefore immutable.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    user = await factories.create_user(session)
    role = await factories.create_role(session, permissions=_PERMS, organization_id=org_a.id)
    await factories.create_membership(session, user=user, organization=org_a, role=role)
    await session.commit()

    role.organization_id = org_b.id
    with pytest.raises(DBAPIError) as caught:
        await session.commit()
    assert "immutable" in str(caught.value)
    await session.rollback()


async def test_promoting_a_platform_role_to_org_owned_is_also_rejected(
    session: AsyncSession,
) -> None:
    """NULL -> a tenant is a re-home too, and would strand every other tenant's
    membership that legitimately referenced the shared role."""
    org = await factories.create_organization(session)
    role = await factories.create_role(session, permissions=_PERMS)
    await session.commit()

    role.organization_id = org.id
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()


# ---------------------------------------------------------------------------
# Knowing the foreign role's id grants nothing
# ---------------------------------------------------------------------------


async def test_a_known_foreign_role_id_grants_no_permissions(
    session: AsyncSession,
) -> None:
    """The realistic attacker position: org A knows org B's role id exactly.

    Ids leak — through URLs, exports, logs. Knowing one must not be usable.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    user = await factories.create_user(session)
    powerful = await factories.create_role(
        session,
        permissions=frozenset({"org:call:read", "org:call:export", "org:agent:delete"}),
        organization_id=org_b.id,
    )
    minimal = await factories.create_role(
        session, permissions=frozenset({"org:call:read"}), organization_id=org_a.id
    )
    membership = await factories.create_membership(
        session, user=user, organization=org_a, role=minimal
    )
    await session.commit()
    # Read the ids out before the rollback: a rolled-back session expires its
    # objects, and touching one afterwards would trigger a lazy refresh.
    user_id, org_a_id, powerful_id = user.id, org_a.id, powerful.id

    # The membership cannot be repointed at B's powerful role...
    membership.role_id = powerful_id
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()

    # ...and the context still carries only what A's own role grants.
    context = await _context_for(session, user_id, org_a_id)
    assert context.permissions == frozenset({"org:call:read"})
    assert not context.has("org:call:export")
    assert not context.has("org:agent:delete")


# ---------------------------------------------------------------------------
# Layer 2 alone, with the database guard removed
# ---------------------------------------------------------------------------


async def test_corrupt_membership_cannot_produce_an_authorized_context(
    session: AsyncSession,
) -> None:
    """The property that matters: **database correctness is not load-bearing.**

    The trigger is disabled for the duration of one insert, simulating data that
    predates it, arrived through a restore, or was written by a path with
    triggers off (`session_replication_role = replica` during a bulk load is the
    realistic one). `build_tenant_context` must still refuse.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    user = await factories.create_user(session)
    foreign_role = await factories.create_role(
        session, permissions=_PERMS, organization_id=org_b.id
    )
    await session.commit()

    await session.execute(
        text(
            "ALTER TABLE organization_members DISABLE TRIGGER organization_members_role_scope_trigger"
        )
    )
    session.add(
        OrganizationMemberModel(
            id=new_id(),
            organization_id=org_a.id,
            user_id=user.id,
            role_id=foreign_role.id,
            created_at=now_utc(),
        )
    )
    await session.commit()
    await session.execute(
        text(
            "ALTER TABLE organization_members ENABLE TRIGGER organization_members_role_scope_trigger"
        )
    )
    await session.commit()

    # The corrupt row exists. Authorization must still refuse it.
    with pytest.raises(AuthorizationError) as caught:
        await _context_for(session, user.id, org_a.id)
    assert caught.value.detail["reason"] == "cross_tenant_role"


async def test_the_write_guard_is_actually_installed(session: AsyncSession) -> None:
    """Guards the guard: a dropped trigger would make the tests above vacuous."""
    result = await session.execute(
        text(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgname IN ('organization_members_role_scope_trigger', "
            "'roles_owner_immutable_trigger')"
        )
    )
    installed = {row[0] for row in result}
    assert installed == {
        "organization_members_role_scope_trigger",
        "roles_owner_immutable_trigger",
    }
