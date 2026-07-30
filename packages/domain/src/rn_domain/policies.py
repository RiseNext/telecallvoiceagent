"""Pure business policies.

Decisions with no I/O. Everything a policy needs is passed in, which is what
makes these exhaustively testable in milliseconds — and these are the rules that
will change most often, so the tests need to stay fast.

If a policy wants to *look something up*, it does not belong here. Split it: the
pure decision stays, the lookup moves to `rn_services` and passes the result in.

The pre-dial gate below is the compliance-critical one. It returns a structured
decision rather than a boolean because *why* a contact was rejected is a
reportable number — a spike in consent rejections means a tenant uploaded a list
they should not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from rn_core.clock import is_within_window
from rn_domain.enums import CallStatus

__all__ = [
    "DialDecision",
    "DialRejection",
    "RetryPolicy",
    "evaluate_dial_eligibility",
    "next_retry_at",
]


class DialRejection(StrEnum):
    """Why a contact may not be dialled. Each value is a reportable metric.

    Ordering matters at the call site: the cheapest and most absolute checks run
    first, so a suppressed number is never evaluated against a calling window.
    """

    ORGANIZATION_INACTIVE = "organization_inactive"
    SUPPRESSED = "suppressed"
    NO_CONSENT = "no_consent"
    OUTSIDE_CALLING_WINDOW = "outside_calling_window"
    CONTACT_NOT_DIALABLE = "contact_not_dialable"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    CAMPAIGN_NOT_RUNNING = "campaign_not_running"


@dataclass(frozen=True, slots=True)
class DialDecision:
    """The outcome of the pre-dial compliance gate."""

    allowed: bool
    rejection: DialRejection | None = None

    @classmethod
    def allow(cls) -> DialDecision:
        return cls(allowed=True)

    @classmethod
    def reject(cls, rejection: DialRejection) -> DialDecision:
        return cls(allowed=False, rejection=rejection)


# Many returns are deliberate here: a compliance gate is a sequence of independent
# rejections, and the guard-clause chain is the readable form. Collapsing it into
# fewer returns would obscure the ordering, which is itself a security property.
def evaluate_dial_eligibility(  # noqa: PLR0911
    *,
    organization_active: bool,
    campaign_running: bool,
    contact_dialable: bool,
    is_suppressed: bool,
    has_consent: bool,
    attempts_made: int,
    max_attempts: int,
    now: datetime,
    window_start: time,
    window_end: time,
    timezone: str,
    require_consent: bool = True,
    enforce_window: bool = True,
) -> DialDecision:
    """Decide whether one contact may be dialled right now.

    Every input is supplied by the caller — this function performs no lookups, so
    it can be exercised across the whole decision space in a unit test.

    `require_consent` and `enforce_window` exist because both are *configuration*
    (PRD **D-3**, **D-4**): the permitted window could not be confirmed from any
    official source, and who must hold consent evidence is unresolved. They
    default to on, and deployed environments are forbidden from turning them off.

    Note the ordering. Suppression is checked before consent and before the
    window because it is the most absolute: someone who asked never to be called
    again must not be dialled even if a tenant later uploads "consent" for them.
    """
    if not organization_active:
        return DialDecision.reject(DialRejection.ORGANIZATION_INACTIVE)
    if not campaign_running:
        return DialDecision.reject(DialRejection.CAMPAIGN_NOT_RUNNING)
    if is_suppressed:
        return DialDecision.reject(DialRejection.SUPPRESSED)
    if require_consent and not has_consent:
        return DialDecision.reject(DialRejection.NO_CONSENT)
    if not contact_dialable:
        return DialDecision.reject(DialRejection.CONTACT_NOT_DIALABLE)
    if attempts_made >= max_attempts:
        return DialDecision.reject(DialRejection.ATTEMPTS_EXHAUSTED)
    if enforce_window and not is_within_window(
        now, window_start=window_start, window_end=window_end, tz=timezone
    ):
        return DialDecision.reject(DialRejection.OUTSIDE_CALLING_WINDOW)
    return DialDecision.allow()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff for redialling a contact that did not connect.

    Bounded and jitter-free by design: the dispatcher already spreads load
    through its rate limiter, and a deterministic next-attempt time makes the
    scheduler's behaviour reproducible in a test.
    """

    base_delay_minutes: int = 60
    multiplier: float = 2.0
    max_delay_minutes: int = 24 * 60

    def __post_init__(self) -> None:
        from rn_core.errors import InvariantViolation  # noqa: PLC0415

        if self.base_delay_minutes < 1:
            raise InvariantViolation("Retry base delay must be at least one minute.")
        if self.multiplier < 1.0:
            raise InvariantViolation("Retry multiplier must not shrink the delay.")


#: Outcomes worth another attempt. A `FAILED` call is not retried blindly —
#: failure usually means a bad number or a provider rejection, and redialling it
#: on a schedule is how a platform generates complaints.
_RETRYABLE_STATUSES = frozenset({CallStatus.NO_ANSWER, CallStatus.BUSY})


def next_retry_at(
    *,
    last_status: CallStatus,
    attempts_made: int,
    after: datetime,
    policy: RetryPolicy | None = None,
) -> datetime | None:
    """When to try again, or `None` if this outcome should not be retried.

    The caller still has to re-run the full dial gate at that time — a contact
    can be suppressed between the first attempt and the retry.
    """
    if last_status not in _RETRYABLE_STATUSES:
        return None
    effective = policy or RetryPolicy()
    delay = effective.base_delay_minutes * (effective.multiplier ** max(attempts_made - 1, 0))
    return after + timedelta(minutes=min(delay, effective.max_delay_minutes))
