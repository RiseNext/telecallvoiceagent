"""Agent-layer errors.

Deliberately thin. `rn_core.errors` already has the taxonomy every layer maps
onto, and a parallel hierarchy here would mean two places to keep in step and two
mappings from error to HTTP status. Only failures with no existing home get a
class, and each one subclasses the `rn_core` type whose *meaning* it carries so
callers that branch on `ConflictError` or `ValidationError` keep working.

None of these ever reach a model. Model-facing failure is a `ToolEnvelope`
(`rn_agent.tools.base`); an exception crossing that boundary would put a stack
trace into something an agent reads out loud.
"""

from __future__ import annotations

from rn_core.errors import ConflictError, InvariantViolation, ValidationError

__all__ = [
    "AgentConfigurationError",
    "SnapshotResolutionError",
    "ToolBlocked",
    "ToolRegistrationError",
]


class SnapshotResolutionError(ConflictError):
    """An agent version cannot serve a call.

    A draft or archived version, most often. `ConflictError` rather than
    `NotFoundError`: the row exists and the caller may see it — it is in the wrong
    *state*, and telling them so is correct because they own it.
    """


class AgentConfigurationError(ValidationError):
    """Stored agent configuration is malformed or outside its permitted range.

    Raised at the translation boundary, where JSONB becomes typed configuration.
    A tenant's stored value is untrusted input like any other, and a bad one must
    fail there rather than reaching a live session as a `KeyError`.
    """


class ToolRegistrationError(InvariantViolation):
    """A tool declaration is invalid.

    Raised at **import time**, not at call time. A duplicate name, an unknown
    permission or an unexportable schema is a deployment that should not start,
    not a surprise on a live call.
    """


# Named for the outcome it maps to rather than for the N818 suffix convention, the
# same way `rn_core.InvariantViolation` is: this raises `ToolOutcome.BLOCKED`, and
# `ToolBlockedError` would read as an error about blocking rather than as the block.
class ToolBlocked(ConflictError):  # noqa: N818
    """A tool was refused by a compliance gate — opt-out, consent, calling window.

    Maps to `ToolOutcome.BLOCKED`. Defined here in Phase 2 so the outcome
    vocabulary is complete and its mapping is tested; **no compliance gate is
    implemented yet.** The gates themselves are Phase 9/10, where the tools with
    external effects arrive.
    """
