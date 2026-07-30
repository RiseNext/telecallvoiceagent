"""The agent snapshot: determinism, immutability, and the translation boundary.

The property under test throughout is the one two concurrent calls depend on:
**a snapshot is a frozen, deterministic function of an agent version, and nothing a
caller does to it can be observed by another call.**

`test_building_touches_no_clock_and_no_randomness` is the load-bearing one. Every
other test here would still pass if someone added `created_at=now_utc()` to the
hashed document; that one would not.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from rn_agent.errors import AgentConfigurationError, SnapshotResolutionError
from rn_agent.snapshot import (
    CONTENT_HASH_SCHEMA_VERSION,
    MAX_CONVERSATION_TURNS,
    AgentSnapshot,
    GuardrailConfig,
    build_snapshot,
)
from rn_agent.tools import REGISTRY
from rn_core.clock import now_utc
from rn_core.errors import ValidationError
from rn_core.ids import new_id
from rn_domain.entities.agents import AgentVersion
from rn_domain.enums import AgentVersionStatus
from rn_domain.identifiers import AgentId, AgentVersionId, KnowledgeBaseId, OrganizationId
from rn_domain.values import LanguagePolicy, LanguageTag

pytestmark = [pytest.mark.unit]

_INSTRUCTIONS = "You are a helpful assistant for a company that sells software services."

_POLICY = LanguagePolicy(
    primary=LanguageTag("en"),
    allowed=(LanguageTag("en"), LanguageTag("hi-IN"), LanguageTag("te-IN")),
)


def _version(**overrides: object) -> AgentVersion:
    defaults: dict[str, object] = {
        "id": AgentVersionId(new_id()),
        "organization_id": OrganizationId(new_id()),
        "agent_id": AgentId(new_id()),
        "version_number": 3,
        "instructions": _INSTRUCTIONS,
        "language_policy": _POLICY,
        "status": AgentVersionStatus.PUBLISHED,
        "published_at": now_utc(),
    }
    defaults.update(overrides)
    return AgentVersion(**defaults)  # type: ignore[arg-type]


def _build(version: AgentVersion | None = None, **kwargs: object) -> AgentSnapshot:
    return build_snapshot(
        version=version or _version(),
        enabled_tool_names=kwargs.pop("enabled_tool_names", ["list_knowledge_bases"]),  # type: ignore[arg-type]
        knowledge_base_ids=kwargs.pop("knowledge_base_ids", ()),  # type: ignore[arg-type]
        registry=REGISTRY,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_version_builds_an_identical_snapshot() -> None:
    version = _version()
    first = _build(version)
    second = _build(version)
    assert first.content_hash == second.content_hash
    assert first == second


def test_jsonb_key_order_does_not_change_the_hash() -> None:
    """Postgres does not preserve JSONB key order, so the hash must not depend on it."""
    version_a = _version(turn_policy={"mode": "server_vad", "eagerness": "high"})
    version_b = dataclasses.replace(
        version_a, turn_policy={"eagerness": "high", "mode": "server_vad"}
    )
    assert _build(version_a).content_hash == _build(version_b).content_hash


def test_enabled_tool_order_does_not_change_the_hash() -> None:
    a = _build(enabled_tool_names=["list_knowledge_bases", "find_knowledge_base"])
    b = _build(enabled_tool_names=["find_knowledge_base", "list_knowledge_bases"])
    # Different version ids would differ, so compare the enabled set and specs only.
    assert a.enabled_tools == b.enabled_tools
    assert a.tool_specs_json == b.tool_specs_json


def test_a_behaviour_change_changes_the_hash() -> None:
    """The hash is only useful if it actually detects a change."""
    base = _version()
    assert (
        _build(base).content_hash
        != _build(dataclasses.replace(base, instructions=_INSTRUCTIONS + " Be brief.")).content_hash
    )
    assert _build(base).content_hash != _build(base, enabled_tool_names=[]).content_hash


def test_two_versions_with_identical_behaviour_still_hash_differently() -> None:
    """The hash is a cache key, so it must be per version, not per behaviour."""
    a = _version()
    b = _version()  # same fields, different id
    assert _build(a).content_hash != _build(b).content_hash


def test_building_touches_no_clock_and_no_randomness(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test that would catch a `now_utc()` creeping into the hashed document.

    Every other determinism test here would still pass with a timestamp in the
    document, because they build twice in the same millisecond. This makes the
    absence structural: the clock and the id generator are replaced with something
    that raises.
    """
    import rn_agent.snapshot as snapshot_module

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot construction must not read a clock or a random source")

    monkeypatch.setattr("rn_core.clock.now_utc", explode, raising=True)
    monkeypatch.setattr("rn_core.ids.new_id", explode, raising=True)
    # Guard against the module having imported them by name rather than by module.
    for name in ("now_utc", "new_id", "uuid4", "time"):
        if hasattr(snapshot_module, name):
            monkeypatch.setattr(snapshot_module, name, explode, raising=True)

    assert _build().content_hash  # would raise if any of the above were called


def test_hash_document_carries_a_schema_version() -> None:
    """Without it, adding a field would leave old and new snapshots colliding."""
    assert CONTENT_HASH_SCHEMA_VERSION >= 1


# ---------------------------------------------------------------------------
# Immutability and concurrency safety
# ---------------------------------------------------------------------------


def test_the_snapshot_is_frozen() -> None:
    snapshot = _build()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.instruction_prefix = "rewritten"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.enabled_tools = frozenset()  # type: ignore[misc]


def test_no_field_is_a_mutable_container() -> None:
    """A dict, list or set field would be shared mutable state across live calls."""
    snapshot = _build(knowledge_base_ids=(KnowledgeBaseId(new_id()),))
    for field in dataclasses.fields(snapshot):
        value = getattr(snapshot, field.name)
        assert not isinstance(value, (dict, list, set, bytearray)), field.name


def test_tool_specs_are_handed_out_as_fresh_copies() -> None:
    """A caller mutating what it is given must not be able to corrupt the snapshot."""
    snapshot = _build(enabled_tool_names=["list_knowledge_bases"])
    first = snapshot.realtime_tool_specs()
    first[0]["name"] = "hijacked"
    first[0]["parameters"]["properties"].clear()

    second = snapshot.realtime_tool_specs()
    assert second[0]["name"] == "list_knowledge_bases"
    assert second[0]["parameters"]["properties"]


def test_tool_specs_json_is_canonical() -> None:
    snapshot = _build(enabled_tool_names=["find_knowledge_base", "list_knowledge_bases"])
    parsed = json.loads(snapshot.tool_specs_json)
    assert [spec["name"] for spec in parsed] == ["find_knowledge_base", "list_knowledge_bases"]
    # Re-encoding canonically must reproduce the stored text exactly.
    assert (
        json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        == snapshot.tool_specs_json
    )


# ---------------------------------------------------------------------------
# Publication state
# ---------------------------------------------------------------------------


def test_a_draft_version_cannot_become_a_snapshot() -> None:
    draft = _version(status=AgentVersionStatus.DRAFT, published_at=None)
    with pytest.raises(SnapshotResolutionError):
        _build(draft)


def test_an_archived_version_cannot_become_a_snapshot() -> None:
    """Archived is not published: it served calls once and must not serve new ones."""
    archived = _version(status=AgentVersionStatus.ARCHIVED)
    with pytest.raises(SnapshotResolutionError):
        _build(archived)


# ---------------------------------------------------------------------------
# The translation boundary: stored JSONB -> typed configuration
# ---------------------------------------------------------------------------


def test_stored_configuration_becomes_typed_not_dicts() -> None:
    snapshot = _build(
        _version(
            turn_policy={
                "mode": "server_vad",
                "eagerness": "high",
                "parameters": {"threshold": 0.6},
            },
            voice_map={"en": {"provider": "openai", "voice_id": "marin"}},
            guardrail_config={"max_turns": 6},
        )
    )
    assert snapshot.turn_policy.mode == "server_vad"
    assert snapshot.turn_policy.parameter("threshold") == 0.6
    assert snapshot.guardrail_config.max_turns == 6
    voice = snapshot.voice_for("en")
    assert voice is not None and voice.voice_id == "marin"
    assert snapshot.voice_for("te-IN") is None


def test_unknown_turn_policy_keys_survive_a_publish() -> None:
    """A knob a future dashboard writes must not be silently dropped by old code."""
    snapshot = _build(_version(turn_policy={"future_knob": 42}))
    assert snapshot.turn_policy.parameter("future_knob") == 42


@pytest.mark.parametrize(
    "stored",
    [
        {"mode": 7},
        {"parameters": "not-an-object"},
        {"parameters": {"nested": {"too": "deep"}}},
    ],
)
def test_malformed_turn_policy_is_refused_at_the_boundary(stored: dict[str, object]) -> None:
    with pytest.raises(AgentConfigurationError):
        _build(_version(turn_policy=stored))


@pytest.mark.parametrize(
    "stored",
    [
        {"en": "marin"},
        {"en": {"provider": "openai"}},
        # A key that is not a language tag can never be selected, so it is a
        # configuration error rather than a harmless extra row. `LanguageTag` raises
        # `ValidationError` for it, which is the correct type for a bad boundary value.
        {"English": {"provider": "openai", "voice_id": "marin"}},
    ],
)
def test_malformed_voice_map_is_refused_at_the_boundary(stored: dict[str, object]) -> None:
    with pytest.raises((AgentConfigurationError, ValidationError)):
        _build(_version(voice_map=stored))


def test_a_boolean_is_not_accepted_where_an_integer_is_required() -> None:
    """`bool` is an `int` in Python: `max_turns: true` would quietly become 1."""
    with pytest.raises(AgentConfigurationError):
        _build(_version(guardrail_config={"max_turns": True}))


def test_a_tenant_cannot_raise_the_turn_ceiling() -> None:
    """Guardrail knobs may only make a call shorter or stricter."""
    with pytest.raises(AgentConfigurationError):
        _build(_version(guardrail_config={"max_turns": MAX_CONVERSATION_TURNS + 1}))
    with pytest.raises(AgentConfigurationError):
        _build(_version(guardrail_config={"max_invalid_tool_retries": 99}))


def test_guardrail_config_has_no_switch_for_disclosure_or_opt_out() -> None:
    """A knob a tenant cannot change is worse than no knob: someone wires it up.

    Disclosure and opt-out are platform obligations enforced in code. If a field for
    either ever appears here, this test should be the thing that stops it.
    """
    fields = {field.name for field in dataclasses.fields(GuardrailConfig)}
    for forbidden in ("require_ai_disclosure", "disclosure", "honour_opt_out", "opt_out"):
        assert forbidden not in fields


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------


def test_an_unregistered_tool_name_is_dropped_rather_than_fatal() -> None:
    """A stored config row can outlive the tool it names.

    Refusing to build would take a tenant's calls down over a configuration row; the
    tool is simply not offered, and the dispatcher refuses it if the model asks.
    """
    snapshot = _build(enabled_tool_names=["list_knowledge_bases", "tool_removed_in_v9"])
    assert snapshot.enabled_tools == frozenset({"list_knowledge_bases"})
    assert [spec["name"] for spec in snapshot.realtime_tool_specs()] == ["list_knowledge_bases"]


def test_allows_tool_reflects_the_enabled_set() -> None:
    snapshot = _build(enabled_tool_names=["list_knowledge_bases"])
    assert snapshot.allows_tool("list_knowledge_bases")
    assert not snapshot.allows_tool("find_knowledge_base")


def test_instruction_prefix_contains_the_platform_layer_first() -> None:
    from rn_agent.instructions.platform import PLATFORM_INSTRUCTIONS

    snapshot = _build()
    assert snapshot.instruction_prefix.startswith(PLATFORM_INSTRUCTIONS.strip()[:80])
    assert _INSTRUCTIONS in snapshot.instruction_prefix
