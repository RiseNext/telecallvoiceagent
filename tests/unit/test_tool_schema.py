"""The flat tool schema. HC-19's silent failure, made loud.

> Realtime declares tools **flat** — `{"type","name","description","parameters"}`
> with the properties at the top level. `convert_to_openai_tool` returns the
> **nested** Chat-Completions shape, `{"type":"function","function":{...}}`, which the
> session accepts and the model then never calls a tool from.

The symptom is "the agent won't use its tools" or "the agent invented a price", which
reads as a prompt problem and costs about a day. So the shape is pinned here, exactly,
including the assertion that `"function"` is not a *key*.

`anyOf` is asserted as Pydantic emits it rather than normalised. OpenAI's `strict`
schema subset — and whether Realtime honours it identically to Chat Completions — is
UNVERIFIED (AGENT_ARCHITECTURE §3.3/§12) and the beta interface most examples describe
was removed in May 2026. Pinning the current shape makes a future change visible;
inventing a normalisation against an unverified constraint would not.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from rn_agent.errors import ToolRegistrationError
from rn_agent.tools import REGISTRY
from rn_agent.tools.base import INJECTED_CONTEXT_KEYS, ToolArgs
from rn_agent.tools.schema import MAX_NODE_DEPTH, build_flat_tool_spec, inline_schema_refs

pytestmark = [pytest.mark.unit]


class _Nested(BaseModel):
    label: str
    weight: int = 1


class _WithNested(ToolArgs):
    item: _Nested = Field(description="A nested object.")
    optional_note: str | None = Field(default=None, description="Optional free text.")


class _Simple(ToolArgs):
    query: str = Field(description="What to look for.")


class _Empty(ToolArgs):
    pass


def _spec(args: type[ToolArgs], name: str = "some_tool") -> dict[str, Any]:
    return build_flat_tool_spec(
        name=name,
        description="A description long enough to be useful to the model.",
        args_schema=args.model_json_schema(),
    )


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def test_the_spec_is_flat_not_nested() -> None:
    spec = _spec(_Simple)
    assert set(spec) == {"type", "name", "description", "parameters"}
    # The specific failure HC-19 describes: a `function` KEY means the nested
    # Chat-Completions shape reached the Realtime session.
    assert "function" not in spec
    assert spec["type"] == "function"
    assert spec["parameters"]["properties"]["query"]["type"] == "string"


def test_extra_keys_are_forbidden_in_the_generated_schema() -> None:
    """`extra="forbid"` is what turns an invented field into a validation failure."""
    assert _spec(_Simple)["parameters"]["additionalProperties"] is False


def test_an_argument_less_tool_still_has_a_properties_object() -> None:
    """Pydantic omits `properties` for an empty model; a provider expects the key."""
    assert _spec(_Empty)["parameters"]["properties"] == {}


def test_field_descriptions_survive_into_the_spec() -> None:
    """Descriptions are prompt surface — losing them silently degrades tool choice."""
    assert _spec(_Simple)["parameters"]["properties"]["query"]["description"] == (
        "What to look for."
    )


# ---------------------------------------------------------------------------
# $ref inlining
# ---------------------------------------------------------------------------


def test_refs_are_inlined_and_defs_removed() -> None:
    spec = _spec(_WithNested)
    parameters = spec["parameters"]
    assert "$defs" not in parameters
    assert _no_refs(parameters), parameters
    item = parameters["properties"]["item"]
    assert item["properties"]["label"]["type"] == "string"


def test_a_description_beside_a_ref_is_preserved() -> None:
    """Pydantic emits `{"$ref": ..., "description": ...}` for an annotated nested
    field. Dropping the sibling would discard prompt surface written on purpose."""
    item = _spec(_WithNested)["parameters"]["properties"]["item"]
    assert item["description"] == "A nested object."


def test_optional_fields_keep_pydantics_anyof_shape() -> None:
    """Pinned, not normalised. See the module docstring."""
    note = _spec(_WithNested)["parameters"]["properties"]["optional_note"]
    assert [entry["type"] for entry in note["anyOf"]] == ["string", "null"]


def test_nested_titles_are_stripped() -> None:
    """`title` is Pydantic's class name — internal naming leaking into prompt text."""
    spec = _spec(_WithNested)
    assert "title" not in spec["parameters"]
    assert "title" not in spec["parameters"]["properties"]["item"]


def test_an_unresolvable_ref_fails_at_registration() -> None:
    with pytest.raises(ToolRegistrationError):
        inline_schema_refs({"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}})


def test_an_external_ref_is_refused() -> None:
    """We do not fetch schemas, and a provider is not documented to dereference."""
    with pytest.raises(ToolRegistrationError):
        inline_schema_refs({"type": "object", "properties": {"x": {"$ref": "http://example/x"}}})


def test_a_self_referential_model_is_refused_rather_than_looping() -> None:
    """The reason `$ref` expansion needs a bound at all.

    Pydantic will happily generate a recursive schema; inlining one without a bound
    does not terminate. Refused at registration, which is where a tool that cannot be
    exported should fail.
    """

    class _Recursive(ToolArgs):
        child: _Recursive | None = None

    _Recursive.model_rebuild()
    with pytest.raises(ToolRegistrationError) as caught:
        _spec(_Recursive, name="recursive_tool")
    assert "max_ref_depth" in caught.value.detail


def test_pathological_json_nesting_is_refused() -> None:
    """A hand-written schema, not something Pydantic produces. Runaway guard only."""
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_NODE_DEPTH + 3):
        schema = {"type": "object", "properties": {"deeper": schema}}
    with pytest.raises(ToolRegistrationError):
        inline_schema_refs(schema)


def test_an_ordinary_nested_model_is_not_refused() -> None:
    """Guards the bound against being tightened into something that rejects real tools."""
    assert _spec(_WithNested)["parameters"]["properties"]["item"]["type"] == "object"


def test_a_non_object_schema_is_refused() -> None:
    with pytest.raises(ToolRegistrationError):
        build_flat_tool_spec(
            name="bad_tool",
            description="A description long enough to be useful to the model.",
            args_schema={"type": "array", "items": {"type": "string"}},
        )


# ---------------------------------------------------------------------------
# What must never be in a model-visible schema
# ---------------------------------------------------------------------------


def test_no_registered_tool_exposes_injected_context() -> None:
    """The whole tenancy story in one assertion.

    `ToolRuntime` is a separate parameter rather than a field of `ToolArgs`, so it
    cannot be in a generated schema. This asserts the consequence for every tool at
    once, so a future tool that declared `organization_id` as an argument fails here
    even if it somehow passed registration.
    """
    for spec in REGISTRY.specs():
        properties = set(spec.realtime_spec["parameters"].get("properties", {}))
        assert not properties & INJECTED_CONTEXT_KEYS, spec.name
        assert "runtime" not in properties
        assert "rt" not in properties


def test_every_registered_tool_exports_a_valid_flat_spec() -> None:
    """A meta-test: adding a tool means adding it to this assertion for free."""
    for spec in REGISTRY.specs():
        exported = spec.realtime_spec
        assert set(exported) == {"type", "name", "description", "parameters"}, spec.name
        assert exported["name"] == spec.name
        assert _no_refs(exported), spec.name


def _no_refs(node: Any) -> bool:
    if isinstance(node, dict):
        return "$ref" not in node and all(_no_refs(value) for value in node.values())
    if isinstance(node, list):
        return all(_no_refs(item) for item in node)
    return True


def test_enums_arrays_and_constraints_export_correctly() -> None:
    """Covers the field kinds a real tool will use, which the built-ins do not exercise.

    An enum is a `$ref` in Pydantic's output, so this is also the case where inlining
    has to preserve the values rather than just resolving the reference.
    """
    from enum import StrEnum

    class Channel(StrEnum):
        SMS = "sms"
        WHATSAPP = "whatsapp"

    class _Rich(ToolArgs):
        channel: Channel = Field(description="Which channel to use.")
        tags: list[str] = Field(default_factory=list, max_length=5, description="Labels.")
        count: int = Field(default=1, ge=1, le=10, description="How many.")

    properties = _spec(_Rich, name="rich_tool")["parameters"]["properties"]

    # The enum was a $ref; inlining must keep its values, not just resolve the pointer.
    assert properties["channel"]["enum"] == ["sms", "whatsapp"]
    assert properties["channel"]["type"] == "string"
    assert "$ref" not in str(properties["channel"])
    # Array element type and bound survive.
    assert properties["tags"]["type"] == "array"
    assert properties["tags"]["items"] == {"type": "string"}
    assert properties["tags"]["maxItems"] == 5
    # Numeric bounds survive — they are what keeps a coerced value in range.
    assert properties["count"]["minimum"] == 1
    assert properties["count"]["maximum"] == 10
    # Only genuinely required fields are required.
    assert _spec(_Rich, name="rich_tool")["parameters"]["required"] == ["channel"]
