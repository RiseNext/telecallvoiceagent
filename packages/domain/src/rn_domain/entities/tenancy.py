"""Tenancy and identity entities.

`Organization` is the tenant. Its id is **our** UUID, never an auth provider's —
telephony records, call history, billing ledgers and retained recordings must
outlive an auth-provider migration or a deleted external org (ADR-007).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rn_core.clock import now_utc, zone
from rn_core.errors import InvariantViolation
from rn_domain.enums import OrganizationStatus
from rn_domain.identifiers import OrganizationId, RoleId, UserId
from rn_domain.permissions import ALL_PERMISSIONS, Permission, is_platform_permission

__all__ = ["Organization", "OrganizationMember", "Role", "User"]

_MAX_NAME_LENGTH = 200


@dataclass(slots=True)
class Organization:
    """A tenant.

    `timezone` is the organization's business timezone — it drives calling
    windows and how times are rendered, never how they are stored.
    """

    id: OrganizationId
    name: str
    slug: str
    status: OrganizationStatus = OrganizationStatus.ACTIVE
    timezone: str = "Asia/Kolkata"
    #: The external identity-provider organization key. A unique column, never
    #: the primary key. Nullable because an organization can exist before it is
    #: linked to an identity provider — and because a webhook is not allowed to
    #: be the only path that creates a tenant (HC-33).
    external_auth_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvariantViolation("Organization name must not be blank.")
        if len(self.name) > _MAX_NAME_LENGTH:
            raise InvariantViolation("Organization name is too long.")
        if not self.slug.strip():
            raise InvariantViolation("Organization slug must not be blank.")
        # Validates the IANA zone. An unknown timezone here would surface much
        # later as a campaign dialling at the wrong hour.
        zone(self.timezone)

    @property
    def is_active(self) -> bool:
        return self.status is OrganizationStatus.ACTIVE and self.deleted_at is None

    @property
    def may_dial(self) -> bool:
        """Whether this organization is permitted to place calls at all.

        A suspended organization keeps its data and its dashboard; it just stops
        dialling. Checked by the pre-dial gate, not by the dashboard.
        """
        return self.is_active


@dataclass(slots=True)
class User:
    """A human. Platform-global: one person may belong to several organizations.

    Deliberately **not** tenant-owned. Modelling a user per organization would
    make "the same person in two tenants" two rows that drift apart, and would
    make an auth-provider identity map to many local users.
    """

    id: UserId
    email: str
    display_name: str | None = None
    #: External identity-provider user key. Unique column, never the PK.
    external_auth_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if "@" not in self.email or self.email.startswith("@") or self.email.endswith("@"):
            raise InvariantViolation("User email is malformed.")
        self.email = self.email.strip().lower()


@dataclass(slots=True)
class Role:
    """A named set of permissions.

    `organization_id is None` means a platform catalog role, shared by every
    tenant. A non-null value is a tenant's own custom role — which is where they
    have to live, because the identity provider caps custom roles well below what
    a multi-tenant platform needs (HC-31).
    """

    id: RoleId
    key: str
    name: str
    permissions: frozenset[Permission]
    organization_id: OrganizationId | None = None
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise InvariantViolation("Role key must not be blank.")
        unknown = set(self.permissions) - ALL_PERMISSIONS
        if unknown:
            raise InvariantViolation(
                "Role references permissions that are not in the catalog.",
                detail={"unknown": sorted(unknown)},
            )
        # A tenant-owned role granting a cross-tenant capability would be a
        # privilege-escalation path: a CLIENT_ADMIN could mint themselves a role
        # that reads other organizations' calls.
        if self.organization_id is not None:
            platform = {p for p in self.permissions if is_platform_permission(p)}
            if platform:
                raise InvariantViolation(
                    "An organization-owned role may not grant platform permissions.",
                    detail={"permissions": sorted(platform)},
                )

    @property
    def is_platform_role(self) -> bool:
        return self.organization_id is None


@dataclass(slots=True)
class OrganizationMember:
    """A user's membership of an organization, carrying their role.

    Membership *is* the tenant grant: a subject with no membership row for an
    organization has no access to it, whatever their token claims.
    """

    user_id: UserId
    organization_id: OrganizationId
    role_id: RoleId
    created_at: datetime = field(default_factory=now_utc)
    deleted_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None
