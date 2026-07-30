"""Identifier generation.

Every primary key in this platform is a UUID generated **here**, in the
application, never by the database. Two reasons:

1. The domain needs an entity's identity before anything is flushed. A `Call`
   aggregate builds an outbox event referencing its own id inside the same
   transaction that inserts it.
2. Ids stay stable across the retry of a job that already partially ran, which
   is what makes those jobs idempotent.

We use **UUIDv7** (RFC 9562) everywhere. v7 is time-ordered, so inserts land at
the right-hand edge of the B-tree instead of scattering across it — the
difference between an append-friendly index and a page-split machine on
`calls`, `call_events`, `call_tool_executions`, `outbox` and `audit_logs`.
Using it for configuration tables too costs nothing and keeps one code path.

The generator is `uuid_utils` (Rust, RFC 9562) rather than something hand-rolled
here. Getting the millisecond-boundary monotonic counter right is fiddly and a
subtly wrong implementation produces duplicate or non-monotonic keys under load
— exactly the failure that is invisible until it is a production incident.
`uuid_utils.compat` returns genuine `uuid.UUID` instances, so nothing downstream
needs to know the library exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from uuid_utils import compat as _uuid7

from rn_core.errors import ValidationError

__all__ = ["ID_REGEX", "is_uuid7", "new_id", "parse_id", "timestamp_of"]

#: Canonical textual form. Used by API validation and by CHECK constraints.
ID_REGEX = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

_UNIX_TS_MS_BITS = 48
_UUID_VERSION_7 = 7


def new_id() -> uuid.UUID:
    """Generate a fresh time-ordered UUIDv7."""
    return _uuid7.uuid7()


def parse_id(value: str | uuid.UUID) -> uuid.UUID:
    """Parse an identifier, raising a typed error rather than `ValueError`.

    Used at every trust boundary — request paths, CSV rows, provider callbacks,
    tool arguments. Note this validates *shape only*. It says nothing about
    whether the caller may see the thing it names; that is authorization's job,
    and a well-formed id from an attacker is still an attacker's id.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError("Malformed identifier.", detail={"value": str(value)}) from exc


def is_uuid7(value: uuid.UUID) -> bool:
    """Whether this id carries a v7 version nibble."""
    return value.version == _UUID_VERSION_7


def timestamp_of(value: uuid.UUID) -> datetime:
    """Extract the embedded creation time from a UUIDv7.

    Useful for debugging and for coarse ordering checks. **Not** a substitute for
    a stored `created_at`: the embedded time is generation time on whichever
    machine generated it, with no guarantee of clock agreement across hosts, and
    business timestamps must never depend on that.
    """
    if not is_uuid7(value):
        raise ValidationError(
            "Timestamp can only be extracted from a UUIDv7.",
            detail={"version": value.version},
        )
    millis = value.int >> (128 - _UNIX_TS_MS_BITS)
    return datetime.fromtimestamp(millis / 1000, tz=UTC)
