"""Pydantic model -> flat provider tool spec.

> **The trap this file exists for (HC-19, AGENT_ARCHITECTURE §3.2).** OpenAI
> Realtime declares tools **flat**:
>
>     {"type": "function", "name": ..., "description": ..., "parameters": {...}}
>
> `langchain_core.utils.function_calling.convert_to_openai_tool()` returns the
> **nested** Chat-Completions shape, `{"type": "function", "function": {...}}`,
> and it does not work against Realtime. It fails *silently*: the session accepts
> the payload and the model simply never calls the tool. The symptom looks like a
> prompt problem — "the agent won't use its tools", "the agent invented a price" —
> and costs about a day.

Two defences, and only the second is durable:

1. This module builds the spec from `model_json_schema()` directly. No LangChain
   code is importable from `rn_agent` at all — an import-linter contract forbids
   it — so the wrong function is not reachable.
2. `test_tool_schema.py` asserts the exact top-level key set, and asserts that
   `"function"` is not among the keys. A refactor that reintroduces nesting fails
   in CI rather than on a live call.

**`$ref` is inlined; `anyOf` is left exactly as Pydantic emits it.** Inlining is
required — the provider is not documented to dereference `$defs`. Normalising
`anyOf` is *not* done, deliberately: OpenAI's `strict` schema subset and whether
Realtime honours it identically to Chat Completions is listed UNVERIFIED in
AGENT_ARCHITECTURE §3.3/§12, and the beta interface that most examples describe
was removed in May 2026. Inventing a normalisation against an unverified
constraint is how a plausible-looking payload becomes a silent failure. The shape
is pinned by a test so that a future change is a visible decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rn_agent.errors import ToolRegistrationError

__all__ = [
    "MAX_NODE_DEPTH",
    "MAX_REF_DEPTH",
    "build_flat_tool_spec",
    "canonical_json",
    "inline_schema_refs",
]


def canonical_json(value: Any) -> str:
    """The one canonical JSON encoding used for tool specs and snapshot hashing.

    `sort_keys` so mapping order cannot change the bytes; tight separators so
    whitespace cannot; `ensure_ascii=False` so a Devanagari or Telugu description is
    encoded as itself rather than as escapes, which keeps output stable across Python
    versions that might change escaping behaviour.

    Lives here rather than in `snapshot` because the tool layer canonicalises first —
    and two canonicalisations that must agree are one too many.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: How many levels of `$ref` expansion are permitted.
#:
#: This is the bound that matters. Inlining a self-referential model — `child:
#: "Self | None"` — would not terminate without it, and Pydantic will happily
#: generate that schema. Three levels admits a model containing a model containing a
#: model, which is already deeper than §3.3 recommends ("keep argument types shallow
#: and primitive; deeply nested objects raise argument-error rates for marginal
#: expressiveness"), and refuses the rest.
MAX_REF_DEPTH = 3

#: A structural guard on raw JSON nesting, independent of `$ref`.
#:
#: Deliberately generous: a legitimate schema for a two-level model is already about
#: eight nodes deep once `properties` / `anyOf` / `items` wrappers are counted, so a
#: tight bound here would reject ordinary tools. This exists only so a pathological
#: hand-written schema cannot turn inlining into an unbounded walk. It is not the
#: shallow-arguments rule — `MAX_REF_DEPTH` is.
MAX_NODE_DEPTH = 24

#: JSON Schema keys Pydantic emits that are noise on the wire. `title` is derived
#: from the class name and leaks internal naming into prompt surface for no
#: benefit; `$defs` is empty once refs are inlined.
_STRIPPED_KEYS = ("title", "$defs")


def inline_schema_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve every `$ref` against `$defs`, returning a self-contained schema.

    Raises:
        ToolRegistrationError: on an unresolvable reference, on more than
            `MAX_REF_DEPTH` levels of `$ref` expansion — which is what a recursive
            model produces — or on JSON nesting beyond `MAX_NODE_DEPTH`.
    """
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, Mapping):
        raise ToolRegistrationError("Tool argument schema has a malformed $defs section.")
    resolved = _inline(schema, definitions, depth=0, ref_depth=0)
    # `_inline` walks heterogeneous JSON and is typed accordingly. Narrowing here is
    # a real check rather than a cast: a top-level schema that is not an object is a
    # declaration mistake, and it must fail at import with a useful message.
    if not isinstance(resolved, dict):
        raise ToolRegistrationError(
            "Tool argument schema did not resolve to an object.",
            detail={"type": type(resolved).__name__},
        )
    for key in _STRIPPED_KEYS:
        resolved.pop(key, None)
    return resolved


def _inline(node: Any, definitions: Mapping[str, Any], *, depth: int, ref_depth: int) -> Any:
    """Walk the schema, expanding `$ref`.

    Two counters, measuring two different things. `depth` is raw JSON nesting and is
    only a runaway guard. `ref_depth` counts `$ref` expansions and is the one that
    makes a recursive model terminate — conflating them would reject an ordinary
    one-level-nested model, because `properties` / `anyOf` / `items` wrappers already
    account for most of the JSON depth.
    """
    if depth > MAX_NODE_DEPTH:
        raise ToolRegistrationError(
            "Tool argument schema is nested too deeply to export.",
            detail={"max_node_depth": MAX_NODE_DEPTH},
        )
    if isinstance(node, Mapping):
        reference = node.get("$ref")
        if isinstance(reference, str):
            if ref_depth >= MAX_REF_DEPTH:
                raise ToolRegistrationError(
                    "Tool argument schema nests models too deeply, or refers to itself. "
                    "Keep tool arguments shallow and primitive.",
                    detail={"max_ref_depth": MAX_REF_DEPTH, "ref": reference},
                )
            target = _lookup(reference, definitions)
            # Sibling keys alongside a `$ref` (a `description` on a nested model
            # field, typically) must survive: the referenced definition supplies
            # the shape, the siblings refine it, and dropping them would discard
            # prompt surface that was written on purpose.
            merged = {k: v for k, v in node.items() if k != "$ref"}
            inlined = _inline(target, definitions, depth=depth, ref_depth=ref_depth + 1)
            inlined.update(_inline(merged, definitions, depth=depth, ref_depth=ref_depth + 1))
            return inlined
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            if key == "title" and depth > 0:
                # Keep the top-level strip explicit in `inline_schema_refs`; drop
                # nested titles here, where they are always Pydantic's class name.
                continue
            result[key] = _inline(value, definitions, depth=depth + 1, ref_depth=ref_depth)
        return result
    if isinstance(node, list):
        return [_inline(item, definitions, depth=depth + 1, ref_depth=ref_depth) for item in node]
    return node


def _lookup(reference: str, definitions: Mapping[str, Any]) -> Mapping[str, Any]:
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        raise ToolRegistrationError(
            "Tool argument schema contains a reference we do not resolve.",
            detail={"ref": reference},
        )
    target = definitions.get(reference.removeprefix(prefix))
    if not isinstance(target, Mapping):
        raise ToolRegistrationError(
            "Tool argument schema references a definition that is not present.",
            detail={"ref": reference},
        )
    return target


def build_flat_tool_spec(
    *, name: str, description: str, args_schema: Mapping[str, Any]
) -> dict[str, Any]:
    """The flat provider spec for one tool.

    Args:
        args_schema: The output of `SomeToolArgs.model_json_schema()`.

    Raises:
        ToolRegistrationError: if a `$ref` survives inlining, or if the schema is
            not an object schema. Both are import-time failures by design.
    """
    parameters = inline_schema_refs(args_schema)

    # A tool whose parameters are not an object cannot be called: the provider
    # sends a JSON object of named arguments, so anything else is a declaration
    # mistake rather than an exotic-but-valid schema.
    if parameters.get("type") != "object":
        raise ToolRegistrationError(
            "Tool arguments must be an object schema.",
            detail={"tool": name, "type": str(parameters.get("type"))},
        )
    if _contains_ref(parameters):
        raise ToolRegistrationError(
            "Tool argument schema still contains a $ref after inlining.",
            detail={"tool": name},
        )

    # An argument-less tool: Pydantic omits `properties` entirely for an empty
    # model, and a provider reading `parameters` expects the key to exist.
    parameters.setdefault("properties", {})

    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": parameters,
    }


def _contains_ref(node: Any) -> bool:
    if isinstance(node, Mapping):
        return "$ref" in node or any(_contains_ref(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_ref(item) for item in node)
    return False
