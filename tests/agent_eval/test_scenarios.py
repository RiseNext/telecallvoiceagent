"""The Tier-1 evaluation runner: one test per declarative scenario.

Reads `scenarios.yaml` and drives each scenario through the real conversation loop, the
real tool registry and the real dispatch pipeline. Only the *transport* is different
from a live call — which is exactly why the registry is framework-free and
transport-agnostic (AGENT_ARCHITECTURE §11.4).

**What this measures and what it does not.** The assistant turns are scripted, so this
measures the *platform*: that disclosure detection fires, that tools dispatch with
server-injected context, that forged tenant identity is discarded, that opt-out is
recognised independently of the model, that an unavailable tool produces a refusal
rather than an exception. It does **not** measure whether a real model would produce
those turns. That is Tier 2 — a real provider and a judge — and nothing here is
evidence about model quality, Hindi/Telugu fluency (PRD **D-2**) or latency.

Compliance, opt-out and injection resistance are **gates**: a scenario failing one is a
failure whatever else it did.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from rn_agent.conversation import ConversationResult, StopReason, run_text_conversation
from rn_agent.guardrails.disclosure import DisclosureKind
from rn_agent.snapshot import AgentSnapshot
from rn_agent.tools import REGISTRY
from rn_agent.tools.base import ToolOutcome, ToolRuntime
from rn_providers.fakes import FakeLLMProvider, ScriptedToolCall, ScriptedTurn

pytestmark = [pytest.mark.agent_eval]

_SCENARIO_FILE = pathlib.Path(__file__).parent / "scenarios.yaml"


def _load() -> tuple[list[dict[str, Any]], int]:
    document = yaml.safe_load(_SCENARIO_FILE.read_text(encoding="utf-8"))
    return list(document["scenarios"]), int(document["scenario_suite_version"])


_SCENARIOS, _SUITE_VERSION = _load()


def _script(scenario: dict[str, Any]) -> list[ScriptedTurn]:
    turns: list[ScriptedTurn] = []
    for turn in scenario["assistant_turns"]:
        calls = tuple(
            ScriptedToolCall(
                name=call["name"],
                arguments=call.get("arguments", {}) or {},
                arguments_json=call.get("arguments_json"),
            )
            for call in turn.get("tool_calls", []) or []
        )
        turns.append(ScriptedTurn(content=turn.get("text"), tool_calls=calls))
    return turns


async def _run(
    scenario: dict[str, Any], snapshot: AgentSnapshot, runtime: ToolRuntime
) -> ConversationResult:
    return await run_text_conversation(
        provider=FakeLLMProvider(_script(scenario)),
        snapshot=snapshot,
        runtime=runtime,
        registry=REGISTRY,
        caller_utterances=scenario["caller_utterances"],
    )


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[scenario["id"] for scenario in _SCENARIOS])
async def test_scenario(
    scenario: dict[str, Any], eval_snapshot: AgentSnapshot, eval_runtime: ToolRuntime
) -> None:
    """Run one scenario and assert every expectation it declares."""
    result = await _run(scenario, eval_snapshot, eval_runtime)
    expected = scenario["expect"]

    assert list(result.tool_names_called) == expected["tool_calls"]
    assert result.successful_tool_calls == expected["successful_tool_calls"]
    assert result.stop_reason is StopReason(expected["stop_reason"])

    # --- compliance gates ----------------------------------------------
    #
    # `disclosed` is the gate. Both `AFFIRMED_AI` ("I am an AI") and `DENIED_HUMAN`
    # ("I am not a human") satisfy the roadmap requirement, and a real reply to "are
    # you a human?" usually does both at once — so asserting one specific kind would
    # be asserting a phrasing rather than the obligation. `disclosure_kind` pins the
    # kind only where a scenario's reply is deliberately one-sided.
    assert result.disclosure.disclosed is expected["disclosed"], (
        f"scenario {scenario['id']}: AI disclosure gate failed"
    )
    if expected.get("disclosure_kind"):
        assert result.disclosure.kind is DisclosureKind(expected["disclosure_kind"])
    assert bool(result.opt_out and result.opt_out.matched) is expected["opt_out"], (
        f"scenario {scenario['id']}: opt-out gate failed"
    )
    if expected.get("opt_out_language"):
        assert result.opt_out is not None
        assert result.opt_out.language is not None
        assert result.opt_out.language.value == expected["opt_out_language"]

    if "denied_tool_calls" in expected:
        denied = sum(
            1 for record in result.tool_executions if record.envelope.outcome is ToolOutcome.DENIED
        )
        assert denied == expected["denied_tool_calls"]


# ---------------------------------------------------------------------------
# Suite-level gates. These assert properties of the whole scenario set, not of one
# scenario, and they are what stop the suite silently losing coverage.
# ---------------------------------------------------------------------------


def test_the_suite_covers_ai_disclosure_in_all_three_languages() -> None:
    """The Phase-2 definition-of-done gate, asserted as coverage rather than as a run.

    Every individual scenario could pass while the Telugu one had been quietly deleted.
    This is what notices.
    """
    covered = {
        scenario["language"]
        for scenario in _SCENARIOS
        if scenario["id"].startswith("are_you_human")
    }
    assert covered == {"en", "hi-IN", "te-IN"}, covered


def test_the_suite_covers_opt_out_in_all_three_languages() -> None:
    covered = {
        scenario["language"] for scenario in _SCENARIOS if scenario["id"].startswith("opt_out")
    }
    assert covered == {"en", "hi-IN", "te-IN"}, covered


def test_at_least_one_scenario_makes_two_or_more_tool_calls() -> None:
    """The roadmap wording exactly: "at least two tool calls, in CI, no network egress"."""
    assert any(len(scenario["expect"]["tool_calls"]) >= 2 for scenario in _SCENARIOS), (
        "no scenario exercises multiple tool calls"
    )


def test_the_suite_covers_injection_resistance() -> None:
    assert any("inject" in scenario["id"] for scenario in _SCENARIOS)


def test_every_scenario_declares_its_expectations() -> None:
    """A scenario with no expectations passes vacuously, which is worse than absent."""
    required = {"tool_calls", "successful_tool_calls", "disclosed", "stop_reason", "opt_out"}
    for scenario in _SCENARIOS:
        missing = required - set(scenario["expect"])
        assert not missing, f"{scenario['id']} is missing expectations: {sorted(missing)}"
        assert scenario["description"].strip(), f"{scenario['id']} has no description"


def test_the_suite_is_versioned() -> None:
    """A scenario change alters what a score means, so the suite carries a version."""
    assert _SUITE_VERSION >= 1
