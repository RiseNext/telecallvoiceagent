"""The tool registry. Declared once, exported to several consumers.

Everything a tool is — name, description, argument model, effect, permission,
timeout, implementation — is stated in one decorator call, and every consumer
derives what it needs from that. There is no second list of tool names anywhere,
because a second list is a thing that goes out of date.

**Validation happens at registration, which is import time.** A duplicate name, a
permission that is not in the frozen catalog, an unexportable schema, an argument
that collides with server-injected context: all of these stop the process from
starting. The alternative — discovering them when a caller is on the line — is
not a trade worth making for a check that costs microseconds once.

**The LangChain export is not here.** `rn_orchestration.tools.to_langchain_tools`
walks this registry from above (AGENT_ARCHITECTURE §3.1). Putting it here would
make the package that every live call loads depend on `langchain-core`, which hard-
depends on `langsmith` — a tracing SaaS client in the audio process, and Indian
call transcripts leaving the country by default. An import-linter contract makes
that impossible rather than merely discouraged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from rn_agent.errors import ToolRegistrationError
from rn_agent.tools.base import (
    INJECTED_CONTEXT_KEYS,
    Effect,
    ToolArgs,
    ToolHandler,
    ToolSpec,
)
from rn_agent.tools.schema import build_flat_tool_spec, canonical_json
from rn_domain.permissions import ORG_PERMISSIONS, Permission

__all__ = ["MAX_TOOL_TIMEOUT", "MIN_TOOL_TIMEOUT", "ToolRegistry"]

#: Provider-facing tool names. Lowercase snake_case, because that is what the
#: function-calling conventions of every provider we might use accept without
#: transformation — and a name that has to be transformed is a name that can be
#: transformed differently in two places.
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

#: A tool that cannot finish inside this is not usable in a live conversation: the
#: caller hears silence and the turn budget is already spent. Anything slower runs
#: as a background job and the agent says it will follow up.
MAX_TOOL_TIMEOUT = timedelta(seconds=15)
#: Guards the plausible typo `timeout=timedelta(milliseconds=200)`, which would
#: make a tool fail under ordinary latency and look like an outage.
MIN_TOOL_TIMEOUT = timedelta(seconds=1)

#: A tool description is what the model reads when deciding whether this tool is the
#: right one, so a throwaway description is a correctness problem rather than a
#: documentation one. Twenty characters is roughly "Books a meeting slot" — short,
#: but a real sentence rather than a label.
MIN_DESCRIPTION_CHARS = 20

#: Effects a Phase-2 tool may declare.
#:
#: `EXTERNAL` requires a server-derived idempotency key, per-call/per-org/per-
#: provider rate limits and a compliance gate (AGENT_ARCHITECTURE §4.4). None of
#: that exists yet, so a tool declaring it would be a tool with no protection
#: against sending the same WhatsApp message three times. Refused here rather than
#: documented as a caution.
_PERMITTED_EFFECTS: frozenset[Effect] = frozenset({Effect.READ, Effect.INTERNAL})


class ToolRegistry:
    """A set of tool declarations.

    The process-wide instance is `rn_agent.tools.REGISTRY`, frozen after the
    built-in tools import. Tests construct their own unfrozen instances; that is
    the only reason this is a class rather than module state.
    """

    __slots__ = ("_frozen", "_specs")

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._frozen = False

    # -- declaration --------------------------------------------------------

    def tool(
        self,
        *,
        name: str,
        description: str,
        args: type[ToolArgs],
        effect: Effect,
        permission: Permission,
        timeout: timedelta,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Declare a tool.

        Returns the handler unchanged. The decorator registers a `ToolSpec` and
        does **not** wrap the function: a wrapper would make the handler's own
        tests test the wrapper, and every check the wrapper would perform belongs
        to the dispatcher, which is the only sanctioned way to invoke a tool.
        """

        def decorate(handler: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    args=args,
                    effect=effect,
                    permission=permission,
                    timeout=timeout,
                    handler=handler,
                    realtime_spec_json=canonical_json(
                        build_flat_tool_spec(
                            name=name,
                            description=description,
                            args_schema=args.model_json_schema(),
                        )
                    ),
                )
            )
            return handler

        return decorate

    def register(self, spec: ToolSpec) -> None:
        """Add a validated spec. Raises `ToolRegistrationError` on any problem."""
        if self._frozen:
            raise ToolRegistrationError(
                "The tool registry is frozen; tools may only be declared at import time.",
                detail={"tool": spec.name},
            )
        self._validate(spec)
        self._specs[spec.name] = spec

    def freeze(self) -> None:
        """Forbid further registration.

        Called once, after the built-in tools are imported. The registry is read
        by every concurrent session, so a mutation at runtime would be a shared-
        state bug across live calls — and one that only appears under concurrency.
        """
        self._frozen = True

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def _validate(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ToolRegistrationError(
                "A tool with this name is already registered.",
                detail={"tool": spec.name},
            )
        if not _TOOL_NAME_RE.match(spec.name):
            raise ToolRegistrationError(
                "Tool names must be lowercase snake_case, 3-64 characters.",
                detail={"tool": spec.name},
            )
        if len(spec.description.strip()) < MIN_DESCRIPTION_CHARS:
            raise ToolRegistrationError(
                "Tool descriptions are prompt surface and must actually describe the tool.",
                detail={"tool": spec.name, "length": len(spec.description.strip())},
            )
        if not (isinstance(spec.args, type) and issubclass(spec.args, ToolArgs)):
            raise ToolRegistrationError(
                "Tool arguments must be a ToolArgs subclass, so extra='forbid' applies.",
                detail={"tool": spec.name},
            )
        # Only org-scoped permissions. A `platform:*` permission crosses the
        # tenant boundary, and no tool a model can request may ever require one.
        if spec.permission not in ORG_PERMISSIONS:
            raise ToolRegistrationError(
                "Tool permissions must be organization-scoped and in the frozen catalog. "
                "Adding a permission requires a migration.",
                detail={"tool": spec.name, "permission": spec.permission},
            )
        if spec.effect not in _PERMITTED_EFFECTS:
            raise ToolRegistrationError(
                "This effect requires the idempotency, rate-limit and compliance machinery "
                "that does not exist yet.",
                detail={"tool": spec.name, "effect": spec.effect.value},
            )
        if not MIN_TOOL_TIMEOUT <= spec.timeout <= MAX_TOOL_TIMEOUT:
            raise ToolRegistrationError(
                "Tool timeout is outside the range a live conversation can absorb.",
                detail={
                    "tool": spec.name,
                    "seconds": spec.timeout.total_seconds(),
                    "min_seconds": MIN_TOOL_TIMEOUT.total_seconds(),
                    "max_seconds": MAX_TOOL_TIMEOUT.total_seconds(),
                },
            )
        colliding = sorted(set(spec.args.model_fields) & INJECTED_CONTEXT_KEYS)
        if colliding:
            raise ToolRegistrationError(
                "Tool arguments may not use a name that the server injects; "
                "trusted context is never a tool parameter.",
                detail={"tool": spec.name, "fields": colliding},
            )

    # -- lookup -------------------------------------------------------------

    def get(self, name: str) -> ToolSpec | None:
        """The spec for a name, or `None`.

        Returns `None` rather than raising because the realistic caller is the
        dispatcher handling a name the *model* produced, where "no such tool" is
        an expected input to be answered with a refusal — not an exception.
        """
        return self._specs.get(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Every spec, ordered by name so iteration is deterministic."""
        return tuple(self._specs[name] for name in sorted(self._specs))

    # -- export -------------------------------------------------------------

    def to_realtime_specs(self, enabled: Iterable[str]) -> tuple[dict[str, Any], ...]:
        """Flat provider specs for the enabled tools, ordered by name.

        Each spec is a **fresh object**: the registry is shared by every concurrent
        session, so handing out its own dictionaries would let one caller's edit change
        what every other live call is told about a tool.

        Sorted rather than in declaration or caller order: this feeds a session
        configuration and a snapshot content hash, and both must be byte-stable for the
        same input set.

        Silently skips a name with no registered tool. A stored `agent_tool_configs`
        row can outlive the tool it names — a tool removed in a later release, an
        older agent version — and refusing to build the session would take an
        entire tenant's calls down over a configuration row. The tool is simply not
        offered; if the model somehow asks for it anyway, the dispatcher refuses.
        """
        return tuple(self._specs[name].realtime_spec for name in self._enabled_names(enabled))

    def to_realtime_specs_json(self, enabled: Iterable[str]) -> str:
        """The same specs as one canonical JSON array.

        What `AgentSnapshot` stores. Going straight from the per-tool canonical text to
        the array text means the specs are canonicalised once, at registration, rather
        than parsed and re-encoded on every snapshot build.
        """
        inner = ",".join(
            self._specs[name].realtime_spec_json for name in self._enabled_names(enabled)
        )
        return f"[{inner}]"

    def _enabled_names(self, enabled: Iterable[str]) -> list[str]:
        return sorted(set(enabled) & self.names)
