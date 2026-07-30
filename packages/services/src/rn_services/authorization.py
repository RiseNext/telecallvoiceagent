"""The authorization seam.

**Authentication and authorization are separate concerns and separate types.**

* *Authentication* answers "who is this?" and produces a `Principal`. That is
  someone else's job — a token verifier, an identity provider, a test fixture.
  There is no identity vendor anywhere in this module, and there will not be
  one: Clerk arrives in a later phase behind `rn_providers.IdentityProvider`,
  and when it does, nothing here changes.
* *Authorization* answers "what may they do?" and produces a `TenantContext` or
  a `PlatformContext`, carrying an explicit permission set.

The step in the middle is the one that matters for tenant isolation:

    Principal + requested organization_id
        -> look up an ACTIVE membership in the database
        -> load that membership's role
        -> TenantContext(organization_id, permissions)

A caller asks to act in an organization; it does not get to *assert* that it
may. If there is no active membership row, the request fails — whatever the
token, request body or tool argument claimed. This is the server-side derivation
that CLAUDE.md rules 3 and 4 require, and it is the only sanctioned way to build
a `TenantContext` from an untrusted request.

Everything here is testable without HTTP, because none of it knows what HTTP is.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from rn_core.errors import AuthorizationError
from rn_core.logging import get_logger
from rn_domain.identifiers import OrganizationId, UserId
from rn_domain.permissions import Permission, is_platform_permission
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_persistence.models import OrganizationMemberModel
from rn_persistence.repositories import RoleRepository, UserRepository

__all__ = [
    "Principal",
    "accessible_organization_ids",
    "build_platform_context",
    "build_tenant_context",
]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Principal:
    """The result of authentication. Deliberately provider-independent.

    Carries identity and nothing else — no permissions, no organization. A
    `Principal` is a claim about *who*, never about *what they may do*; keeping
    those apart is what stops an identity provider's opinion about roles
    becoming our authorization decision.

    `is_platform_staff` is the one privileged bit, and it is set by the
    authentication layer from a source we control, never from a self-asserted
    token claim.
    """

    user_id: UserId | None = None
    external_auth_id: str | None = None
    is_platform_staff: bool = False
    request_id: str | None = None
    #: Permissions granted to platform staff. Empty for ordinary users; a
    #: non-empty value on a non-staff principal is rejected when building a
    #: platform context.
    platform_permissions: frozenset[Permission] = field(default_factory=frozenset)

    @property
    def is_anonymous(self) -> bool:
        return self.user_id is None and self.external_auth_id is None


async def build_tenant_context(
    *,
    principal: Principal,
    organization_id: OrganizationId,
    users: UserRepository,
    roles: RoleRepository,
) -> TenantContext:
    """Derive authority to act inside one organization.

    The requested `organization_id` is **verified against membership**, not
    trusted. This is the choke point: everything downstream receives a
    `TenantContext` whose organization has already been proven, so no repository
    or service has to re-check it.

    Raises:
        AuthorizationError: if the principal is anonymous, or has no active
            membership of that organization. Both produce the same generic
            message — telling a caller "that organization exists but you are not
            a member" confirms the organization exists, which is information we
            do not owe them.
    """
    if principal.user_id is None:
        raise AuthorizationError(
            "You do not have access to this organization.",
            detail={"reason": "anonymous_principal"},
        )

    memberships = await users.list_memberships(principal.user_id)
    membership = next(
        (m for m in memberships if m.organization_id == organization_id and m.deleted_at is None),
        None,
    )
    if membership is None:
        # Security-relevant: someone asked for an organization they are not in.
        # Logged with ids only — never a name, never an email.
        _logger.warning(
            "authorization.membership_denied",
            organization_id=str(organization_id),
            actor_id=str(principal.user_id),
        )
        raise AuthorizationError(
            "You do not have access to this organization.",
            detail={"reason": "no_active_membership"},
        )

    role = await roles.get(membership.role_id)

    # A membership must reference either a platform catalog role
    # (`organization_id IS NULL`) or a custom role owned by this same
    # organization. Nothing in the schema enforces this: `organization_members`
    # has a plain FK to `roles.id`, and the composite FK we use everywhere else
    # cannot be applied here because `roles.organization_id` is nullable, so a
    # platform role would never satisfy it.
    #
    # Left unchecked, a membership in organization A pointing at organization B's
    # custom role would let B decide A's permission set — a privilege-escalation
    # path across the tenant boundary. Refuse rather than degrade: unlike the
    # stale-permission case below, this is not a role that means slightly too
    # much, it is a role that belongs to someone else.
    if role.organization_id is not None and role.organization_id != organization_id:
        _logger.error(
            "authorization.cross_tenant_role",
            organization_id=str(organization_id),
            role_id=str(role.id),
            role_organization_id=str(role.organization_id),
        )
        raise AuthorizationError(
            "You do not have access to this organization.",
            detail={"reason": "cross_tenant_role", "role_id": str(role.id)},
        )

    granted = frozenset(role.permissions)

    # A tenant role must never carry a cross-tenant permission. The domain
    # enforces this on `Role` and again on `TenantContext`; filtering here means
    # a stale row written before that rule existed degrades to "fewer
    # permissions" rather than failing the request outright.
    platform_grants = {p for p in granted if is_platform_permission(p)}
    if platform_grants:
        _logger.error(
            "authorization.platform_permission_on_tenant_role",
            role_id=str(role.id),
            organization_id=str(organization_id),
        )
        granted = frozenset(granted - platform_grants)

    return TenantContext(
        organization_id=organization_id,
        actor_id=principal.user_id,
        permissions=granted,
        request_id=principal.request_id,
    )


def build_platform_context(principal: Principal) -> PlatformContext:
    """Derive cross-tenant authority.

    Deliberately synchronous and deliberately narrow: platform authority comes
    from the authentication layer, not from a database lookup, so there is no
    query here that could be tricked into granting it.

    Raises:
        AuthorizationError: if the principal is not platform staff. A principal
            carrying platform permissions without the staff flag is a bug or an
            attack; either way it is refused rather than partially honoured.
    """
    if not principal.is_platform_staff:
        _logger.warning(
            "authorization.platform_context_denied",
            actor_id=str(principal.user_id) if principal.user_id else None,
        )
        raise AuthorizationError(
            "This action requires platform administration access.",
            detail={"reason": "not_platform_staff"},
        )

    return PlatformContext(
        actor_id=principal.user_id,
        permissions=principal.platform_permissions,
        request_id=principal.request_id,
    )


def accessible_organization_ids(
    memberships: Sequence[OrganizationMemberModel],
) -> list[uuid.UUID]:
    """The organizations a set of membership rows grants access to.

    Used to render an organization switcher. Note that this answers "which
    tenants may this person enter", not "which tenant is this request for" —
    the second question is always answered by `build_tenant_context`, which
    re-checks membership against the database rather than trusting this list.
    """
    return [m.organization_id for m in memberships if m.deleted_at is None]
