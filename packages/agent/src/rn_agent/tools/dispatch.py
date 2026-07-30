"""The tool execution pipeline.

Five stages, in this order, because the order is the security property
(AGENT_ARCHITECTURE §4): **cheapest and most security-relevant first.** An
unauthorized call is rejected before we spend a schema validation on it, and long
before a database round trip.

    1. resolve       the tool exists at all
    2. authorize     enabled for this agent version  AND  permitted for this org
    3. parse         the model's JSON, and strip any forged trusted context
    4. validate      Pydantic, extra="forbid"
    5. execute       under a deadline, then envelope

Two invariants this module is responsible for, and they are the reason it is a
single function with a single call site:

**The model never sees an exception.** Every failure — including one nobody
anticipated — becomes a `ToolEnvelope`. A stack trace handed back to a model is a
stack trace an agent reads out loud on the phone.

**Trusted identity flows one way.** `organization_id`, `call_id` and
`agent_version_id` come from `ToolRuntime`, which was built from a verified
`TenantContext`. If model output contains a colliding key it is discarded and a
security event is recorded — that is a prompt-injection signal, not a bug report.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from rn_agent.errors import ToolBlocked
from rn_agent.tools.base import (
    INJECTED_CONTEXT_KEYS,
    AgentToolScope,
    ToolEnvelope,
    ToolOutcome,
    ToolRuntime,
    ToolSpec,
)
from rn_agent.tools.registry import ToolRegistry
from rn_core.errors import (
    ApplicationError,
    AuthorizationError,
    ConflictError,
    InvariantViolation,
    NotFoundError,
    ProviderError,
    RateLimitError,
    TransientError,
    ValidationError,
)
from rn_core.logging import get_logger
from rn_core.telemetry import start_span

__all__ = ["MAX_FIELD_ERRORS", "dispatch_tool_call"]

_logger = get_logger(__name__)

#: How many field-level messages go back to the model on a validation failure.
#: Enough to correct a call; not so many that a wide model floods the context.
MAX_FIELD_ERRORS = 6

#: Model-supplied strings are truncated before they reach a log line. Redaction
#: already masks phone-shaped and credential-shaped content, but a model can emit
#: an arbitrarily long tool name and an unbounded log field is its own problem.
_MAX_LOGGED_NAME = 64

# Ordered because these types are related by inheritance and the first match
# wins: `RateLimitError` is a `ProviderError`, and `ToolBlocked` is a
# `ConflictError`. Reordering this tuple silently changes what the model is told.
_ERROR_MAP: tuple[tuple[type[ApplicationError], ToolOutcome, str, bool], ...] = (
    (
        AuthorizationError,
        ToolOutcome.DENIED,
        "I'm not able to do that on this call.",
        False,
    ),
    (
        ToolBlocked,
        ToolOutcome.BLOCKED,
        "I'm not able to do that for this contact.",
        False,
    ),
    (
        RateLimitError,
        ToolOutcome.RATE_LIMITED,
        "I'm being asked to do that too often just now.",
        True,
    ),
    (
        NotFoundError,
        ToolOutcome.NOT_FOUND,
        "I couldn't find that.",
        False,
    ),
    (
        ConflictError,
        ToolOutcome.CONFLICT,
        "That's just changed, so I couldn't complete it.",
        False,
    ),
    (
        ValidationError,
        ToolOutcome.INVALID_ARGUMENTS,
        "I didn't have the right details for that.",
        False,
    ),
    (
        ProviderError,
        ToolOutcome.UNAVAILABLE,
        "I couldn't reach that system just now.",
        True,
    ),
    (
        TransientError,
        ToolOutcome.UNAVAILABLE,
        "I couldn't reach that system just now.",
        True,
    ),
)


async def dispatch_tool_call(
    *,
    registry: ToolRegistry,
    scope: AgentToolScope,
    runtime: ToolRuntime,
    name: str,
    arguments_json: str,
) -> ToolEnvelope:
    """Execute one model-requested tool call and return its envelope.

    Args:
        scope: The agent version being served — normally an `AgentSnapshot`.
        runtime: Server-side context, carrying the only tenant authority in play.
        name: The tool the model asked for. Untrusted.
        arguments_json: The model's raw argument string. Untrusted, unparsed.

    Never raises for anything the model did. Raises only on a programming error:
    an `InvariantViolation`, including the cross-tenant guard below, which must
    crash loudly rather than be reported to a caller as "unavailable".

    Deliberately single-shot. Retry policy belongs to the conversation loop, which
    is the layer that knows how much of the turn budget is left; a dispatcher that
    retried internally would hide that spend from the thing bounding it.
    """
    # Cross-tenant guard. A scope and a runtime that disagree about the tenant is
    # not a caller error to be enveloped -- it is a wiring bug, and continuing would
    # run a tool against one tenant's data under another tenant's configuration.
    if scope.organization_id != runtime.organization_id:
        raise InvariantViolation(
            "The agent snapshot and the tool runtime belong to different organizations.",
            detail={
                "scope_organization_id": str(scope.organization_id),
                "runtime_organization_id": str(runtime.organization_id),
            },
        )
    if scope.agent_version_id != runtime.agent_version_id:
        raise InvariantViolation(
            "The agent snapshot and the tool runtime refer to different agent versions.",
            detail={
                "scope_agent_version_id": str(scope.agent_version_id),
                "runtime_agent_version_id": str(runtime.agent_version_id),
            },
        )

    spec = registry.get(name)
    safe_name = name[:_MAX_LOGGED_NAME]

    # -- stage 1: does the tool exist -------------------------------------
    if spec is None:
        _security_event("agent.tool.unknown_tool_requested", runtime, tool_name=safe_name)
        return _denied()

    # -- stage 2: authorization, twice ------------------------------------
    #
    # Agent-level and organization-level are separate checks because tenant
    # entitlements change without an agent version change: removing a tenant's
    # messaging integration must stop `send_whatsapp` with no redeploy and no new
    # version. One combined check could not express that.
    if name not in scope.enabled_tools:
        # The tool was not in the session configuration, so the model either
        # fabricated the call or is working from a stale snapshot. Anomalous
        # either way.
        _security_event("agent.tool.not_enabled_for_version", runtime, tool_name=safe_name)
        return _denied()

    try:
        runtime.context.require(spec.permission)
    except AuthorizationError:
        _logger.warning(
            "agent.tool.permission_denied",
            tool_name=safe_name,
            required_permission=spec.permission,
            organization_id=str(runtime.organization_id),
            agent_version_id=str(runtime.agent_version_id),
        )
        return _denied()

    # -- stage 3: parse, and strip forged trusted context -----------------
    parsed = _parse_arguments(arguments_json)
    if parsed is None:
        return ToolEnvelope(
            outcome=ToolOutcome.INVALID_ARGUMENTS,
            message="I didn't have the right details for that.",
            field_errors=("arguments were not valid JSON",),
        )

    cleaned, forged = _strip_injected_context(parsed)
    if forged:
        # `forged` holds only names from `INJECTED_CONTEXT_KEYS` -- our own
        # literals, never model text -- so this line cannot carry a payload. The
        # values the model supplied are dropped without being logged: recording
        # them to describe the event would put attacker-chosen content, possibly
        # including PII, into our logs.
        _security_event(
            "agent.tool.injected_context_rejected",
            runtime,
            tool_name=safe_name,
            forged_keys=sorted(forged),
        )

    # -- stage 4: validation ----------------------------------------------
    try:
        args = spec.args.model_validate(cleaned)
    except PydanticValidationError as exc:
        return ToolEnvelope(
            outcome=ToolOutcome.INVALID_ARGUMENTS,
            message="I didn't have the right details for that.",
            field_errors=_field_errors(exc),
        )

    # -- stage 5: execute, envelope ---------------------------------------
    return await _execute(spec, args, runtime, safe_name=safe_name)


async def _execute(
    spec: ToolSpec, args: Any, runtime: ToolRuntime, *, safe_name: str
) -> ToolEnvelope:
    started = time.perf_counter()
    # `tool` rather than `tool_name`: `rn_core.telemetry` drops any attribute key
    # containing "name", because customer names must never reach a span. A tool
    # name is a fixed catalog value and is safe -- under a key that survives.
    with start_span("agent.tool.execute", tool=safe_name, effect=spec.effect.value):
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                reply = await spec.handler(args, runtime)
        except TimeoutError:
            _log_outcome(safe_name, ToolOutcome.TIMEOUT, started, runtime)
            return ToolEnvelope(
                outcome=ToolOutcome.TIMEOUT,
                message="That took longer than I could wait for.",
                retryable=True,
            )
        except InvariantViolation:
            # A broken domain rule or a wiring bug. Not something to soften into
            # "unavailable" -- it must surface, be alerted on, and be fixed.
            raise
        except ApplicationError as exc:
            envelope = _map_application_error(exc)
            _log_outcome(safe_name, envelope.outcome, started, runtime, error=exc)
            return envelope
        except Exception as exc:
            # The backstop. An unmapped exception must still become an envelope,
            # because the alternative is an exception propagating into the
            # session loop and ending a live call. Logged with the traceback; the
            # model is told nothing about it.
            _logger.exception(
                "agent.tool.unhandled_error",
                tool_name=safe_name,
                error_type=type(exc).__name__,
                organization_id=str(runtime.organization_id),
                agent_version_id=str(runtime.agent_version_id),
            )
            return ToolEnvelope(
                outcome=ToolOutcome.UNAVAILABLE,
                message="I couldn't complete that just now.",
            )

    _log_outcome(safe_name, ToolOutcome.OK, started, runtime)
    return ToolEnvelope(outcome=ToolOutcome.OK, data=reply.data, message=reply.message)


def _map_application_error(exc: ApplicationError) -> ToolEnvelope:
    for error_type, outcome, message, retryable in _ERROR_MAP:
        if isinstance(exc, error_type):
            return ToolEnvelope(outcome=outcome, message=message, retryable=retryable)
    # A typed error we have not classified. `UNAVAILABLE` rather than a new
    # outcome: the model's vocabulary is closed on purpose, and `exc.message` is
    # deliberately not forwarded -- it is safe for a *user*, which is not the same
    # as safe to put in a model's context on a phone call.
    return ToolEnvelope(
        outcome=ToolOutcome.UNAVAILABLE,
        message="I couldn't complete that just now.",
        retryable=exc.retryable,
    )


def _parse_arguments(arguments_json: str) -> dict[str, Any] | None:
    """Parse the model's argument string, or `None` if it is unusable.

    A non-object top level (`"[]"`, `"null"`, `"7"`) is treated the same as
    malformed JSON: the provider contract is a JSON object of named arguments, and
    anything else cannot be validated against an argument model.
    """
    if not arguments_json.strip():
        # An argument-less tool call. Providers send "" or "{}" inconsistently.
        return {}
    try:
        parsed = json.loads(arguments_json)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _strip_injected_context(
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], frozenset[str]]:
    """Drop any argument whose name the server owns.

    Matching is case-insensitive and ignores a leading underscore, because
    `Organization_ID` and `_call_id` are the same attempt. Note that `extra="forbid"`
    would already reject these — the point of stripping them *first* is to preserve
    the **signal**: otherwise a prompt-injection attempt arrives as an ordinary
    validation error and nothing records that it happened.
    """
    kept: dict[str, Any] = {}
    forged: set[str] = set()
    for key, value in arguments.items():
        normalised = str(key).strip().lstrip("_").lower()
        if normalised in INJECTED_CONTEXT_KEYS:
            forged.add(normalised)
            continue
        kept[key] = value
    return kept, frozenset(forged)


def _field_errors(exc: PydanticValidationError) -> tuple[str, ...]:
    """Field-level messages for the model.

    Only the location and Pydantic's own message — never the submitted value.
    Echoing the value back would put model-chosen content (potentially a phone
    number a caller just spoke) into the next prompt and into any log of it.
    """
    rendered: list[str] = []
    for error in exc.errors()[:MAX_FIELD_ERRORS]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        rendered.append(f"{location}: {error.get('msg', 'invalid')}")
    return tuple(rendered)


def _denied() -> ToolEnvelope:
    """The single refusal envelope.

    One message for "no such tool", "not enabled" and "no permission", on purpose.
    A model that can distinguish them can be used to enumerate what a tenant has
    configured, and the model is reachable by anyone who can phone the number.
    """
    return ToolEnvelope(
        outcome=ToolOutcome.DENIED,
        message="I'm not able to do that on this call.",
    )


def _security_event(event: str, runtime: ToolRuntime, **fields: Any) -> None:
    _logger.warning(
        event,
        organization_id=str(runtime.organization_id),
        agent_version_id=str(runtime.agent_version_id),
        call_id=str(runtime.call_id) if runtime.call_id else None,
        **fields,
    )


def _log_outcome(
    tool_name: str,
    outcome: ToolOutcome,
    started: float,
    runtime: ToolRuntime,
    *,
    error: ApplicationError | None = None,
) -> None:
    """Record what happened. **Arguments and results are never included.**

    `duration_ms` is a measurement of this execution, not a claim about typical
    latency — nothing in Phase 2 has been benchmarked.
    """
    _logger.info(
        "agent.tool.completed",
        tool_name=tool_name,
        outcome=outcome.value,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        organization_id=str(runtime.organization_id),
        agent_version_id=str(runtime.agent_version_id),
        call_id=str(runtime.call_id) if runtime.call_id else None,
        # `code` and `detail` are ours; `detail` may hold provider text, so only
        # the stable code goes on the line.
        error_code=error.code if error else None,
    )
