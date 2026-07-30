"""Unit tests for the domain: value objects, entity invariants, policies, contexts.

Fast by design — no fixtures, no I/O. These are the rules that change most
often, so the suite that guards them has to stay cheap enough to run constantly.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from rn_core.clock import IST, now_utc
from rn_core.errors import (
    AuthorizationError,
    ConflictError,
    InvariantViolation,
    ValidationError,
)
from rn_core.ids import new_id
from rn_domain.entities.agents import AgentVersion
from rn_domain.entities.calls import Call
from rn_domain.entities.campaigns import Campaign, CampaignContact
from rn_domain.entities.people import Suppression
from rn_domain.entities.tenancy import Organization, Role
from rn_domain.enums import (
    AgentVersionStatus,
    CallDirection,
    CallEndReason,
    CallStatus,
    CampaignContactStatus,
    CampaignStatus,
    SuppressionReason,
    SuppressionScope,
)
from rn_domain.events import EventType, build_outbox_event
from rn_domain.identifiers import (
    AgentId,
    AgentVersionId,
    CallId,
    CampaignContactId,
    CampaignId,
    ContactId,
    OrganizationId,
    RoleId,
    UserId,
)
from rn_domain.permissions import ALL_PERMISSIONS, ORG_PERMISSIONS, PLATFORM_PERMISSIONS
from rn_domain.policies import DialRejection, RetryPolicy, evaluate_dial_eligibility, next_retry_at
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_domain.values import LanguagePolicy, LanguageTag, PhoneNumber

pytestmark = [pytest.mark.unit]


def _org_id() -> OrganizationId:
    return OrganizationId(new_id())


# ---------------------------------------------------------------------------
# PhoneNumber
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("098765-43210", "+919876543210"),
    ],
)
def test_indian_numbers_normalise_to_e164(raw: str, expected: str) -> None:
    assert PhoneNumber.parse(raw).e164 == expected


@pytest.mark.parametrize("raw", ["", "12", "not a number", "+91123"])
def test_invalid_numbers_are_rejected_at_the_boundary(raw: str) -> None:
    with pytest.raises(ValidationError):
        PhoneNumber.parse(raw)


def test_try_parse_returns_none_for_bulk_import() -> None:
    """A bad CSV row is a row to reject, not an exception on row 3."""
    assert PhoneNumber.try_parse("garbage") is None
    assert PhoneNumber.try_parse("9876543210") is not None


def test_validation_error_does_not_echo_the_number() -> None:
    """A rejected value is still a phone number."""
    with pytest.raises(ValidationError) as caught:
        PhoneNumber.parse("+91123")
    assert "123" not in str(caught.value.detail)


def test_str_and_repr_are_masked() -> None:
    """An accidental f-string must not leak a number."""
    phone = PhoneNumber.parse("9876543210")
    assert "9876543210" not in str(phone)
    assert "9876543210" not in repr(phone)
    assert str(phone) == "+91XXXXXXXX10"


def test_hash_is_deterministic_and_pepper_dependent() -> None:
    phone = PhoneNumber.parse("9876543210")
    assert phone.hashed("pepper-a") == phone.hashed("pepper-a")
    assert phone.hashed("pepper-a") != phone.hashed("pepper-b")


def test_hash_requires_a_pepper() -> None:
    """An unpeppered hash of a 10-digit space is reversible by enumeration."""
    with pytest.raises(InvariantViolation):
        PhoneNumber.parse("9876543210").hashed("")


def test_different_numbers_hash_differently() -> None:
    a = PhoneNumber.parse("9876543210").hashed("p")
    b = PhoneNumber.parse("9876543211").hashed("p")
    assert a != b


@pytest.mark.parametrize("tag", ["en", "hi-IN", "te-IN"])
def test_language_tags_accepted(tag: str) -> None:
    assert LanguageTag(tag).value == tag


def test_language_tag_primary_ignores_region() -> None:
    assert LanguageTag("hi-IN").primary == "hi"


def test_malformed_language_tag_rejected() -> None:
    with pytest.raises(ValidationError):
        LanguageTag("English")


# ---------------------------------------------------------------------------
# Entity invariants
# ---------------------------------------------------------------------------


def test_organization_rejects_an_unknown_timezone() -> None:
    """An unknown zone would surface much later as a call at the wrong hour."""
    with pytest.raises(ValidationError):
        Organization(id=_org_id(), name="Acme", slug="acme", timezone="Mars/Olympus")


def test_organization_rejects_a_blank_name() -> None:
    with pytest.raises(InvariantViolation):
        Organization(id=_org_id(), name="   ", slug="acme")


def test_tenant_role_cannot_grant_platform_permissions() -> None:
    """Otherwise a client admin could mint themselves cross-tenant access."""
    with pytest.raises(InvariantViolation):
        Role(
            id=RoleId(new_id()),
            key="admin",
            name="Admin",
            permissions=frozenset({"platform:call:read"}),
            organization_id=_org_id(),
        )


def test_platform_role_may_grant_platform_permissions() -> None:
    role = Role(
        id=RoleId(new_id()),
        key="platform-admin",
        name="Platform Admin",
        permissions=frozenset({"platform:call:read"}),
    )
    assert role.is_platform_role


def test_role_rejects_permissions_outside_the_catalog() -> None:
    with pytest.raises(InvariantViolation):
        Role(
            id=RoleId(new_id()),
            key="x",
            name="X",
            permissions=frozenset({"org:agent:launch_nukes"}),
        )


def _agent_version(**overrides: object) -> AgentVersion:
    defaults: dict[str, object] = {
        "id": AgentVersionId(new_id()),
        "organization_id": _org_id(),
        "agent_id": AgentId(new_id()),
        "version_number": 1,
        "instructions": "You are Aira, a helpful assistant for RiseNext.",
        "language_policy": LanguagePolicy.single("en"),
    }
    defaults.update(overrides)
    return AgentVersion(**defaults)  # type: ignore[arg-type]


def test_agent_version_requires_substantive_instructions() -> None:
    with pytest.raises(InvariantViolation):
        _agent_version(instructions="hi")


def test_agent_version_requires_a_language() -> None:
    """The invariant now lives on `LanguagePolicy`, which is where it belongs.

    An agent version has no `languages` field to leave empty — `languages` is a
    read-only projection of the policy — so the only way to reach this state is
    through a policy that allows nothing, and the policy refuses to exist.
    """
    with pytest.raises(InvariantViolation):
        LanguagePolicy(primary=LanguageTag("en"), allowed=())


def test_agent_version_languages_project_the_policy() -> None:
    """`languages` is derived, not stored. There is nothing to disagree with."""
    policy = LanguagePolicy(
        primary=LanguageTag("hi-IN"),
        allowed=(LanguageTag("hi-IN"), LanguageTag("en")),
    )
    version = _agent_version(language_policy=policy)
    assert version.languages == policy.allowed
    # And it is genuinely a property: assigning it is not a thing you can do.
    with pytest.raises(AttributeError):
        version.languages = ()  # type: ignore[misc]


def test_publishing_freezes_the_version() -> None:
    version = _agent_version()
    assert not version.is_frozen
    version.publish()
    assert version.is_published and version.is_frozen


def test_republishing_is_a_conflict_not_a_crash() -> None:
    """A double-clicked button is a user action, not a broken invariant."""
    version = _agent_version()
    version.publish()
    with pytest.raises(ConflictError):
        version.publish()


def test_archived_version_cannot_be_published() -> None:
    version = _agent_version()
    version.archive()
    with pytest.raises(ConflictError):
        version.publish()


def test_published_version_must_carry_published_at() -> None:
    with pytest.raises(InvariantViolation):
        _agent_version(status=AgentVersionStatus.PUBLISHED, published_at=None)


# ---------------------------------------------------------------------------
# Call state machine
# ---------------------------------------------------------------------------


def _call(**overrides: object) -> Call:
    defaults: dict[str, object] = {
        "id": CallId(new_id()),
        "organization_id": _org_id(),
        "agent_version_id": AgentVersionId(new_id()),
        "direction": CallDirection.OUTBOUND,
        "counterparty_phone": PhoneNumber.parse("9876543210"),
        "contact_id": ContactId(new_id()),
    }
    defaults.update(overrides)
    return Call(**defaults)  # type: ignore[arg-type]


def test_outbound_call_requires_a_contact() -> None:
    with pytest.raises(InvariantViolation):
        _call(contact_id=None)


def test_happy_path_transitions() -> None:
    call = _call()
    call.transition_to(CallStatus.DIALING)
    call.transition_to(CallStatus.RINGING)
    started = now_utc()
    call.mark_answered(started)
    assert call.status is CallStatus.IN_PROGRESS
    call.finalize(
        status=CallStatus.COMPLETED,
        ended_at=started + timedelta(seconds=42),
        reason=CallEndReason.CALLER_HUNG_UP,
        duration_ms=42_000,
    )
    assert call.is_terminal
    assert call.talk_time_ms == 42_000


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (CallStatus.QUEUED, CallStatus.IN_PROGRESS),
        (CallStatus.COMPLETED, CallStatus.IN_PROGRESS),
        (CallStatus.NO_ANSWER, CallStatus.COMPLETED),
        (CallStatus.IN_PROGRESS, CallStatus.RINGING),
    ],
)
def test_illegal_transitions_are_refused(start: CallStatus, target: CallStatus) -> None:
    """A replayed or out-of-order provider callback must not rewrite our state."""
    call = _call(status=start)
    with pytest.raises(ConflictError):
        call.transition_to(target)


def test_repeating_the_current_status_is_idempotent() -> None:
    """Duplicate provider callbacks are expected, not an error."""
    call = _call(status=CallStatus.DIALING)
    call.transition_to(CallStatus.DIALING)
    assert call.status is CallStatus.DIALING


def test_finalize_requires_a_terminal_status() -> None:
    call = _call(status=CallStatus.DIALING)
    with pytest.raises(InvariantViolation):
        call.finalize(status=CallStatus.RINGING, ended_at=now_utc())


def test_negative_duration_is_rejected() -> None:
    """Wall-clock subtraction across a clock step can produce one."""
    with pytest.raises(InvariantViolation):
        _call(duration_ms=-1)


def test_call_cannot_end_before_it_started() -> None:
    started = now_utc()
    with pytest.raises(InvariantViolation):
        _call(started_at=started, ended_at=started - timedelta(seconds=1))


def test_measured_duration_wins_over_wall_clock() -> None:
    """The media plane measures monotonically; wall clock is a fallback."""
    call = _call(status=CallStatus.DIALING)
    started = now_utc()
    call.mark_answered(started)
    call.finalize(
        status=CallStatus.COMPLETED,
        ended_at=started + timedelta(seconds=10),
        duration_ms=9_500,
    )
    assert call.duration_ms == 9_500


# ---------------------------------------------------------------------------
# Campaign dispatch state machine
# ---------------------------------------------------------------------------


def _campaign_contact(**overrides: object) -> CampaignContact:
    defaults: dict[str, object] = {
        "id": CampaignContactId(new_id()),
        "organization_id": _org_id(),
        "campaign_id": CampaignId(new_id()),
        "contact_id": ContactId(new_id()),
    }
    defaults.update(overrides)
    return CampaignContact(**defaults)  # type: ignore[arg-type]


def test_attempt_count_increments_at_dial_not_at_completion() -> None:
    """A crash between dialling and the callback must not loop forever."""
    row = _campaign_contact()
    row.mark_in_flight(CallId(new_id()))
    assert row.attempt_count == 1
    assert row.status is CampaignContactStatus.IN_FLIGHT


def test_a_second_dial_while_in_flight_is_refused() -> None:
    row = _campaign_contact()
    row.mark_in_flight(CallId(new_id()))
    with pytest.raises(ConflictError):
        row.mark_in_flight(CallId(new_id()))


def test_an_excluded_contact_is_never_dialled() -> None:
    row = _campaign_contact()
    row.exclude(DialRejection.SUPPRESSED.value)
    with pytest.raises(ConflictError):
        row.mark_in_flight(CallId(new_id()))


def test_exclusion_must_record_a_reason() -> None:
    """Exclusion counts are a compliance signal, so the reason is mandatory."""
    with pytest.raises(InvariantViolation):
        _campaign_contact(status=CampaignContactStatus.EXCLUDED)
    row = _campaign_contact()
    with pytest.raises(InvariantViolation):
        row.exclude("  ")


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (CampaignStatus.COMPLETED, CampaignStatus.RUNNING),
        (CampaignStatus.CANCELLED, CampaignStatus.RUNNING),
        (CampaignStatus.DRAFT, CampaignStatus.RUNNING),
    ],
)
def test_illegal_campaign_transitions_refused(
    start: CampaignStatus, target: CampaignStatus
) -> None:
    campaign = Campaign(
        id=CampaignId(new_id()),
        organization_id=_org_id(),
        name="C",
        agent_version_id=AgentVersionId(new_id()),
        status=start,
    )
    with pytest.raises(ConflictError):
        campaign.transition_to(target)


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_platform_suppression_blocks_every_tenant() -> None:
    """ "Never call me from this platform again" cannot be per-tenant."""
    entry = Suppression(
        id=new_id(),  # type: ignore[arg-type]
        phone_hash="h" * 64,
        scope=SuppressionScope.PLATFORM,
        reason=SuppressionReason.USER_REQUEST,
    )
    assert entry.blocks(_org_id()) and entry.blocks(_org_id())


def test_org_suppression_blocks_only_its_own_tenant() -> None:
    owner = _org_id()
    entry = Suppression(
        id=new_id(),  # type: ignore[arg-type]
        phone_hash="h" * 64,
        scope=SuppressionScope.ORGANIZATION,
        reason=SuppressionReason.USER_REQUEST,
        organization_id=owner,
    )
    assert entry.blocks(owner)
    assert not entry.blocks(_org_id())


def test_suppression_scope_and_tenant_must_agree() -> None:
    with pytest.raises(InvariantViolation):
        Suppression(
            id=new_id(),  # type: ignore[arg-type]
            phone_hash="h" * 64,
            scope=SuppressionScope.PLATFORM,
            reason=SuppressionReason.MANUAL,
            organization_id=_org_id(),
        )
    with pytest.raises(InvariantViolation):
        Suppression(
            id=new_id(),  # type: ignore[arg-type]
            phone_hash="h" * 64,
            scope=SuppressionScope.ORGANIZATION,
            reason=SuppressionReason.MANUAL,
        )


# ---------------------------------------------------------------------------
# Pre-dial compliance gate
# ---------------------------------------------------------------------------


def _gate(**overrides: object) -> object:
    defaults: dict[str, object] = {
        "organization_active": True,
        "campaign_running": True,
        "contact_dialable": True,
        "is_suppressed": False,
        "has_consent": True,
        "attempts_made": 0,
        "max_attempts": 3,
        "now": datetime(2026, 7, 29, 12, 0, tzinfo=IST),
        "window_start": time(9),
        "window_end": time(21),
        "timezone": "Asia/Kolkata",
    }
    defaults.update(overrides)
    return evaluate_dial_eligibility(**defaults)  # type: ignore[arg-type]


def test_gate_allows_a_fully_eligible_contact() -> None:
    assert _gate().allowed  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"organization_active": False}, DialRejection.ORGANIZATION_INACTIVE),
        ({"campaign_running": False}, DialRejection.CAMPAIGN_NOT_RUNNING),
        ({"is_suppressed": True}, DialRejection.SUPPRESSED),
        ({"has_consent": False}, DialRejection.NO_CONSENT),
        ({"contact_dialable": False}, DialRejection.CONTACT_NOT_DIALABLE),
        ({"attempts_made": 3}, DialRejection.ATTEMPTS_EXHAUSTED),
        (
            {"now": datetime(2026, 7, 29, 23, 0, tzinfo=IST)},
            DialRejection.OUTSIDE_CALLING_WINDOW,
        ),
    ],
)
def test_gate_rejects_with_a_reportable_reason(
    override: dict[str, object], expected: DialRejection
) -> None:
    """Each rejection is a countable metric, not a bare False."""
    decision = _gate(**override)
    assert not decision.allowed  # type: ignore[attr-defined]
    assert decision.rejection is expected  # type: ignore[attr-defined]


def test_suppression_outranks_consent() -> None:
    """Someone who asked never to be called again is not un-suppressed by a
    tenant later uploading "consent" for them."""
    decision = _gate(is_suppressed=True, has_consent=True)
    assert decision.rejection is DialRejection.SUPPRESSED  # type: ignore[attr-defined]


def test_consent_requirement_is_configurable_not_hardcoded() -> None:
    """D-3 is unresolved, so this is configuration; deployed environments are
    forbidden from turning it off by the settings validator."""
    assert _gate(has_consent=False, require_consent=False).allowed  # type: ignore[attr-defined]


def test_calling_window_is_configurable_not_hardcoded() -> None:
    """D-4 is unresolved and two different windows appear in secondary sources."""
    late = datetime(2026, 7, 29, 23, 0, tzinfo=IST)
    assert _gate(now=late, window_start=time(22), window_end=time(6)).allowed  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CallStatus.NO_ANSWER, CallStatus.BUSY])
def test_retryable_outcomes_produce_a_next_attempt(status: CallStatus) -> None:
    when = next_retry_at(last_status=status, attempts_made=1, after=now_utc())
    assert when is not None


@pytest.mark.parametrize("status", [CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.CANCELLED])
def test_non_retryable_outcomes_are_not_redialled(status: CallStatus) -> None:
    """Redialling a failure on a schedule is how a platform generates complaints."""
    assert next_retry_at(last_status=status, attempts_made=1, after=now_utc()) is None


def test_backoff_grows_and_is_capped() -> None:
    base = now_utc()
    policy = RetryPolicy(base_delay_minutes=60, multiplier=2.0, max_delay_minutes=180)
    first = next_retry_at(last_status=CallStatus.BUSY, attempts_made=1, after=base, policy=policy)
    second = next_retry_at(last_status=CallStatus.BUSY, attempts_made=2, after=base, policy=policy)
    far = next_retry_at(last_status=CallStatus.BUSY, attempts_made=9, after=base, policy=policy)
    assert first is not None and second is not None and far is not None
    assert first < second
    assert far - base == timedelta(minutes=180)


# ---------------------------------------------------------------------------
# Tenant / platform contexts
# ---------------------------------------------------------------------------


def test_tenant_context_rejects_platform_permissions() -> None:
    with pytest.raises(InvariantViolation):
        TenantContext(organization_id=_org_id(), permissions=frozenset({"platform:call:read"}))


def test_tenant_context_rejects_unknown_permissions() -> None:
    with pytest.raises(InvariantViolation):
        TenantContext(organization_id=_org_id(), permissions=frozenset({"org:made:up"}))


def test_require_raises_and_does_not_name_the_permission_to_the_user() -> None:
    context = TenantContext(organization_id=_org_id(), permissions=frozenset({"org:call:read"}))
    context.require("org:call:read")
    with pytest.raises(AuthorizationError) as caught:
        context.require("org:call:export")
    assert "org:call:export" not in caught.value.message
    assert caught.value.detail["required"] == "org:call:export"


def test_system_actor_is_recorded_honestly() -> None:
    assert TenantContext(organization_id=_org_id()).is_system
    assert not TenantContext(organization_id=_org_id(), actor_id=UserId(new_id())).is_system


def test_platform_context_narrows_to_a_tenant_explicitly() -> None:
    """Entering a tenant is visible in the code, not implied."""
    platform = PlatformContext(permissions=frozenset({"platform:call:read"}))
    org = _org_id()
    narrowed = platform.for_organization(org, frozenset({"org:call:read"}))
    assert isinstance(narrowed, TenantContext)
    assert narrowed.organization_id == org
    assert not narrowed.has("platform:call:read")


# ---------------------------------------------------------------------------
# Permission catalog and events
# ---------------------------------------------------------------------------


def test_permission_scopes_do_not_overlap() -> None:
    assert not (ORG_PERMISSIONS & PLATFORM_PERMISSIONS)
    assert ALL_PERMISSIONS == ORG_PERMISSIONS | PLATFORM_PERMISSIONS


def test_every_permission_is_well_formed() -> None:
    for permission in ALL_PERMISSIONS:
        scope, feature, action = permission.split(":", 2)
        assert scope in {"org", "platform"}
        assert feature and action


def test_outbox_event_rejects_an_unknown_type() -> None:
    with pytest.raises(ValidationError):
        build_outbox_event(event_type="call.exploded", payload={})


@pytest.mark.parametrize("key", ["phone", "transcript", "email", "full_name"])
def test_outbox_payloads_may_not_carry_personal_data(key: str) -> None:
    """An outbox row reaches every downstream integration and is retained."""
    with pytest.raises(ValidationError):
        build_outbox_event(event_type=EventType.CALL_COMPLETED, payload={key: "x"})


def test_outbox_event_accepts_identifiers() -> None:
    event = build_outbox_event(
        event_type=EventType.CALL_COMPLETED,
        payload={"call_id": str(new_id())},
        organization_id=_org_id(),
    )
    assert event.event_type == EventType.CALL_COMPLETED
    assert not event.is_published
