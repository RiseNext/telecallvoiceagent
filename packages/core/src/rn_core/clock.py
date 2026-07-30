"""Time handling.

Two rules, and every bug this module exists to prevent comes from breaking one:

1. **Storage is UTC. Always.** `timestamptz` in Postgres, timezone-aware
   `datetime` in Python. IST is a rendering and business-rule concern, never a
   storage format. ruff's `DTZ` ruleset enforces the Python half.
2. **Naive datetimes do not exist here.** A naive datetime in a dialler that
   obeys calling windows is not a style problem, it is a compliance incident
   waiting for the first daylight-saving boundary or containerised timezone.

Note the separation between *instants* and *local intent*. "Friday evening" is
not an instant — it is a wall-clock intent in someone's timezone. Storing only
the resolved instant loses the information needed to reschedule correctly if the
zone's rules change, so callers store both (`scheduled_at`, `scheduled_tz`).
`resolve_local_time` exists to make that pairing the easy path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rn_core.errors import InvariantViolation, ValidationError

__all__ = [
    "IST",
    "ensure_utc",
    "is_within_window",
    "now_utc",
    "resolve_local_time",
    "to_timezone",
    "zone",
]

#: India Standard Time. The default business timezone, not a storage timezone.
IST = ZoneInfo("Asia/Kolkata")


def now_utc() -> datetime:
    """The current instant, timezone-aware, in UTC.

    The single source of "now" for application code. Tests freeze this rather
    than patching `datetime` globally.

    Not for call timing. `started_at`/`answered_at`/`ended_at` come from measured
    monotonic clocks in the media plane — wall-clock time can step backwards, and
    a negative call duration is a real bug this rule prevents.
    """
    return datetime.now(UTC)


def zone(name: str) -> ZoneInfo:
    """Look up an IANA timezone, failing with a typed error.

    Organization timezones are user-supplied configuration, so an unknown zone is
    a validation failure rather than a crash somewhere later.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValidationError("Unknown timezone.", detail={"timezone": name}) from exc


def ensure_utc(value: datetime) -> datetime:
    """Return `value` in UTC, refusing naive datetimes.

    Deliberately does **not** assume naive means UTC. That assumption is how a
    campaign ends up dialling at 04:00 local: it is silently wrong exactly when
    the process timezone is not what the author imagined.
    """
    if value.tzinfo is None:
        raise InvariantViolation(
            "Naive datetime crossed a boundary that requires an instant.",
            detail={"value": value.isoformat()},
        )
    return value.astimezone(UTC)


def to_timezone(value: datetime, tz: ZoneInfo | str) -> datetime:
    """Render an instant in a timezone. Presentation and business rules only."""
    target = zone(tz) if isinstance(tz, str) else tz
    return ensure_utc(value).astimezone(target)


def resolve_local_time(
    day: date,
    local_time: time,
    tz: ZoneInfo | str,
) -> tuple[datetime, str]:
    """Resolve a wall-clock intent into `(instant_utc, iana_zone_name)`.

    Callers persist both halves. Keeping the zone alongside the instant is what
    lets a later reschedule or a timezone-rule change be handled correctly
    instead of silently moving someone's appointment.

    Ambiguous and non-existent local times (DST transitions) resolve via Python's
    documented `fold` behaviour. India does not observe DST, so this matters for
    future non-IST tenants rather than today.
    """
    target = zone(tz) if isinstance(tz, str) else tz
    local = datetime.combine(day, local_time, tzinfo=target)
    return local.astimezone(UTC), str(target)


def is_within_window(
    instant: datetime,
    *,
    window_start: time,
    window_end: time,
    tz: ZoneInfo | str,
) -> bool:
    """Whether `instant` falls inside a daily local wall-clock window.

    The permitted calling window is regulatory, unconfirmed (PRD **D-4**), and
    therefore *configuration* — this function takes the bounds as arguments and
    hardcodes nothing. Windows that wrap midnight are supported.
    """
    local = to_timezone(instant, tz).timetz().replace(tzinfo=None)
    if window_start <= window_end:
        return window_start <= local <= window_end
    return local >= window_start or local <= window_end


def utc_days_ago(days: int) -> datetime:
    """Convenience for retention and recency checks."""
    return now_utc() - timedelta(days=days)
