"""RiseNext shared kernel — configuration, errors, IDs, time, structured logging, telemetry, redaction.

The bottom of the layer graph. Everything depends on this package; it depends on
nothing internal and performs no I/O beyond writing log lines and exporting
spans. See packages/core/README.md for what does and does not belong here.
"""

from rn_core.clock import IST, ensure_utc, is_within_window, now_utc, to_timezone, zone
from rn_core.correlation import (
    CorrelationContext,
    bind_correlation,
    correlation_fields,
    get_correlation,
    new_request_id,
)
from rn_core.errors import (
    ApplicationError,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConflictError,
    ErrorCode,
    InvariantViolation,
    NotFoundError,
    ProviderError,
    RateLimitError,
    TransientError,
    ValidationError,
)
from rn_core.ids import new_id, parse_id
from rn_core.logging import configure_logging, get_logger
from rn_core.redaction import mask_phone, redact_mapping, redact_text
from rn_core.settings import Environment, Settings, get_settings
from rn_core.telemetry import configure_telemetry, start_span

__version__ = "0.1.0"

__all__ = [
    "IST",
    "ApplicationError",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConflictError",
    "CorrelationContext",
    "Environment",
    "ErrorCode",
    "InvariantViolation",
    "NotFoundError",
    "ProviderError",
    "RateLimitError",
    "Settings",
    "TransientError",
    "ValidationError",
    "bind_correlation",
    "configure_logging",
    "configure_telemetry",
    "correlation_fields",
    "ensure_utc",
    "get_correlation",
    "get_logger",
    "get_settings",
    "is_within_window",
    "mask_phone",
    "new_id",
    "new_request_id",
    "now_utc",
    "parse_id",
    "redact_mapping",
    "redact_text",
    "start_span",
    "to_timezone",
    "zone",
]
