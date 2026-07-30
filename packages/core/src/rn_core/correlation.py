"""Correlation identifiers for logs, spans and job payloads.

This is **observability context only.** It exists so that a log line emitted deep
inside a service can be tied back to the request, call or job that caused it,
without threading a context object through every signature.

> **It is NOT an authorization source. Ever.**
>
> `organization_id` appears here because every log line should carry it. It must
> never be read back out to decide what a caller may see. Tenant authority comes
> from `rn_services.tenancy.TenantContext`, which is constructed from a verified
> identity — see CLAUDE.md rules 3 and 4. A contextvar is ambient, and ambient
> authority is how cross-tenant bugs happen: a task that inherits a stale context
> would silently act as the wrong tenant.
>
> The two are separate types on purpose. If you find yourself wanting to read
> `get_correlation().organization_id` to scope a query, stop — you need a
> `TenantContext` passed in explicitly.

`contextvars` are used rather than thread-locals because the whole stack is
asyncio: a contextvar is inherited by tasks spawned from the current context,
which is what makes a per-turn tool task carry its call's ids automatically.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "CorrelationContext",
    "bind_correlation",
    "correlation_fields",
    "get_correlation",
    "new_request_id",
]


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Identifiers safe to attach to every log line and span.

    Every field is optional — an API request has no `call_id`, a media session
    has no `request_id`. Emitting only what is known keeps log volume honest.

    Everything here is an opaque identifier. **No PII belongs in this object**:
    no phone numbers, no names, no transcript text. That invariant is what lets
    the logging pipeline attach it unconditionally.
    """

    request_id: str | None = None
    trace_id: str | None = None
    organization_id: str | None = None
    actor_id: str | None = None
    call_id: str | None = None
    campaign_id: str | None = None
    agent_version_id: str | None = None
    job_id: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Only the fields that are set, flattened for structured logging."""
        data: dict[str, Any] = {
            k: v
            for k, v in (
                ("request_id", self.request_id),
                ("trace_id", self.trace_id),
                ("organization_id", self.organization_id),
                ("actor_id", self.actor_id),
                ("call_id", self.call_id),
                ("campaign_id", self.campaign_id),
                ("agent_version_id", self.agent_version_id),
                ("job_id", self.job_id),
            )
            if v is not None
        }
        data.update(self.extra)
        return data


#: A single shared empty context. The ContextVar default is `None` rather than an
#: instance so that nothing mutable is shared across tasks by construction.
_EMPTY = CorrelationContext()

_CONTEXT: contextvars.ContextVar[CorrelationContext | None] = contextvars.ContextVar(
    "rn_correlation", default=None
)


def get_correlation() -> CorrelationContext:
    """The current correlation context. Never `None`."""
    return _CONTEXT.get() or _EMPTY


def correlation_fields() -> dict[str, Any]:
    """The current context as log fields."""
    return get_correlation().as_dict()


def new_request_id() -> str:
    """A fresh opaque request identifier."""
    return uuid.uuid4().hex


@contextmanager
def bind_correlation(**values: str | None) -> Iterator[CorrelationContext]:
    """Bind correlation values for the duration of a block.

    Merges with — rather than replaces — whatever is already bound, so an inner
    scope adding `call_id` keeps the outer `request_id`. Always restores on exit,
    including on exception, which is what stops a failed request leaking its ids
    into whatever the worker picks up next.
    """
    current = get_correlation()
    known = {f for f in CorrelationContext.__dataclass_fields__ if f != "extra"}
    updates = {k: v for k, v in values.items() if k in known and v is not None}
    extra = {k: str(v) for k, v in values.items() if k not in known and v is not None}
    merged = replace(
        current,
        **updates,
        extra={**current.extra, **extra} if extra else current.extra,
    )
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)
