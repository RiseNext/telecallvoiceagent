"""The acting subject: who is doing this, and in which tenant.

`TenantContext` lives in the domain rather than in `rn_services` because both
layers above need it and neither may import the other: repositories in
`rn_persistence` are scoped by it, and the authorization policy in `rn_services`
reasons about it. It is a pure value object with no I/O, so the domain is the
only place it can sit without inverting the layer graph. The *policy* seam —
deciding what a context may do — remains in `rn_services`.

**Two types, not one type with a flag.**

`TenantContext` always carries an organization. `PlatformContext` never does.
A tenant-scoped repository accepts only the former, so cross-tenant access is
not something you can reach by passing `bypass_tenant=True` — it requires
constructing a different type and calling a differently-named repository, which
means it shows up in a grep and in a diff. Dangerous access should be visible,
not merely discouraged.

**Where the organization id comes from.** A verified session token, or a
server-side call session. Never a request body, never a query parameter, never a
CSV row, never a model's tool arguments. Constructing one of these is an
assertion that the caller has already established identity server-side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rn_core.errors import AuthorizationError, InvariantViolation
from rn_domain.identifiers import OrganizationId, UserId
from rn_domain.permissions import ALL_PERMISSIONS, Permission, is_platform_permission

__all__ = ["ActorContext", "PlatformContext", "TenantContext"]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Authority to act within exactly one organization."""

    organization_id: OrganizationId
    actor_id: UserId | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    #: Opaque correlation id for logs and audit rows. Never an authority source.
    request_id: str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.permissions) - ALL_PERMISSIONS
        if unknown:
            raise InvariantViolation(
                "TenantContext carries permissions that are not in the catalog.",
                detail={"unknown": sorted(unknown)},
            )
        # A tenant context granting a cross-tenant capability would defeat the
        # entire point of having two types.
        platform = {p for p in self.permissions if is_platform_permission(p)}
        if platform:
            raise InvariantViolation(
                "A TenantContext may not carry platform permissions; "
                "use PlatformContext for cross-tenant work.",
                detail={"permissions": sorted(platform)},
            )

    @property
    def is_system(self) -> bool:
        """Whether the actor is the platform itself rather than a person.

        A scheduled job acting for a tenant has no user. Audit rows record this
        honestly as "no actor" rather than attributing the action to someone.
        """
        return self.actor_id is None

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Assert a permission, raising `AuthorizationError` if absent.

        The error deliberately does not say which permission was missing when it
        reaches a user — `message` is generic and the specifics live in `detail`,
        which is logged and never serialised to a client. Telling an attacker
        exactly which permission to look for is free reconnaissance.
        """
        if permission not in self.permissions:
            raise AuthorizationError(
                "You do not have permission to perform this action.",
                detail={
                    "required": permission,
                    "organization_id": str(self.organization_id),
                    "actor_id": str(self.actor_id) if self.actor_id else None,
                },
            )


@dataclass(frozen=True, slots=True)
class PlatformContext:
    """Authority to act across organizations. Platform staff and system jobs only.

    Every permission here crosses the tenant boundary, which is why they are a
    separate catalog and why this is a separate type. Anything constructed with
    one of these should be assumed to be in scope for a security review.
    """

    actor_id: UserId | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    request_id: str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.permissions) - ALL_PERMISSIONS
        if unknown:
            raise InvariantViolation(
                "PlatformContext carries permissions that are not in the catalog.",
                detail={"unknown": sorted(unknown)},
            )

    @property
    def is_system(self) -> bool:
        return self.actor_id is None

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise AuthorizationError(
                "You do not have permission to perform this action.",
                detail={
                    "required": permission,
                    "actor_id": str(self.actor_id) if self.actor_id else None,
                },
            )

    def for_organization(
        self, organization_id: OrganizationId, permissions: frozenset[Permission]
    ) -> TenantContext:
        """Narrow to one organization.

        The only sanctioned way for platform staff to use a tenant-scoped
        repository. Explicit by design: the caller has to name the organization
        and the organization-scoped permissions being exercised, so the act of
        entering a tenant is visible in the code rather than implied.
        """
        return TenantContext(
            organization_id=organization_id,
            actor_id=self.actor_id,
            permissions=permissions,
            request_id=self.request_id,
        )


#: Either kind of caller, for code that only needs the actor and correlation id.
type ActorContext = TenantContext | PlatformContext
