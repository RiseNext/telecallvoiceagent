"""The permission catalog.

Authorization is entirely ours. The identity provider's built-in permissions
never reach the backend (PROVIDER_CONSTRAINTS HC-30), and its custom-role ceiling
means per-tenant roles must live in our database anyway (HC-31, PRD **D-7**). So
this list is the whole security surface, and it is deliberately a frozen literal
rather than rows someone can add at runtime.

Format: `<scope>:<feature>:<action>`.

* `org:*` — acts within one organization. Granted by an organization role.
* `platform:*` — acts across organizations. Granted only to platform staff, and
  every one of these is a cross-tenant capability, so they are enumerated
  separately and are easy to grep for in a security review.

**Adding a permission requires a migration.** The `roles.permissions` array is
constrained by a CHECK generated from `ALL_PERMISSIONS`, so a new value cannot be
stored until the constraint is updated. That friction is intentional: a
permission appearing without review is how privilege creep happens.

> Note on DATA_MODEL §2.1: it describes this as "validated by a CHECK against a
> small reference table", which is not a thing Postgres can do — a CHECK cannot
> reference another table, and array-element foreign keys are not available in
> PG 17. The enforceable equivalent is a CHECK against a literal array generated
> from this module, which is what the migration does.
"""

from __future__ import annotations

from typing import Final

from rn_core.errors import ValidationError

__all__ = [
    "ALL_PERMISSIONS",
    "ORG_PERMISSIONS",
    "PLATFORM_PERMISSIONS",
    "Permission",
    "is_platform_permission",
    "parse_permission",
]

Permission = str

# --------------------------------------------------------------------------
# Organization-scoped. The acting organization is always the subject's own.
# --------------------------------------------------------------------------
ORG_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        "org:organization:read",
        "org:organization:update",
        "org:member:read",
        "org:member:invite",
        "org:member:update",
        "org:member:remove",
        "org:agent:read",
        "org:agent:create",
        "org:agent:update",
        "org:agent:publish",
        "org:agent:delete",
        "org:knowledge:read",
        "org:knowledge:create",
        "org:knowledge:update",
        "org:knowledge:delete",
        "org:contact:read",
        "org:contact:create",
        "org:contact:update",
        "org:contact:delete",
        "org:contact:import",
        "org:contact:export",
        "org:lead:read",
        "org:lead:update",
        "org:lead:export",
        "org:campaign:read",
        "org:campaign:create",
        "org:campaign:update",
        "org:campaign:start",
        "org:campaign:pause",
        "org:campaign:cancel",
        "org:call:read",
        # Separate from `call:read` because an export leaves the platform with
        # transcripts and phone numbers in a file someone can forward.
        "org:call:export",
        "org:analytics:read",
        "org:consent:read",
        "org:consent:manage",
        "org:suppression:read",
        "org:suppression:manage",
        "org:integration:read",
        "org:integration:manage",
        "org:audit:read",
    }
)

# --------------------------------------------------------------------------
# Platform-scoped. Every one of these crosses the tenant boundary.
# --------------------------------------------------------------------------
PLATFORM_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        "platform:organization:read",
        "platform:organization:create",
        "platform:organization:update",
        "platform:organization:suspend",
        "platform:usage:read",
        "platform:audit:read",
        # Reading another tenant's calls. Support genuinely needs this; it is
        # also the single most dangerous permission in the catalog, so it is its
        # own value rather than being folded into a broader admin grant.
        "platform:call:read",
        "platform:health:read",
    }
)

ALL_PERMISSIONS: Final[frozenset[Permission]] = ORG_PERMISSIONS | PLATFORM_PERMISSIONS


def parse_permission(value: str) -> tuple[str, str, str]:
    """Split a permission into `(scope, feature, action)`.

    Raises `ValidationError` for anything not in the catalog — an unknown
    permission is never treated as a harmless no-op, because a typo in a policy
    check would then silently allow or deny.
    """
    if value not in ALL_PERMISSIONS:
        raise ValidationError("Unknown permission.", detail={"permission": value})
    scope, feature, action = value.split(":", 2)
    return scope, feature, action


def is_platform_permission(value: Permission) -> bool:
    """Whether this permission crosses the tenant boundary."""
    return value in PLATFORM_PERMISSIONS
