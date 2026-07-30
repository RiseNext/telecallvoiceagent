"""Resource-oriented authorization policies.

A permission check alone is rarely the whole answer. "May this actor start this
campaign?" is a permission *and* a state question — the actor may hold
`org:campaign:start` while the campaign is already running, or the organization
is suspended.

Each function here answers one such question completely, so callers never write

    if role == "admin" and campaign.status == "draft": ...

anywhere. That pattern is the thing this module exists to prevent: scattered, it
drifts, and the copy someone forgets to update is the one that matters.

Policies raise rather than return a boolean. A boolean invites `if not
can_x(): ...` with a forgotten `not`; a raised `AuthorizationError` cannot be
accidentally ignored.
"""

from __future__ import annotations

from rn_core.errors import AuthorizationError, ConflictError
from rn_domain.entities.campaigns import Campaign
from rn_domain.entities.tenancy import Organization
from rn_domain.enums import CampaignStatus
from rn_domain.tenancy import TenantContext

__all__ = [
    "ensure_can_export_calls",
    "ensure_can_publish_agent",
    "ensure_can_start_campaign",
    "ensure_organization_active",
    "ensure_same_tenant",
]


def ensure_same_tenant(context: TenantContext, organization_id: object) -> None:
    """Assert that a resource belongs to the acting tenant.

    Repositories already guarantee this for anything they loaded — this is for
    the cases where an entity arrives from somewhere else: a job payload, a
    provider callback, a tool argument. Cheap, and the one place a cross-tenant
    object could otherwise slip through.
    """
    if organization_id != context.organization_id:
        raise AuthorizationError(
            "That resource does not belong to this organization.",
            detail={
                "expected_organization_id": str(context.organization_id),
                "actual_organization_id": str(organization_id),
            },
        )


def ensure_organization_active(organization: Organization) -> None:
    """Assert the organization may still act.

    Separate from permissions on purpose: a suspended tenant's admins keep every
    permission they had, they just cannot make the platform do anything. Folding
    this into the permission set would mean recomputing permissions on
    suspension, and would make "why did this fail" harder to answer.
    """
    if not organization.is_active:
        raise AuthorizationError(
            "This organization is not active.",
            detail={"organization_id": str(organization.id), "status": organization.status.value},
        )


def ensure_can_start_campaign(
    context: TenantContext, campaign: Campaign, organization: Organization
) -> None:
    """Permission, tenancy, organization state and campaign state, in one place.

    Ordered cheapest-and-most-absolute first: a caller without the permission
    learns nothing about the campaign's state, which is the correct amount of
    information to leak.
    """
    context.require("org:campaign:start")
    ensure_same_tenant(context, campaign.organization_id)
    ensure_organization_active(organization)

    if campaign.status not in {
        CampaignStatus.DRAFT,
        CampaignStatus.SCHEDULED,
        CampaignStatus.PAUSED,
    }:
        raise ConflictError(
            "This campaign cannot be started from its current state.",
            detail={"status": campaign.status.value},
        )


def ensure_can_publish_agent(context: TenantContext, organization: Organization) -> None:
    """Publishing changes what an agent will say and do on real calls."""
    context.require("org:agent:publish")
    ensure_organization_active(organization)


def ensure_can_export_calls(context: TenantContext) -> None:
    """Exporting is a separate permission from reading, deliberately.

    A call export leaves the platform as a file containing transcripts and phone
    numbers, which someone can then forward. Whoever may read calls in the UI
    does not automatically get to take them away.
    """
    context.require("org:call:export")
