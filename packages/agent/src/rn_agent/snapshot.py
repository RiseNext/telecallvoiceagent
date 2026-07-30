"""The agent snapshot: one immutable object, shared read-only by every call.

Two concurrent callers on the same agent version share exactly one of these
(AGENT_ARCHITECTURE §1.1). Everything per-call — conversation history, ring
buffer, tool idempotency keys, "disclosure already spoken" — lives elsewhere, in
per-call state that dies with the call. If a field here were mutable, a second
concurrent call could observe the first one's edit, which is the single worst class
of bug this system can have.

So the rules this module enforces on itself:

**Frozen all the way down.** No `dict`, no `list`, no `set` field. Open-shape
configuration is carried as a sorted tuple of key/value pairs; provider tool specs
are carried as canonical JSON *text*. Both are genuinely immutable and both are
deterministic, which the mutable equivalents are not.

**No clock, no randomness, no I/O in construction.** `build_snapshot` is a pure
function of its arguments. That is what makes `content_hash` stable, and it is
asserted by a test that patches `time` and `uuid` to explode.

**Immutable-on-publish is what makes caching correct.** A published version can
never change, so a future per-process cache keyed by `agent_version_id` needs no
invalidation channel at all — only eviction. Phase 2 adds no cache (there is no
live-call path to measure), but it builds the property the cache will depend on.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rn_agent.errors import AgentConfigurationError, SnapshotResolutionError
from rn_agent.instructions.compose import compose_instruction_prefix
from rn_agent.tools.registry import ToolRegistry
from rn_agent.tools.schema import canonical_json
from rn_domain.entities.agents import AgentVersion
from rn_domain.identifiers import AgentId, AgentVersionId, KnowledgeBaseId, OrganizationId
from rn_domain.values import LanguagePolicy, LanguageTag

__all__ = [
    "CONTENT_HASH_SCHEMA_VERSION",
    "AgentSnapshot",
    "GuardrailConfig",
    "TurnPolicy",
    "VoiceRef",
    "build_snapshot",
]

#: Bumped whenever the canonical serialisation below changes shape.
#:
#: Without it, adding a field would leave old and new snapshots hashing
#: identically for the field's default value, and a cache keyed on the hash would
#: serve a stale entry. Part of the hashed document, so a bump changes every hash
#: — which is the intended, visible consequence.
CONTENT_HASH_SCHEMA_VERSION = 1

#: JSON scalars are the only value types open-shape configuration may hold. A
#: nested structure in `turn_policy.parameters` would be a mutable object inside a
#: frozen snapshot, and would make ordering — and therefore the hash — ambiguous.
type JsonScalar = str | int | float | bool | None

#: Ceiling on how long one conversation may run. A tenant may configure fewer
#: turns, never more: an unbounded conversation is a cost incident and, on a phone
#: call, a caller trapped with a looping agent.
MAX_CONVERSATION_TURNS = 40
DEFAULT_CONVERSATION_TURNS = 12

#: Ceiling on retries after the model sends unusable arguments. Two is enough for
#: a model to correct a field; more is a validation loop eating the turn budget.
MAX_INVALID_TOOL_RETRIES = 2


@dataclass(frozen=True, slots=True)
class VoiceRef:
    """Which voice to use, for one language. `(provider, voice_id)`.

    Language-keyed rather than agent-keyed because the catalogues are not
    interchangeable and a mid-call language switch cannot always change the voice
    (AGENT_ARCHITECTURE §7). Phase 2 stores and validates the mapping; nothing
    speaks yet.
    """

    provider: str
    voice_id: str


@dataclass(frozen=True, slots=True)
class TurnPolicy:
    """Turn-taking configuration.

    `parameters` is deliberately opaque here. VAD thresholds, frame sizes and
    eagerness knobs differ per provider, GA defaults for them are listed
    UNVERIFIED in AGENT_ARCHITECTURE §12, and Phase 2 has no audio path that could
    validate one. Carrying them as opaque ordered pairs preserves them across a
    publish without pretending to understand them — the adapter that does will
    parse them in its own phase.
    """

    mode: str = "semantic_vad"
    eagerness: str = "low"
    parameters: tuple[tuple[str, JsonScalar], ...] = ()

    def parameter(self, key: str) -> JsonScalar:
        return next((value for name, value in self.parameters if name == key), None)


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    """Guardrail settings a tenant may legitimately choose.

    **There is deliberately no switch for AI disclosure or opt-out handling.** Both
    are platform obligations that a tenant may not turn off, and a field a tenant
    cannot change is not configuration — it is a misleading knob that invites
    someone to wire it up later. Those two guardrails are enforced in code
    (`rn_agent.guardrails`) and in the platform instruction layer, neither of which
    is addressable from tenant configuration.

    The knobs that *are* here can only make a call shorter or stricter.
    """

    max_turns: int = DEFAULT_CONVERSATION_TURNS
    max_invalid_tool_retries: int = MAX_INVALID_TOOL_RETRIES
    extra: tuple[tuple[str, JsonScalar], ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.max_turns <= MAX_CONVERSATION_TURNS:
            raise AgentConfigurationError(
                "Configured turn limit is outside the permitted range.",
                detail={"max_turns": self.max_turns, "ceiling": MAX_CONVERSATION_TURNS},
            )
        if not 0 <= self.max_invalid_tool_retries <= MAX_INVALID_TOOL_RETRIES:
            raise AgentConfigurationError(
                "Configured tool-retry limit is outside the permitted range.",
                detail={
                    "max_invalid_tool_retries": self.max_invalid_tool_retries,
                    "ceiling": MAX_INVALID_TOOL_RETRIES,
                },
            )


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """Everything one agent version needs in order to hold a conversation.

    Constructed only by `build_snapshot`. Safe to share across concurrent
    sessions: every field is immutable, and `realtime_tool_specs()` hands out a
    fresh copy so a caller that mutates what it is given cannot corrupt what the
    next caller sees.
    """

    agent_version_id: AgentVersionId
    organization_id: OrganizationId
    agent_id: AgentId
    version_number: int
    #: Instruction layers 1-3, already composed. Byte-identical across every call
    #: on this version, which is the precondition for provider prompt caching.
    #: Layer 4 is rendered per call and never stored here.
    instruction_prefix: str
    language_policy: LanguagePolicy
    turn_policy: TurnPolicy
    guardrail_config: GuardrailConfig
    enabled_tools: frozenset[str]
    #: Flat provider tool specs as canonical JSON text.
    #:
    #: Text rather than parsed objects because a `dict` inside a frozen dataclass
    #: is mutable shared state, and `MappingProxyType` — the obvious fix — is not
    #: JSON-serialisable, so it would break the only consumer that matters. Text is
    #: immutable, deterministic, hashable, and already the form the wire needs.
    tool_specs_json: str
    voice_map: tuple[tuple[str, VoiceRef], ...]
    knowledge_base_ids: tuple[KnowledgeBaseId, ...]
    realtime_model: str | None
    telephony_sample_rate: int | None
    #: SHA-256 over the canonical serialisation defined in `_canonical_document`.
    #:
    #: A **change-detection and cache-key** value. Two snapshots with equal hashes
    #: are byte-identical under that serialisation — that is the whole claim. It is
    #: not a content-addressed identifier, it is not a signature, and it must not
    #: be used to authenticate anything.
    content_hash: str

    def realtime_tool_specs(self) -> tuple[dict[str, Any], ...]:
        """The flat tool specs, as fresh mutable objects.

        Parsed on each call, by design: each caller gets its own copy, so nothing a
        caller does to the returned dicts is visible to any other call. The parse
        happens once per session open, not per turn, and never inside a live turn.
        """
        parsed = json.loads(self.tool_specs_json)
        return tuple(parsed)

    def voice_for(self, language: LanguageTag | str) -> VoiceRef | None:
        """The voice configured for a language, or `None`.

        A lookup method rather than a `Mapping` field because a mapping would have
        to be either mutable or a proxy, and because this is how every caller
        actually uses it — nobody iterates a voice map.
        """
        wanted = language.value if isinstance(language, LanguageTag) else language
        return next((ref for tag, ref in self.voice_map if tag == wanted), None)

    def allows_tool(self, name: str) -> bool:
        return name in self.enabled_tools


def build_snapshot(
    *,
    version: AgentVersion,
    enabled_tool_names: Iterable[str],
    knowledge_base_ids: Iterable[KnowledgeBaseId],
    registry: ToolRegistry,
    organization_instructions: str | None = None,
) -> AgentSnapshot:
    """Build a snapshot from a **published** agent version. Pure and deterministic.

    Args:
        version: The agent version. Must be published — see below.
        enabled_tool_names: Tool names enabled on this version, from
            `agent_tool_configs`. Names with no registered tool are dropped, so a
            configuration row that outlived its tool cannot take a tenant's calls
            down; the dispatcher refuses the tool if the model asks anyway.
        knowledge_base_ids: Knowledge bases bound to this version.
        registry: The tool registry the enabled names are resolved against.
        organization_instructions: Instruction layer 2. **No column stores this
            yet** — organization-level instructions arrive with organization
            settings in a later phase. The parameter exists so that layer ordering
            is implemented and tested now rather than retrofitted into a composed
            prefix later.

    Raises:
        SnapshotResolutionError: if the version is not published. Enforced here as
            well as in the service that loads it: two independent refusals, because
            serving a draft configuration on a real call is unrecoverable.
        AgentConfigurationError: if stored JSONB configuration is malformed.
    """
    if not version.is_published:
        raise SnapshotResolutionError(
            "Only a published agent version can serve a conversation.",
            detail={
                "agent_version_id": str(version.id),
                "status": version.status.value,
            },
        )

    instruction_prefix = compose_instruction_prefix(
        organization_layer=organization_instructions,
        agent_layer=version.instructions,
    )
    turn_policy = _parse_turn_policy(version.turn_policy)
    guardrail_config = _parse_guardrail_config(version.guardrail_config)
    voice_map = _parse_voice_map(version.voice_map)

    # Sorted, so the same input set always produces the same snapshot. An
    # unordered `set` iteration order is stable within a process but is not a
    # property to build a content hash on.
    enabled = frozenset(name for name in enabled_tool_names if name in registry.names)
    tool_specs_json = registry.to_realtime_specs_json(enabled)
    bindings = tuple(sorted(knowledge_base_ids, key=str))

    content_hash = _content_hash(
        agent_version_id=version.id,
        organization_id=version.organization_id,
        instruction_prefix=instruction_prefix,
        language_policy=version.language_policy,
        turn_policy=turn_policy,
        guardrail_config=guardrail_config,
        enabled=enabled,
        tool_specs_json=tool_specs_json,
        voice_map=voice_map,
        bindings=bindings,
        realtime_model=version.realtime_model,
        telephony_sample_rate=version.telephony_sample_rate,
    )

    return AgentSnapshot(
        agent_version_id=version.id,
        organization_id=version.organization_id,
        agent_id=version.agent_id,
        version_number=version.version_number,
        instruction_prefix=instruction_prefix,
        language_policy=version.language_policy,
        turn_policy=turn_policy,
        guardrail_config=guardrail_config,
        enabled_tools=enabled,
        tool_specs_json=tool_specs_json,
        voice_map=voice_map,
        knowledge_base_ids=bindings,
        realtime_model=version.realtime_model,
        telephony_sample_rate=version.telephony_sample_rate,
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# The translation boundary: JSONB -> typed, immutable configuration.
#
# Nothing above this point in the call graph sees `dict[str, Any]`. Stored tenant
# configuration is untrusted input like any other: it is parsed here, once, and a
# malformed value fails with a typed error rather than surfacing three layers up
# as a `KeyError` during a call.
# ---------------------------------------------------------------------------


def _parse_turn_policy(raw: Mapping[str, Any]) -> TurnPolicy:
    known = {"mode", "eagerness", "parameters"}
    mode = _optional_str(raw, "mode") or "semantic_vad"
    eagerness = _optional_str(raw, "eagerness") or "low"

    parameters_raw = raw.get("parameters", {})
    if not isinstance(parameters_raw, Mapping):
        raise AgentConfigurationError(
            "Stored turn policy has a malformed 'parameters' object.",
            detail={"type": type(parameters_raw).__name__},
        )
    # Anything outside the known keys is folded into `parameters` rather than
    # dropped: a provider knob written by a future dashboard must survive a
    # publish through this version of the code.
    merged = {**{k: v for k, v in raw.items() if k not in known}, **parameters_raw}
    return TurnPolicy(
        mode=mode,
        eagerness=eagerness,
        parameters=_scalar_pairs(merged, field="turn_policy.parameters"),
    )


def _parse_guardrail_config(raw: Mapping[str, Any]) -> GuardrailConfig:
    known = {"max_turns", "max_invalid_tool_retries"}
    # Explicit `is None` rather than `or`: with `or`, a stored `max_turns: 0` would be
    # replaced by the default instead of being rejected, and a tenant who configured
    # something invalid would silently get something else. Absent means "use the
    # default"; present and out of range means "refuse", which `GuardrailConfig`
    # enforces.
    turns = _optional_int(raw, "max_turns")
    retries = _optional_int(raw, "max_invalid_tool_retries")
    return GuardrailConfig(
        max_turns=DEFAULT_CONVERSATION_TURNS if turns is None else turns,
        max_invalid_tool_retries=MAX_INVALID_TOOL_RETRIES if retries is None else retries,
        extra=_scalar_pairs(
            {k: v for k, v in raw.items() if k not in known},
            field="guardrail_config",
        ),
    )


def _parse_voice_map(raw: Mapping[str, Any]) -> tuple[tuple[str, VoiceRef], ...]:
    entries: list[tuple[str, VoiceRef]] = []
    for tag, value in raw.items():
        # Validates the tag rather than accepting any string: a voice keyed by
        # something that is not a language tag can never be selected, so it is a
        # configuration error and not a harmless extra row.
        LanguageTag(str(tag))
        if not isinstance(value, Mapping):
            raise AgentConfigurationError(
                "A voice map entry must be an object with 'provider' and 'voice_id'.",
                detail={"language": str(tag)},
            )
        provider = _optional_str(value, "provider")
        voice_id = _optional_str(value, "voice_id")
        if not provider or not voice_id:
            raise AgentConfigurationError(
                "A voice map entry needs both 'provider' and 'voice_id'.",
                detail={"language": str(tag)},
            )
        entries.append((str(tag), VoiceRef(provider=provider, voice_id=voice_id)))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _optional_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentConfigurationError(
            "Stored agent configuration field is not a string.",
            detail={"key": key, "type": type(value).__name__},
        )
    return value


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    # `bool` is an `int` in Python, and `max_turns: true` would silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentConfigurationError(
            "Stored agent configuration field is not an integer.",
            detail={"key": key, "type": type(value).__name__},
        )
    return value


def _scalar_pairs(raw: Mapping[str, Any], *, field: str) -> tuple[tuple[str, JsonScalar], ...]:
    """Sorted key/value pairs, scalars only.

    Sorted because this feeds `content_hash`: JSONB does not preserve key order,
    so two logically identical policies must not hash differently because Postgres
    returned their keys in a different order.
    """
    pairs: list[tuple[str, JsonScalar]] = []
    for key, value in raw.items():
        if value is not None and not isinstance(value, str | int | float | bool):
            raise AgentConfigurationError(
                "Stored agent configuration may only hold scalar values here.",
                detail={"field": field, "key": str(key), "type": type(value).__name__},
            )
        pairs.append((str(key), value))
    return tuple(sorted(pairs, key=lambda item: item[0]))


# ---------------------------------------------------------------------------
# Canonical serialisation and the content hash.
# ---------------------------------------------------------------------------


def _content_hash(
    *,
    agent_version_id: AgentVersionId,
    organization_id: OrganizationId,
    instruction_prefix: str,
    language_policy: LanguagePolicy,
    turn_policy: TurnPolicy,
    guardrail_config: GuardrailConfig,
    enabled: frozenset[str],
    tool_specs_json: str,
    voice_map: Sequence[tuple[str, VoiceRef]],
    bindings: Sequence[KnowledgeBaseId],
    realtime_model: str | None,
    telephony_sample_rate: int | None,
) -> str:
    """SHA-256 over the canonical document.

    **No timestamp and no random value participates**, which is what makes the hash
    reproducible for a given version. `agent_version_id` and `organization_id` do
    participate, so the hash is safe to use as a cache key: two different versions
    with identical behaviour still hash differently.
    """
    document = {
        "schema_version": CONTENT_HASH_SCHEMA_VERSION,
        "agent_version_id": str(agent_version_id),
        "organization_id": str(organization_id),
        "instruction_prefix": instruction_prefix,
        "language_policy": language_policy.to_storage(),
        "turn_policy": {
            "mode": turn_policy.mode,
            "eagerness": turn_policy.eagerness,
            "parameters": [list(pair) for pair in turn_policy.parameters],
        },
        "guardrail_config": {
            "max_turns": guardrail_config.max_turns,
            "max_invalid_tool_retries": guardrail_config.max_invalid_tool_retries,
            "extra": [list(pair) for pair in guardrail_config.extra],
        },
        "enabled_tools": sorted(enabled),
        # The already-canonical spec text, embedded as a string. Re-encoding the
        # parsed objects here would mean two canonicalisations to keep in step.
        "tool_specs": tool_specs_json,
        "voice_map": [[tag, ref.provider, ref.voice_id] for tag, ref in voice_map],
        "knowledge_base_ids": [str(kb) for kb in bindings],
        "realtime_model": realtime_model,
        "telephony_sample_rate": telephony_sample_rate,
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
