"""End-to-end logging tests.

These exist because the redaction pipeline was configured correctly and *never
actually invoked* — every earlier test called `redact_text` directly rather than
emitting a log line, so a broken logger factory went unnoticed until an
unrelated integration test happened to log a warning.

So the rule these encode: **assert on captured output, not on the redaction
function.** The function being right is necessary and not sufficient; what
matters is that a real `logger.warning(...)` produces a redacted line.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import pytest

from rn_core.correlation import bind_correlation
from rn_core.logging import configure_logging, get_logger

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    """Logging configuration is process-global; put it back afterwards."""
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])


def _emit(capsys: pytest.CaptureFixture[str], **fields: object) -> dict[str, Any]:
    configure_logging(level="INFO", json_output=True, service_name="rn-test")
    get_logger("rn.test").warning("test.event", **fields)
    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "nothing was logged"
    return cast("dict[str, Any]", json.loads(captured[-1]))


def test_a_real_log_call_emits_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    payload = _emit(capsys, count=3)
    assert payload["event"] == "test.event"
    assert payload["level"] == "warning"
    assert payload["count"] == 3
    assert payload["service"] == "rn-test"
    assert "timestamp" in payload
    assert payload["logger"] == "rn.test"


def test_a_phone_number_never_reaches_the_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headline requirement, asserted through the real pipeline."""
    payload = _emit(capsys, to_number="+919876543210", note="ring +919876543210 back")
    rendered = json.dumps(payload)
    assert "9876543210" not in rendered
    assert payload["to_number"] == "+91XXXXXXXX10"


def test_secrets_never_reach_the_output(capsys: pytest.CaptureFixture[str]) -> None:
    payload = _emit(
        capsys,
        api_key="sk-abcdefghijklmnopqrstuvwxyz01",
        dsn="postgresql://u:hunter2@db/rn",
    )
    rendered = json.dumps(payload)
    assert "sk-abcdefghijklmnopqrstuvwxyz01" not in rendered
    assert "hunter2" not in rendered


def test_correlation_ids_are_attached_automatically(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A log call deep in a service inherits its request and call ids."""
    configure_logging(level="INFO", json_output=True)
    with bind_correlation(request_id="req-1", organization_id="org-1", call_id="call-1"):
        get_logger("rn.test").warning("inside.request")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["request_id"] == "req-1"
    assert payload["organization_id"] == "org-1"
    assert payload["call_id"] == "call-1"


def test_explicit_fields_win_over_ambient_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Silently overwriting a caller's explicit value would hide the real one."""
    configure_logging(level="INFO", json_output=True)
    with bind_correlation(call_id="ambient"):
        get_logger("rn.test").warning("event", call_id="explicit")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["call_id"] == "explicit"


def test_exception_tracebacks_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """A traceback frequently contains the arguments that caused the failure.

    That is exactly where a phone number shows up in practice, which is why
    redaction runs *after* exception rendering in the processor chain.
    """
    configure_logging(level="INFO", json_output=True)
    try:
        raise ValueError("failed while dialling +919876543210")
    except ValueError:
        get_logger("rn.test").exception("call.failed")
    rendered = capsys.readouterr().out
    assert "9876543210" not in rendered


def test_third_party_stdlib_logs_are_redacted_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Libraries log DSNs on connection failure. They go through our pipeline."""
    configure_logging(level="INFO", json_output=True)
    logging.getLogger("some.library").warning(
        "connection failed: postgresql://u:hunter2@db/rn for +919876543210"
    )
    rendered = capsys.readouterr().out
    assert "hunter2" not in rendered
    assert "9876543210" not in rendered


def test_console_format_also_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    """Local development gets readable output, not unredacted output."""
    configure_logging(level="INFO", json_output=False)
    get_logger("rn.test").warning("local.event", to_number="+919876543210")
    rendered = capsys.readouterr().out
    assert "9876543210" not in rendered


def test_level_filtering_is_applied(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", json_output=True)
    logger = get_logger("rn.test")
    logger.info("should.not.appear")
    logger.warning("should.appear")
    lines = [line for line in capsys.readouterr().out.strip().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "should.appear"
