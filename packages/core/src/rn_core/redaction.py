"""Centralised redaction for logs, spans and error detail.

The design premise: **developers will forget.** Any scheme that depends on
remembering to wrap a value at the call site fails the first time someone logs
an exception whose message happens to contain a request body. So redaction is a
processor in the logging pipeline and a helper on the span path — applied to
everything, by default, whether or not the author thought about it.

Two mechanisms, because neither is sufficient alone:

* **Key-based** — a field *named* like a secret is replaced wholesale. Catches
  structured values regardless of their shape.
* **Pattern-based** — free text is scrubbed for things that *look* like secrets
  or phone numbers. Catches the exception messages and provider payloads that
  key-based redaction cannot see into.

On phone numbers specifically: PRD and SECURITY require that a complete number
never reaches a log. We mask rather than drop, because support genuinely needs
to correlate "which call" — `+91XXXXXXXX21` keeps the country and the last two
digits, which is enough to match against a record someone is already authorised
to see, and useless as a contact detail on its own.

Deliberate bias: **over-redact rather than under-redact.** A log line missing a
value costs an engineer a few minutes; a leaked customer number is a breach.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "REDACTED",
    "mask_phone",
    "redact_mapping",
    "redact_text",
    "redact_value",
]

REDACTED = "[REDACTED]"

_MAX_DEPTH = 6
_MAX_SEQUENCE = 50

# Shorter than this and there is nothing safe left to show after masking.
_MIN_MASKABLE_DIGITS = 7
# An Indian national mobile number, without country code.
_NATIONAL_DIGITS = 10

# Field names whose *value* is replaced entirely, matched case-insensitively as a
# substring so `x_api_key`, `db_password` and `AUTHORIZATION` are all covered.
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "cookie",
    "session_id",
    "credential",
    "private_key",
    "signing_key",
    "signature",
    "dsn",
    "database_url",
    "connection_string",
    "webhook_secret",
    "client_secret",
    "access_key",
    "secret_key",
)

# Field names that hold a phone number and should be masked rather than dropped.
_PHONE_KEY_PARTS: tuple[str, ...] = (
    "phone",
    "msisdn",
    "caller_id",
    "from_number",
    "to_number",
    "e164",
    "callerid",
)


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# Ordered: credentials first, then phone numbers. A DSN password that also looks
# like digits must be caught as a credential, not partially masked as a number.
_CREDENTIAL_PATTERNS: list[re.Pattern[str]] = _compile(
    [
        # scheme://user:password@host  -> keep the shape, drop the password
        r"(?P<prefix>[a-z][a-z0-9+.\-]*://[^:/\s@]+:)(?P<secret>[^@\s]+)(?=@)",
        r"\bBearer\s+[A-Za-z0-9._\-]{8,}",
        r"\bBasic\s+[A-Za-z0-9+/=]{8,}",
        r"\bsk-[A-Za-z0-9_\-]{16,}",
        r"\bsk_(?:live|test)_[A-Za-z0-9]{8,}",
        r"\bpk_(?:live|test)_[A-Za-z0-9]{8,}",
        r"\bwhsec_[A-Za-z0-9+/=]{8,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+",  # JWT
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    ]
)

# Phone shapes. Anchored deliberately narrowly so that ordinary integers —
# durations, counts, unix timestamps — survive.
#
# A bare 10-digit run is only treated as a number when it starts 6-9, which is
# the Indian mobile range. Unix epoch seconds start with 1 until 2033, so this
# does not eat timestamps.
_PHONE_PATTERNS: list[re.Pattern[str]] = _compile(
    [
        r"\+\d[\d\s\-().]{6,18}\d",  # international, any separators
        r"\b(?:0091|91)[6-9]\d{9}\b",  # India with country prefix, no plus
        r"\b0[6-9]\d{9}\b",  # India, national trunk prefix
        r"\b[6-9]\d{9}\b",  # India, bare mobile
    ]
)


def mask_phone(value: str) -> str:
    """Mask a phone number, preserving country prefix and the last two digits.

    `+919876543210` -> `+91XXXXXXXX10`. Short or unparseable values are replaced
    entirely rather than partially exposed.
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) < _MIN_MASKABLE_DIGITS:
        return REDACTED
    plus = "+" if value.strip().startswith("+") else ""
    # Keep a country-code-ish prefix only when the value carried one.
    head = digits[:2] if (plus or len(digits) > _NATIONAL_DIGITS) else ""
    tail = digits[-2:]
    hidden = "X" * max(len(digits) - len(head) - len(tail), 1)
    return f"{plus}{head}{hidden}{tail}"


def _mask_phones_in_text(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        return mask_phone(match.group(0))

    for pattern in _PHONE_PATTERNS:
        text = pattern.sub(_sub, text)
    return text


def redact_text(text: str) -> str:
    """Scrub credentials and phone numbers out of free text.

    Applied to log event names, exception messages and any string value — the
    places where sensitive data arrives without a helpful field name attached.
    """
    for pattern in _CREDENTIAL_PATTERNS:
        if "prefix" in pattern.groupindex:
            text = pattern.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return _mask_phones_in_text(text)


def _key_matches(key: str, parts: tuple[str, ...]) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in parts)


def redact_value(key: str, value: Any, *, _depth: int = 0) -> Any:
    """Redact one structured field, recursing into containers.

    Key-based rules win over pattern-based ones: a field named `api_key` is
    dropped whole even if its value looks harmless, because the name is better
    evidence than the shape.
    """
    if _key_matches(key, _SENSITIVE_KEY_PARTS):
        return REDACTED
    if _key_matches(key, _PHONE_KEY_PARTS) and isinstance(value, str):
        return mask_phone(value)
    return _redact_any(value, _depth=_depth)


def _redact_any(value: Any, *, _depth: int = 0) -> Any:
    # Depth and width limits keep a pathological payload from turning a log call
    # into an unbounded walk. Truncation is visible, never silent.
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED:depth]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value, _depth=_depth + 1)
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_SEQUENCE]
        rendered: list[Any] = [_redact_any(item, _depth=_depth + 1) for item in items]
        if len(value) > _MAX_SEQUENCE:
            rendered.append(f"[TRUNCATED:{len(value) - _MAX_SEQUENCE} more]")
        return rendered
    if isinstance(value, BaseException):
        return redact_text(f"{type(value).__name__}: {value}")
    return value


def redact_mapping(data: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Redact every field of a mapping. The entry point for the log processor."""
    if _depth > _MAX_DEPTH:
        return {"_": "[TRUNCATED:depth]"}
    return {str(k): redact_value(str(k), v, _depth=_depth) for k, v in data.items()}
