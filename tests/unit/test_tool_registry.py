"""Registration-time validation.

Everything here fails at **import**, not on a live call. That is the whole design
decision under test: a duplicate name, an unknown permission or an unexportable
schema should stop a deployment starting, because the alternative is discovering it
while a caller is on the line.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from rn_agent.errors import ToolRegistrationError
from rn_agent.tools import REGISTRY
from rn_agent.tools.base import Effect, ToolArgs, ToolReply, ToolRuntime
from rn_agent.tools.builtin import register_builtin_tools
from rn_agent.tools.registry import (
    MAX_TOOL_TIMEOUT,
    MIN_DESCRIPTION_CHARS,
    ToolRegistry,
)

pytestmark = [pytest.mark.unit]

_GOOD_DESCRIPTION = "Looks something up for the caller and returns what it found."


class _Args(ToolArgs):
    query: str = "x"


class _NotToolArgs(BaseModel):
    """A plain Pydantic model — no `extra="forbid"`, so an invented field would pass."""

    query: str = "x"


class _CollidingArgs(ToolArgs):
    organization_id: str = ""


async def _handler(args: _Args, rt: ToolRuntime) -> ToolReply:
    return ToolReply(data={})


def _declare(registry: ToolRegistry, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "name": "a_tool",
        "description": _GOOD_DESCRIPTION,
        "args": _Args,
        "effect": Effect.READ,
        "permission": "org:knowledge:read",
        "timeout": timedelta(seconds=3),
    }
    kwargs.update(overrides)
    registry.tool(**kwargs)(_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Valid declaration
# ---------------------------------------------------------------------------


def test_a_valid_tool_registers() -> None:
    registry = ToolRegistry()
    _declare(registry)
    spec = registry.get("a_tool")
    assert spec is not None
    assert spec.effect is Effect.READ
    assert spec.realtime_spec["name"] == "a_tool"


def test_the_decorator_returns_the_handler_unwrapped() -> None:
    """No wrapper: every check belongs to the dispatcher, which is the only
    sanctioned way to invoke a tool. A wrapper would make handler tests test it."""
    registry = ToolRegistry()
    decorated = registry.tool(
        name="a_tool",
        description=_GOOD_DESCRIPTION,
        args=_Args,
        effect=Effect.READ,
        permission="org:knowledge:read",
        timeout=timedelta(seconds=3),
    )(_handler)
    assert decorated is _handler


# ---------------------------------------------------------------------------
# Rejected declarations
# ---------------------------------------------------------------------------


def test_a_duplicate_name_is_refused() -> None:
    registry = ToolRegistry()
    _declare(registry)
    with pytest.raises(ToolRegistrationError):
        _declare(registry)


@pytest.mark.parametrize("name", ["A_Tool", "a tool", "ab", "tool-name", "1tool", "a" * 65, ""])
def test_a_malformed_name_is_refused(name: str) -> None:
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), name=name)


def test_a_throwaway_description_is_refused() -> None:
    """The description is how the model chooses this tool over another one."""
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), description="x" * (MIN_DESCRIPTION_CHARS - 1))


def test_a_plain_basemodel_is_refused() -> None:
    """Without `ToolArgs`, `extra="forbid"` is absent and an invented field passes."""
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), args=_NotToolArgs)


def test_a_permission_outside_the_frozen_catalog_is_refused() -> None:
    """Adding a permission requires a migration — the DB CHECK on `roles.permissions`
    is built from a frozen snapshot, so a new value cannot even be stored."""
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), permission="org:meetings:write")


def test_a_platform_permission_is_refused() -> None:
    """No tool a model can request may require a cross-tenant capability."""
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), permission="platform:call:read")


def test_an_external_effect_is_refused_until_its_machinery_exists() -> None:
    """`EXTERNAL` needs a server-derived idempotency key, rate limits and a compliance
    gate (Phase 9/10). Declaring one now would be a tool with no protection against
    sending the same message three times."""
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), effect=Effect.EXTERNAL)


@pytest.mark.parametrize(
    "timeout",
    [timedelta(milliseconds=200), MAX_TOOL_TIMEOUT + timedelta(seconds=1)],
)
def test_a_timeout_outside_a_conversational_range_is_refused(timeout: timedelta) -> None:
    with pytest.raises(ToolRegistrationError):
        _declare(ToolRegistry(), timeout=timeout)


def test_an_argument_colliding_with_injected_context_is_refused() -> None:
    """Caught at import so a future tool cannot quietly claim a server-owned name."""
    with pytest.raises(ToolRegistrationError) as caught:
        _declare(ToolRegistry(), args=_CollidingArgs)
    assert "organization_id" in str(caught.value.detail)


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


def test_the_process_registry_is_frozen() -> None:
    """One registry is read by every concurrent session, so a runtime registration
    would be shared mutable state across live calls."""
    assert REGISTRY.is_frozen
    with pytest.raises(ToolRegistrationError):
        _declare(REGISTRY, name="sneaky_tool")
    assert "sneaky_tool" not in REGISTRY.names


def test_a_fresh_registry_is_not_frozen() -> None:
    """Which is the only reason `ToolRegistry` is a class rather than module state."""
    assert not ToolRegistry().is_frozen


# ---------------------------------------------------------------------------
# Export and lookup
# ---------------------------------------------------------------------------


def test_realtime_specs_are_sorted_and_filtered() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    names = [spec["name"] for spec in registry.to_realtime_specs(registry.names)]
    assert names == sorted(names)

    only_one = registry.to_realtime_specs(["find_knowledge_base"])
    assert [spec["name"] for spec in only_one] == ["find_knowledge_base"]


def test_exported_specs_are_fresh_objects_not_the_registry_s_own() -> None:
    """One registry is read by every concurrent session.

    Handing out the registry's own dictionaries would make one caller's edit change
    what every other live call is told about that tool — a shared-state bug that only
    shows up under concurrency, which is the worst kind to go looking for.
    """
    registry = ToolRegistry()
    register_builtin_tools(registry)

    first = registry.to_realtime_specs(["find_knowledge_base"])
    first[0]["name"] = "hijacked"
    first[0]["parameters"]["properties"].clear()

    second = registry.to_realtime_specs(["find_knowledge_base"])
    assert second[0]["name"] == "find_knowledge_base"
    assert second[0]["parameters"]["properties"]
    # And the same applies to the per-spec accessor.
    spec = registry.get("find_knowledge_base")
    assert spec is not None
    assert spec.realtime_spec is not spec.realtime_spec
    assert spec.realtime_spec["name"] == "find_knowledge_base"


def test_the_json_export_matches_the_object_export() -> None:
    """Two encodings that must agree — so the test says so rather than assuming it."""
    import json

    registry = ToolRegistry()
    register_builtin_tools(registry)
    names = ["find_knowledge_base", "list_knowledge_bases"]
    assert json.loads(registry.to_realtime_specs_json(names)) == list(
        registry.to_realtime_specs(names)
    )


def test_an_unknown_enabled_name_is_skipped_not_fatal() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    specs = registry.to_realtime_specs(["find_knowledge_base", "removed_in_v9"])
    assert [spec["name"] for spec in specs] == ["find_knowledge_base"]


def test_lookup_of_an_unknown_tool_returns_none() -> None:
    """`None` rather than raising: the realistic caller is the dispatcher handling a
    name the *model* produced, where "no such tool" is an expected input."""
    assert REGISTRY.get("no_such_tool") is None


def test_specs_iterate_in_a_deterministic_order() -> None:
    assert [spec.name for spec in REGISTRY.specs()] == sorted(REGISTRY.names)


def test_the_builtin_set_is_exactly_the_declared_three() -> None:
    """Pinned deliberately. The rest of the V1 tool set is Phase 3/9/10; if a fourth
    tool appears here without a roadmap phase behind it, that is scope creep and this
    catches it.

    Phase 2 pinned two. A first slice of Phase 3 Stage 2 adds `search_knowledge` — content retrieval over the
    `KnowledgeRetriever` seam, on the `org:knowledge:read` permission that migration
    `0001` already froze, so it needed no migration to arrive.
    """
    assert REGISTRY.names == frozenset(
        {"list_knowledge_bases", "find_knowledge_base", "search_knowledge"}
    )


def test_every_builtin_tool_is_read_only() -> None:
    """Nothing shipped so far changes anything."""
    assert all(spec.effect is Effect.READ for spec in REGISTRY.specs())
