"""The application error taxonomy.

Every failure that crosses a layer boundary is one of these. They are structured
rather than string-formatted so that the API can map them to status codes, the
worker can decide whether to retry, and a tool can hand the model something it
can talk about — all from the same object, without any of those layers parsing
a message.

`rn_core` knows nothing about HTTP. There is no status code here on purpose:
mapping to a transport belongs to the app that speaks that transport.

Two rules that matter more than the class list:

1. **`message` is safe to show a user; `detail` is not.** Anything derived from
   a database error, a provider response body or a stack trace goes in `detail`,
   which is logged and never serialised into a user-facing payload.
2. **Never raise a bare `Exception`** and never let a driver exception escape a
   repository. Wrap it, so the layer above can branch on meaning rather than on
   the vendor of the thing that broke.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ApplicationError",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConflictError",
    "ErrorCode",
    "InvariantViolation",
    "NotFoundError",
    "ProviderError",
    "RateLimitError",
    "TransientError",
    "ValidationError",
]


class ErrorCode:
    """Stable machine-readable codes.

    Clients and dashboards branch on these, so they are part of our contract:
    add freely, never rename, never reuse.
    """

    VALIDATION = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    AUTHENTICATION = "authentication_error"
    AUTHORIZATION = "authorization_error"
    INVARIANT = "invariant_violation"
    CONFIGURATION = "configuration_error"
    PROVIDER = "provider_error"
    RATE_LIMIT = "rate_limit_exceeded"
    TRANSIENT = "transient_error"
    INTERNAL = "internal_error"


class ApplicationError(Exception):
    """Base class for every error this platform raises deliberately.

    Args:
        message: Safe to surface to a user. No identifiers of other tenants, no
            provider internals, no SQL.
        code: A stable `ErrorCode`.
        detail: Diagnostic context for logs. May contain sensitive values, so it
            is never serialised to a client. Redaction still applies when logged.
        retryable: Whether retrying the *same* operation could plausibly succeed.
            Jobs use this; do not set it true for anything non-idempotent.
    """

    code: str = ErrorCode.INTERNAL
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.detail: dict[str, Any] = detail or {}

    def to_dict(self) -> dict[str, Any]:
        """The safe projection. Deliberately excludes `detail`."""
        return {"code": self.code, "message": self.message}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ValidationError(ApplicationError):
    """Input failed validation. The caller can fix it and retry."""

    code = ErrorCode.VALIDATION


class NotFoundError(ApplicationError):
    """The resource does not exist **or is not visible to this tenant**.

    Deliberately indistinguishable from "exists but belongs to someone else":
    telling an attacker which of the two it is leaks the existence of another
    tenant's data. Repositories must raise this, never a "forbidden", when a
    tenant-scoped lookup misses.
    """

    code = ErrorCode.NOT_FOUND


class ConflictError(ApplicationError):
    """The operation conflicts with current state — duplicate key, lost update,
    or a state transition that is not legal from where the entity is now."""

    code = ErrorCode.CONFLICT


class AuthenticationError(ApplicationError):
    """We do not know who this is. Distinct from authorization on purpose."""

    code = ErrorCode.AUTHENTICATION


class AuthorizationError(ApplicationError):
    """We know who this is and they may not do this."""

    code = ErrorCode.AUTHORIZATION


# Named for what it is rather than for the N818 suffix convention:
# "InvariantViolation" is the term the architecture docs use, and
# "InvariantViolationError" reads as a failure to violate an invariant.
class InvariantViolation(ApplicationError):  # noqa: N818
    """A domain rule that should have been impossible to break was broken.

    This is a bug, not a user error. It means an entity was constructed or
    mutated into a state the domain says cannot exist.
    """

    code = ErrorCode.INVARIANT


class ConfigurationError(ApplicationError):
    """Configuration is missing, malformed, or unsafe for this environment.

    Raised at startup. The process must not continue — a service running with
    half its configuration is worse than one that refused to boot.
    """

    code = ErrorCode.CONFIGURATION


class ProviderError(ApplicationError):
    """An external system failed.

    The provider's own message goes in `detail`, never in `message` — provider
    errors routinely echo request contents, which for us can mean a phone number.
    """

    code = ErrorCode.PROVIDER


class RateLimitError(ProviderError):
    """A rate limit was hit, ours or theirs."""

    code = ErrorCode.RATE_LIMIT
    retryable = True


class TransientError(ApplicationError):
    """Failed for a reason that may not recur — a timeout, a dropped connection.

    Retryable **only** when the operation itself is idempotent. Raising this does
    not make an operation safe to repeat; that is the caller's judgement.
    """

    code = ErrorCode.TRANSIENT
    retryable = True
