"""Tenancy and identity tables.

Three classifications live in this one module, which is why they are worth
stating explicitly:

* **Platform-global** — `organizations` (it *is* the tenant) and `users` (one
  person may belong to several organizations). No `organization_id` at all.
* **Nullable tenant** — `roles`. `NULL` means a platform catalog role shared by
  every tenant; a value means a tenant's own custom role.
* **Tenant-owned** — `organization_members`. Each row belongs to exactly one
  organization, so it carries a non-nullable key like any other tenant table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from rn_core.errors import InvariantViolation
from rn_domain.entities.tenancy import Organization, OrganizationMember, Role, User
from rn_domain.enums import OrganizationStatus
from rn_domain.identifiers import OrganizationId, RoleId, UserId
from rn_persistence.base import (
    Base,
    TenantOwnedBase,
    created_at_column,
    enum_check,
    nullable_organization_fk,
)

__all__ = ["OrganizationMemberModel", "OrganizationModel", "RoleModel", "UserModel"]


class OrganizationModel(Base):
    """The tenant.

    `external_auth_id` is a unique *column*, never the primary key. Telephony
    records, call history, billing ledgers and retained recordings must outlive
    an auth-provider migration or a deleted external organization (ADR-007).
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    external_auth_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        UniqueConstraint("external_auth_id", name="uq_organizations_external_auth_id"),
        enum_check("status", OrganizationStatus, "status"),
    )

    def to_domain(self) -> Organization:
        return Organization(
            id=OrganizationId(self.id),
            name=self.name,
            slug=self.slug,
            status=OrganizationStatus(self.status),
            timezone=self.timezone,
            external_auth_id=self.external_auth_id,
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, entity: Organization) -> OrganizationModel:
        return cls(
            id=entity.id,
            name=entity.name,
            slug=entity.slug,
            status=entity.status.value,
            timezone=entity.timezone,
            external_auth_id=entity.external_auth_id,
            created_at=entity.created_at,
            deleted_at=entity.deleted_at,
        )

    def apply(self, entity: Organization) -> None:
        """Copy mutable fields from a domain entity onto a loaded row.

        Deliberately does not touch `id` or `created_at` — an update that can
        change a primary key is a bug generator.
        """
        if entity.id != self.id:
            raise InvariantViolation("Cannot apply a different organization onto this row.")
        self.name = entity.name
        self.slug = entity.slug
        self.status = entity.status.value
        self.timezone = entity.timezone
        self.external_auth_id = entity.external_auth_id
        self.deleted_at = entity.deleted_at


class UserModel(Base):
    """A human. Platform-global: one person may belong to several organizations."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_auth_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("external_auth_id", name="uq_users_external_auth_id"),
    )

    def to_domain(self) -> User:
        return User(
            id=UserId(self.id),
            email=self.email,
            display_name=self.display_name,
            external_auth_id=self.external_auth_id,
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, entity: User) -> UserModel:
        return cls(
            id=entity.id,
            email=entity.email,
            display_name=entity.display_name,
            external_auth_id=entity.external_auth_id,
            created_at=entity.created_at,
            deleted_at=entity.deleted_at,
        )


class RoleModel(Base):
    """A named permission set.

    `permissions` is a `text[]` rather than a join table: we never ask "which
    roles have permission X" at scale, we always load one role and read the whole
    set. The array is constrained by a CHECK generated from the domain catalog,
    so an unknown permission cannot be stored — see the baseline migration.

    `organization_id IS NULL` means a platform catalog role shared by every
    tenant. A non-null value is a tenant's own custom role.
    """

    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = nullable_organization_fk()
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # A platform role's key is globally unique; a custom role's key is unique
        # within its organization. Two partial unique indexes rather than one
        # constraint, because NULL never equals NULL in a UNIQUE.
        Index(
            "uq_roles_platform_key",
            "key",
            unique=True,
            postgresql_where="organization_id IS NULL",
        ),
        Index(
            "uq_roles_org_key",
            "organization_id",
            "key",
            unique=True,
            postgresql_where="organization_id IS NOT NULL",
        ),
    )

    def to_domain(self) -> Role:
        return Role(
            id=RoleId(self.id),
            key=self.key,
            name=self.name,
            permissions=frozenset(self.permissions),
            organization_id=(
                OrganizationId(self.organization_id) if self.organization_id else None
            ),
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: Role) -> RoleModel:
        return cls(
            id=entity.id,
            key=entity.key,
            name=entity.name,
            permissions=sorted(entity.permissions),
            organization_id=entity.organization_id,
            created_at=entity.created_at,
        )


class OrganizationMemberModel(TenantOwnedBase):
    """A user's membership of an organization, carrying their role.

    Membership *is* the tenant grant. A subject with no active row here has no
    access to that organization, whatever an external token claims.

    **Tenant-owned**, because each row belongs to exactly one organization and a
    tenant listing its members must see only its own. The one query that
    deliberately crosses tenants — "which organizations does this user belong
    to?", used to build the organization switcher and to derive a
    `TenantContext` — lives in `UserRepository`, which is a `PlatformRepository`.
    That asymmetry is the point: the cross-tenant read is in a differently-named
    class that a security review can grep for.
    """

    __tablename__ = "organization_members"

    # `role_id` is a plain FK to `roles(id)`, and same-tenant integrity is
    # enforced by the `organization_members_role_scope` TRIGGER rather than by a
    # composite foreign key. The reason is in `roles.organization_id` being
    # nullable, which no declarative constraint handles:
    #
    #   * MATCH SIMPLE composite FK -> skipped entirely whenever the denormalised
    #     role-org column is NULL, so a forged NULL bypasses it.
    #   * MATCH FULL composite FK   -> forbids NULLs outright, which makes every
    #     platform catalog role unassignable.
    #   * splitting into `platform_roles` + `organization_roles` would express it
    #     declaratively, at the cost of two tables and a branch in every read.
    #
    # A trigger states the rule once, enforces it for every writer including raw
    # SQL, and costs nothing on a table written only at invite/remove time. See
    # the baseline migration.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # One active membership per (user, organization). Partial, so that a
        # removed-and-re-added member does not collide with their old row.
        Index(
            "uq_organization_members_active",
            "organization_id",
            "user_id",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
        # The authorization hot path: "what may this user do in this org?"
        Index("ix_organization_members_user_id", "user_id"),
    )

    def to_domain(self) -> OrganizationMember:
        return OrganizationMember(
            user_id=UserId(self.user_id),
            organization_id=OrganizationId(self.organization_id),
            role_id=RoleId(self.role_id),
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )
