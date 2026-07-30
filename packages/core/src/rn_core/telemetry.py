"""OpenTelemetry bootstrap.

Phase 1 needs the *foundation*, not dashboards: a tracer that application code
can call unconditionally, a span helper that cannot leak PII, and an exporter
that is off by default. Retrofitting instrumentation later is what produces
systems nobody can debug, so the seam goes in now even though almost nothing
uses it yet.

Three decisions worth knowing:

**Off by default.** `OTEL_ENABLED` defaults to false. With no SDK configured the
OTel API installs a no-op tracer, so `start_span` costs almost nothing and every
call site stays unconditional. Tests and local development get zero network
dependency.

**Exports to our own collector.** The endpoint is ours, not a SaaS. Call spans
carry organization and call identifiers, and PRD **D-1** (data residency) is
unresolved — routing that to a third party by default would decide it by
accident.

**Attributes are opaque identifiers only.** `safe_attributes` filters through the
same redaction pipeline as logs and drops anything that is not a scalar. No
transcript text, no phone numbers, no tool arguments, no audio. A span attribute
is as public as a log line and is retained longer.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

from rn_core.correlation import correlation_fields
from rn_core.redaction import redact_value

__all__ = ["configure_telemetry", "get_tracer", "safe_attributes", "start_span"]


class _State:
    """Module state in an object rather than a `global` rebind."""

    configured: bool = False


_state = _State()

#: Attribute keys that must never be set, whatever a caller passes. Redaction
#: would mangle most of these anyway; refusing them outright is clearer.
_FORBIDDEN_ATTRIBUTE_PARTS = (
    "transcript",
    "audio",
    "utterance",
    "content",
    "prompt",
    "instructions",
    "payload",
    "body",
    "arguments",
    "result",
    "name",  # customer names; entity names go in a purpose-named attribute
)


def configure_telemetry(
    *,
    enabled: bool,
    service_name: str,
    endpoint: str | None = None,
    sample_ratio: float = 1.0,
    environment: str | None = None,
) -> None:
    """Install the tracer provider. Idempotent; safe to skip entirely.

    When `enabled` is false this does nothing at all, leaving the API's no-op
    implementation in place.

    Raises:
        ConfigurationError: if an endpoint is configured but the OTLP exporter is
            not installed. Install `rn-core[otlp]`. Failing loudly beats booting
            with telemetry silently disabled — a service you believe is traced
            and is not is worse than one you know is not.
    """
    if _state.configured or not enabled:
        return

    # Imported lazily: the SDK and OTLP exporter are only needed when telemetry
    # is switched on, and the media plane should not pay their import cost.
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.sampling import (  # noqa: PLC0415
        ParentBased,
        TraceIdRatioBased,
    )

    attributes: dict[str, Any] = {"service.name": service_name}
    if environment:
        attributes["deployment.environment"] = environment

    provider = TracerProvider(
        resource=Resource.create(attributes),
        sampler=ParentBased(root=TraceIdRatioBased(sample_ratio)),
    )

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter,
            )
        except ImportError as exc:  # pragma: no cover - depends on install extras
            from rn_core.errors import ConfigurationError  # noqa: PLC0415

            raise ConfigurationError(
                "OTEL export is enabled but the OTLP exporter is not installed. "
                "Install the 'otlp' extra of rn-core.",
                detail={"endpoint": endpoint},
            ) from exc

        # Batched, so span export never blocks the thread that created the span.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

    trace.set_tracer_provider(provider)
    _state.configured = True


def get_tracer(name: str = "rn") -> Tracer:
    """A tracer. Returns a no-op tracer when telemetry is not configured."""
    return trace.get_tracer(name)


def safe_attributes(values: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    """Filter and redact attributes so a span cannot carry PII.

    Rejects keys that name content, drops non-scalars (a nested structure on a
    span is both useless and a leak risk), and redacts what remains.
    """
    safe: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(part in lowered for part in _FORBIDDEN_ATTRIBUTE_PARTS):
            continue
        if value is None:
            continue
        cleaned = redact_value(key, value)
        if isinstance(cleaned, bool | int | float):
            safe[key] = cleaned
        elif isinstance(cleaned, str):
            safe[key] = cleaned[:256]
    return safe


@contextmanager
def start_span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a span carrying the ambient correlation ids plus safe attributes."""
    merged = {**correlation_fields(), **attributes}
    with get_tracer().start_as_current_span(name) as span:
        for key, value in safe_attributes(merged).items():
            span.set_attribute(key, value)
        yield span
