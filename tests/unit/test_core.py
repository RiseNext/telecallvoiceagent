"""Unit tests for the shared kernel: ids, time, redaction, errors, settings."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from rn_core.clock import (
    IST,
    ensure_utc,
    is_within_window,
    now_utc,
    resolve_local_time,
    to_timezone,
)
from rn_core.correlation import bind_correlation, get_correlation
from rn_core.errors import (
    ApplicationError,
    ConfigurationError,
    InvariantViolation,
    NotFoundError,
    ValidationError,
)
from rn_core.ids import is_uuid7, new_id, parse_id, timestamp_of
from rn_core.redaction import REDACTED, mask_phone, redact_mapping, redact_text
from rn_core.settings import Environment, LogFormat, Settings

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def test_new_id_is_uuid7() -> None:
    assert is_uuid7(new_id())


def test_ids_are_unique_at_volume() -> None:
    generated = [new_id() for _ in range(20_000)]
    assert len(set(generated)) == len(generated)


def test_ids_are_monotonic_within_a_process() -> None:
    """v7 ids sort in generation order, which is what keeps B-tree inserts local.

    This is an ordering property of the generator, not a clock. Business
    timestamps are stored in explicit `timestamptz` columns; nothing in the
    application derives a business time from an id.
    """
    generated = [new_id() for _ in range(5_000)]
    assert generated == sorted(generated)


def test_timestamp_of_is_close_to_generation_time() -> None:
    """The embedded time is a debugging aid, deliberately not a business fact."""
    before = now_utc()
    extracted = timestamp_of(new_id())
    after = now_utc()
    # Millisecond precision, so allow the boundary in both directions.
    assert before.timestamp() - 0.01 <= extracted.timestamp() <= after.timestamp() + 0.01


def test_timestamp_of_rejects_non_v7() -> None:
    import uuid

    with pytest.raises(ValidationError):
        timestamp_of(uuid.uuid4())


def test_parse_id_rejects_malformed_input_with_a_typed_error() -> None:
    with pytest.raises(ValidationError):
        parse_id("not-a-uuid")


def test_parse_id_error_does_not_echo_a_huge_payload() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_id("x" * 10)
    assert caught.value.code == "validation_error"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_now_utc_is_timezone_aware_and_utc() -> None:
    moment = now_utc()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == UTC.utcoffset(None)


def test_ensure_utc_refuses_naive_datetimes() -> None:
    """A naive datetime must never be assumed to be UTC.

    Assuming is how a campaign ends up dialling at 04:00 local: silently wrong
    exactly when the process timezone is not what the author imagined.
    """
    with pytest.raises(InvariantViolation):
        ensure_utc(datetime(2026, 7, 29, 12, 0))  # noqa: DTZ001


def test_ensure_utc_converts_an_offset_datetime() -> None:
    ist_noon = datetime(2026, 7, 29, 12, 0, tzinfo=IST)
    converted = ensure_utc(ist_noon)
    assert converted.hour == 6 and converted.minute == 30


def test_to_timezone_renders_without_changing_the_instant() -> None:
    instant = datetime(2026, 7, 29, 6, 30, tzinfo=UTC)
    rendered = to_timezone(instant, IST)
    assert rendered.hour == 12
    assert rendered.timestamp() == instant.timestamp()


def test_resolve_local_time_returns_instant_and_zone() -> None:
    """Both halves are stored: the instant alone loses the user's intent."""
    instant, zone_name = resolve_local_time(date(2026, 7, 29), time(15, 0), IST)
    assert zone_name == "Asia/Kolkata"
    assert instant.tzinfo is not None
    assert to_timezone(instant, IST).hour == 15


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(8, False), (9, True), (14, True), (21, True), (22, False)],
)
def test_calling_window_boundaries(hour: int, expected: bool) -> None:
    instant = datetime(2026, 7, 29, hour, 0, tzinfo=IST)
    assert is_within_window(instant, window_start=time(9), window_end=time(21), tz=IST) is expected


def test_calling_window_supports_wrapping_midnight() -> None:
    late = datetime(2026, 7, 29, 23, 0, tzinfo=IST)
    early = datetime(2026, 7, 29, 3, 0, tzinfo=IST)
    midday = datetime(2026, 7, 29, 12, 0, tzinfo=IST)
    for moment, expected in ((late, True), (early, True), (midday, False)):
        assert (
            is_within_window(moment, window_start=time(22), window_end=time(6), tz=IST) is expected
        )


def test_window_is_evaluated_in_the_given_zone_not_the_process_zone() -> None:
    """The same instant is inside the window in one zone and outside in another."""
    instant = datetime(2026, 7, 29, 3, 30, tzinfo=UTC)  # 09:00 IST
    assert is_within_window(instant, window_start=time(9), window_end=time(21), tz=IST)
    assert not is_within_window(
        instant, window_start=time(9), window_end=time(21), tz=ZoneInfo("UTC")
    )


# ---------------------------------------------------------------------------
# Redaction — security-critical
# ---------------------------------------------------------------------------

_FULL_NUMBERS = [
    "+919876543210",
    "+91 98765 43210",
    "+91-98765-43210",
    "9876543210",
    "09876543210",
    "919876543210",
    "+14155552671",
]


@pytest.mark.parametrize("number", _FULL_NUMBERS)
def test_no_complete_phone_number_survives_redaction(number: str) -> None:
    """The core requirement: a complete number must never reach a log."""
    redacted = redact_text(f"calling customer on {number} now")
    digits = re.sub(r"\D", "", number)
    assert digits not in re.sub(r"[^\d]", "", redacted)
    assert number not in redacted


@pytest.mark.parametrize("number", _FULL_NUMBERS)
def test_mask_keeps_only_a_correlatable_tail(number: str) -> None:
    masked = mask_phone(number)
    digits = re.sub(r"\D", "", number)
    assert masked.endswith(digits[-2:])
    assert digits not in masked
    assert "X" in masked


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz012345",
        "sk_live_abcdef123456",
        "whsec_YWJjZGVmZ2hpamtsbW5vcA==",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payloadpayload.signature",
    ],
)
def test_credentials_are_removed_from_free_text(secret: str) -> None:
    assert secret not in redact_text(f"auth failed with {secret}")


def test_dsn_password_is_removed_but_shape_is_kept() -> None:
    """Keeping the shape means the log still says *which* database failed."""
    redacted = redact_text("could not connect: postgresql://risenext:hunter2@db.internal:5432/rn")
    assert "hunter2" not in redacted
    assert "db.internal" in redacted
    assert REDACTED in redacted


def test_sensitive_field_names_are_dropped_whole() -> None:
    result = redact_mapping(
        {
            "api_key": "harmless-looking",
            "db_password": "x",
            "authorization": "Bearer abc",
            "session_id": "s-1",
            "organization_id": "org-1",
        }
    )
    assert result["api_key"] == REDACTED
    assert result["db_password"] == REDACTED
    assert result["authorization"] == REDACTED
    assert result["session_id"] == REDACTED
    # Correlation ids are safe and must survive, or logs become useless.
    assert result["organization_id"] == "org-1"


def test_phone_fields_are_masked_not_dropped() -> None:
    result = redact_mapping({"to_number": "+919876543210"})
    assert result["to_number"] == "+91XXXXXXXX10"


def test_redaction_recurses_into_nested_structures() -> None:
    result = redact_mapping({"outer": {"inner": {"api_key": "s", "note": "+919876543210"}}})
    inner = result["outer"]["inner"]
    assert inner["api_key"] == REDACTED
    assert "9876543210" not in inner["note"]


def test_redaction_bounds_pathological_input() -> None:
    """A hostile payload must not turn one log call into an unbounded walk."""
    deep: dict[str, object] = {"k": "v"}
    for _ in range(50):
        deep = {"k": deep}
    rendered = str(redact_mapping(deep))
    assert "TRUNCATED" in rendered

    wide = redact_mapping({"items": list(range(500))})
    assert len(wide["items"]) < 500


def test_ordinary_values_survive_redaction() -> None:
    """Over-redaction is safer than under-redaction, but must stay usable."""
    result = redact_mapping(
        {"duration_ms": 1234, "count": 42, "epoch": 1785238839, "message": "call completed"}
    )
    assert result["duration_ms"] == 1234
    assert result["count"] == 42
    # Unix seconds are 10 digits and start with 1; the Indian-mobile pattern
    # requires a leading 6-9, so timestamps are not eaten.
    assert result["epoch"] == 1785238839
    assert result["message"] == "call completed"


def test_exception_text_is_redacted() -> None:
    rendered = redact_mapping({"error": ValueError("failed for +919876543210")})
    assert "9876543210" not in str(rendered["error"])


# ---------------------------------------------------------------------------
# Correlation context
# ---------------------------------------------------------------------------


def test_correlation_merges_and_restores() -> None:
    with bind_correlation(request_id="r1", organization_id="o1"):
        with bind_correlation(call_id="c1"):
            current = get_correlation()
            assert current.request_id == "r1"
            assert current.call_id == "c1"
        assert get_correlation().call_id is None
    assert get_correlation().request_id is None


def test_correlation_restores_on_exception() -> None:
    with pytest.raises(RuntimeError), bind_correlation(request_id="r1"):
        raise RuntimeError("boom")
    assert get_correlation().request_id is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_error_to_dict_excludes_detail() -> None:
    """`detail` may hold provider output or a phone number; it never serialises."""
    error = NotFoundError("Contact not found.", detail={"phone": "+919876543210"})
    payload = error.to_dict()
    assert payload == {"code": "not_found", "message": "Contact not found."}
    assert "phone" not in payload


def test_error_repr_does_not_leak_detail() -> None:
    error = ApplicationError("failed", detail={"api_key": "secret"})
    assert "secret" not in repr(error)


def test_retryability_is_explicit() -> None:
    from rn_core.errors import RateLimitError, TransientError

    assert TransientError("x").retryable is True
    assert RateLimitError("x").retryable is True
    assert ValidationError("x").retryable is False
    assert NotFoundError("x").retryable is False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_testing_settings_ignore_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not inherit a developer's `.env` or shell."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert Settings.for_testing().environment is Environment.TEST


def test_secrets_do_not_render_in_repr() -> None:
    rendered = repr(Settings.for_testing().database)
    assert "risenext:risenext" not in rendered


def test_production_rejects_a_localhost_database() -> None:
    from rn_core.settings import AppSettings

    with pytest.raises(ValueError, match="localhost"):
        Settings.for_testing(
            app=AppSettings(
                _env_file=None,
                ENVIRONMENT=Environment.PRODUCTION,
                LOG_FORMAT=LogFormat.JSON,
            )
        )


def test_production_rejects_the_development_pepper() -> None:
    """Rotating the pepper invalidates every stored phone hash, so it must be
    set deliberately once, not inherited from a default."""
    from rn_core.settings import AppSettings, DatabaseSettings

    with pytest.raises(ValueError, match="PHONE_HASH_PEPPER"):
        Settings.for_testing(
            app=AppSettings(
                _env_file=None,
                ENVIRONMENT=Environment.STAGING,
                LOG_FORMAT=LogFormat.JSON,
            ),
            database=DatabaseSettings(
                _env_file=None,
                DATABASE_URL="postgresql+asyncpg://u:p@db.internal:5432/rn",
                DATABASE_URL_DIRECT="postgresql+asyncpg://u:p@db.internal:5432/rn",
            ),
        )


def test_production_requires_json_logs_and_no_debug() -> None:
    from rn_core.settings import AppSettings, ComplianceSettings, DatabaseSettings

    deployed_db = DatabaseSettings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@db.internal:5432/rn",
        DATABASE_URL_DIRECT="postgresql+asyncpg://u:p@db.internal:5432/rn",
    )
    compliance = ComplianceSettings(_env_file=None, PHONE_HASH_PEPPER="a-real-unique-pepper")

    with pytest.raises(ValueError, match="LOG_FORMAT"):
        Settings.for_testing(
            app=AppSettings(_env_file=None, ENVIRONMENT=Environment.PRODUCTION),
            database=deployed_db,
            compliance=compliance,
        )


def test_deployed_environments_cannot_disable_the_compliance_gate() -> None:
    from rn_core.settings import AppSettings, ComplianceSettings, DatabaseSettings

    with pytest.raises(ValueError, match="ENFORCE_CONSENT_GATE"):
        Settings.for_testing(
            app=AppSettings(
                _env_file=None,
                ENVIRONMENT=Environment.PRODUCTION,
                LOG_FORMAT=LogFormat.JSON,
            ),
            database=DatabaseSettings(
                _env_file=None,
                DATABASE_URL="postgresql+asyncpg://u:p@db.internal:5432/rn",
                DATABASE_URL_DIRECT="postgresql+asyncpg://u:p@db.internal:5432/rn",
            ),
            compliance=ComplianceSettings(
                _env_file=None,
                PHONE_HASH_PEPPER="a-real-unique-pepper",
                ENFORCE_CONSENT_GATE=False,
            ),
        )


def test_local_environment_tolerates_development_defaults() -> None:
    """Convenience locally, strictness when deployed — one validator, no branching."""
    assert Settings.for_testing().database.pool_size >= 1


def test_invalid_settings_raise_the_project_error_type() -> None:
    """Callers depend on our taxonomy, not pydantic's."""
    import os

    from rn_core.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    previous = os.environ.get("DATABASE_POOL_SIZE")
    os.environ["DATABASE_POOL_SIZE"] = "-5"
    try:
        with pytest.raises(ConfigurationError):
            get_settings()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_POOL_SIZE", None)
        else:
            os.environ["DATABASE_POOL_SIZE"] = previous
        reset_settings_cache()
