"""Structured logging.

JSON when deployed, human-readable locally, and **redacted in both** — the
redaction processor sits in the pipeline, not at call sites, because call sites
forget (see `rn_core.redaction`).

The processor order matters and is not arbitrary:

1. merge contextvars, so a log call inherits its request/call ids
2. attach correlation fields
3. add level, logger name, ISO-8601 UTC timestamp
4. render any exception into a string field
5. **redact** — last, so it sees everything the earlier steps produced,
   including the rendered traceback
6. render to JSON or console

Redaction must come after exception rendering. A traceback frequently contains
the arguments that caused the failure, which is exactly where a phone number or
a DSN shows up in practice.

One rule the audio path depends on: **logging must never block.** stdlib
handlers writing to stdout are effectively non-blocking against a pipe, but the
media plane still emits at most one line per call lifecycle event, never per
frame. See docs/OBSERVABILITY.md.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from rn_core.correlation import correlation_fields
from rn_core.redaction import redact_mapping

__all__ = ["configure_logging", "get_logger"]


class _State:
    """Module state in an object rather than a `global` rebind."""

    configured: bool = False


_state = _State()


def _add_correlation(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Attach the ambient correlation ids.

    Explicit values already on the event win — a caller naming a different
    `call_id` means it, and silently overwriting would hide the real one.
    """
    for key, value in correlation_fields().items():
        event_dict.setdefault(key, value)
    return event_dict


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Redact the whole event. The last line of defence before rendering."""
    return redact_mapping(event_dict)


def _drop_color_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """uvicorn duplicates `event` into `color_message`; drop the copy.

    Left in place it would double every request line in JSON output — and worse,
    survive redaction review by looking like a different field.
    """
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = False,
    service_name: str | None = None,
) -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent: safe to call from an app entrypoint and from a test fixture.
    Call once at startup, before anything logs.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Everything above may have introduced sensitive values. Nothing below
        # this line is allowed to reintroduce any.
        _redact,
    ]

    if service_name:
        shared.insert(0, _service_binder(service_name))

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    # structlog emits through the *stdlib* logging machinery rather than printing
    # directly. That gives one output path for our logs and for third-party ones
    # (uvicorn, sqlalchemy, alembic), so the redaction processor below applies to
    # both — a library logging a DSN on connection failure is a real and
    # recurring source of leaks. It is also what `add_logger_name` requires:
    # with a print-based factory the logger has no `.name` and the processor
    # raises on the first real log call.
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Applied to records that did NOT come from structlog, so third-party
            # output goes through the same redaction.
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())

    # SQLAlchemy's engine logger is noisy and echoes bound parameters, which for
    # us can be a phone number. Redaction would catch it, but not emitting it at
    # all is cheaper and removes the question.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _state.configured = True


def _service_binder(service_name: str) -> Processor:
    def _bind(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service_name)
        return event_dict

    return _bind


def get_logger(name: str | None = None) -> Any:
    """Get a bound structlog logger.

    Configures with defaults if the application has not done so — a library
    module that logs during import should not crash the process, though every
    entrypoint is expected to call `configure_logging` explicitly.
    """
    if not _state.configured:
        configure_logging()
    return structlog.get_logger(name)
