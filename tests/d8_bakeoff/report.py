"""The D-8 result artifact.

One JSON file per run, written to `tests/d8_bakeoff/results/`, plus a Markdown
summary a non-engineer can read. ADR-011 quotes from the JSON and cites its path;
that is the whole point of writing a file rather than printing a table.

**What every report carries, whether or not anyone wants it to:**

* the dataset version and the chunking policy version — a score is meaningless
  without both, and "we re-chunked halfway through" is otherwise undetectable;
* `decision_grade`, computed by the harness, plus every reason it is false;
* the per-subset breakdown, with the **worst** subset called out first;
* the acceptance gates from ADR-010's successor plan, evaluated, so the report says
  which gates a candidate cleared rather than leaving that to a reader's arithmetic.

**Gate thresholds live here, and they were fixed before any candidate ran.** That is
the only property that makes them gates rather than post-hoc rationalisation. Editing
one after seeing results is a reviewable act in a diff, which is exactly the friction
intended.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rn_core.clock import now_utc
from tests.d8_bakeoff.candidates import Candidate, CostEstimate
from tests.d8_bakeoff.dataset import Dataset
from tests.d8_bakeoff.harness import (
    CandidateResult,
    pooled_answerability,
    worst_subset_answerability,
)
from tests.d8_bakeoff.metrics import REPORTED_K

__all__ = [
    "GATE_ANSWERABILITY_CROSS_SCRIPT",
    "GATE_ANSWERABILITY_PER_SUBSET",
    "GATE_K",
    "RESULTS_DIR",
    "GateOutcome",
    "Report",
    "build_report",
    "candidate_cost_rows",
    "evaluate_gates",
    "render_markdown",
    "write_report",
]

RESULTS_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "results"

#: The `k` the quality gate is evaluated at — the likely production value.
GATE_K: Final[int] = 8

#: **Gate G1, fixed in advance.** `answerability@8` on every language subset.
GATE_ANSWERABILITY_PER_SUBSET: Final[float] = 0.90
#: Cross-script is held to a lower bar because it is the hardest case and no provider
#: publishes a number for it — but it is still a bar, and failing it is a finding.
GATE_ANSWERABILITY_CROSS_SCRIPT: Final[float] = 0.85


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """Whether one candidate cleared one gate, with the number that decided it."""

    gate: str
    passed: bool
    detail: str

    def as_mapping(self) -> Mapping[str, Any]:
        return {"gate": self.gate, "passed": self.passed, "detail": self.detail}


def evaluate_gates(result: CandidateResult) -> tuple[GateOutcome, ...]:
    """Evaluate the pre-fixed acceptance gates against one candidate's scores.

    Only the gates this harness can answer are evaluated here. **G2 (latency at
    corpus scale), G3 (halfvec recall delta) and G5 (partitioning) require a Postgres
    schema that does not exist yet**, and G0 (servability) is a human judgement — so
    they are reported as `not_evaluated` rather than silently passed. A report that
    showed four green gates and omitted the other four would read as a full pass.
    """
    outcomes: list[GateOutcome] = []

    if not result.subset_scores:
        return (GateOutcome(gate="G1-quality", passed=False, detail="no subsets were scored"),)

    failures: list[str] = []
    for name, score in sorted(result.subset_scores.items()):
        threshold = (
            GATE_ANSWERABILITY_CROSS_SCRIPT
            if name == "cross-script"
            else GATE_ANSWERABILITY_PER_SUBSET
        )
        achieved = score.answerability_at(GATE_K)
        if achieved < threshold:
            failures.append(f"{name}={achieved:.3f} < {threshold:.2f}")
    outcomes.append(
        GateOutcome(
            gate=f"G1-quality (answerability@{GATE_K} per subset)",
            passed=not failures,
            detail="all subsets cleared" if not failures else "; ".join(failures),
        )
    )

    for gate, why in (
        ("G0-servability", "human judgement: can we serve this in V1 with no new infrastructure"),
        ("G2-latency", "needs the Postgres schema and a corpus at scale — Stage 2"),
        ("G3-halfvec-recall-delta", "needs a vector column to compare types — Stage 2"),
        ("G4-exact-vs-ann", "needs measured latency at realistic corpus size — Stage 2"),
        ("G5-partitioning", "needs a measured planner problem at real tenant count"),
        ("G6-coexistence", "follows from the chosen width; decided in ADR-011"),
    ):
        outcomes.append(GateOutcome(gate=gate, passed=False, detail=f"NOT EVALUATED — {why}"))

    return tuple(outcomes)


@dataclass(frozen=True, slots=True)
class Report:
    """One bake-off run."""

    generated_at: str
    dataset_version: int
    dataset_review_complete: bool
    chunking_policy_version: str
    results: tuple[CandidateResult, ...]
    cost_estimates: tuple[CostEstimate, ...]
    readiness_notes: tuple[str, ...]

    @property
    def decision_grade_results(self) -> tuple[CandidateResult, ...]:
        return tuple(result for result in self.results if result.is_decision_grade)

    @property
    def is_decision_grade(self) -> bool:
        """Whether **any** result in this report may inform ADR-011."""
        return bool(self.decision_grade_results)

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "generated_at": self.generated_at,
            "dataset_version": self.dataset_version,
            "dataset_review_complete": self.dataset_review_complete,
            "chunking_policy_version": self.chunking_policy_version,
            "report_is_decision_grade": self.is_decision_grade,
            "readiness_notes": list(self.readiness_notes),
            "cost_estimates": [
                {
                    "candidate": estimate.candidate_key,
                    "is_free": estimate.is_free,
                    "tokens_low": estimate.total_tokens_low,
                    "tokens_high": estimate.total_tokens_high,
                    "usd_low": round(estimate.usd_low, 6),
                    "usd_high": round(estimate.usd_high, 6),
                }
                for estimate in self.cost_estimates
            ],
            "results": [_result_mapping(result) for result in self.results],
        }


def _result_mapping(result: CandidateResult) -> Mapping[str, Any]:
    return {
        "candidate": result.candidate_key,
        "kind": result.candidate_kind.value,
        "model_id": result.model_id,
        "dimensions": result.dimensions,
        "chunks": result.chunk_count,
        "decision_grade": result.is_decision_grade,
        "non_decision_reasons": list(result.non_decision_reasons),
        "corpus_embed_seconds": round(result.corpus_embed_seconds, 4),
        "query_embed_seconds": round(result.query_embed_seconds, 4),
        "reported_prompt_tokens": result.reported_prompt_tokens,
        "gates": [outcome.as_mapping() for outcome in evaluate_gates(result)],
        "worst_subset": _worst(result),
        "pooled": {str(k): round(pooled_answerability(result, k), 4) for k in REPORTED_K},
        "subsets": {
            name: {
                "query_count": score.query_count,
                "answerability": {str(k): round(v, 4) for k, v in score.answerability.items()},
                "recall": {str(k): round(v, 4) for k, v in score.recall.items()},
                "ndcg": {str(k): round(v, 4) for k, v in score.ndcg.items()},
                "mrr": {str(k): round(v, 4) for k, v in score.mrr.items()},
            }
            for name, score in sorted(result.subset_scores.items())
        },
    }


def _worst(result: CandidateResult) -> Mapping[str, Any] | None:
    worst = worst_subset_answerability(result, GATE_K)
    if worst is None:
        return None
    subset, value = worst
    return {"subset": subset, f"answerability@{GATE_K}": round(value, 4)}


def build_report(
    *,
    dataset: Dataset,
    results: Sequence[CandidateResult],
    cost_estimates: Sequence[CostEstimate],
    chunking_policy_version: str,
) -> Report:
    """Assemble a report, including the dataset's own review-readiness notes."""
    notes = tuple(
        note
        for note in (dataset.readiness(subset).blocking_reason for subset in dataset.subsets)
        if note
    )
    return Report(
        # ISO-8601 UTC. `now_utc` rather than `datetime.now()` because ruff's DTZ rules
        # ban naive datetimes repository-wide, and a benchmark timestamp with no offset
        # is unusable for correlating a run with a provider incident.
        generated_at=now_utc().isoformat(),
        dataset_version=dataset.version,
        dataset_review_complete=dataset.is_review_complete,
        chunking_policy_version=chunking_policy_version,
        results=tuple(results),
        cost_estimates=tuple(cost_estimates),
        readiness_notes=notes,
    )


def write_report(report: Report, *, directory: pathlib.Path | None = None) -> pathlib.Path:
    """Write the JSON artifact and return its path.

    The filename carries the timestamp so runs accumulate rather than overwrite: a
    bake-off is a sequence of runs, and comparing today's numbers with last week's is
    the main thing anyone does with them.
    """
    root = directory or RESULTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.replace(":", "").replace("-", "").replace(".", "")
    grade = "decision" if report.is_decision_grade else "not-decision-grade"
    path = root / f"d8-{stamp}-{grade}.json"
    path.write_text(
        json.dumps(report.as_mapping(), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def render_markdown(report: Report) -> str:
    """A human-readable summary. Leads with what is *not* decided."""
    lines: list[str] = [
        "# D-8 embedding bake-off — run report",
        "",
        f"- generated: `{report.generated_at}`",
        f"- dataset version: `{report.dataset_version}`",
        f"- chunking policy: `{report.chunking_policy_version}`",
        f"- **decision-grade: {'YES' if report.is_decision_grade else 'NO'}**",
        "",
    ]
    if not report.is_decision_grade:
        lines += [
            "> **This report cannot be used to choose the production embedding model.**",
            "> No result in it is decision-grade. The reasons are listed per candidate",
            "> below; the usual ones are an offline candidate and unreviewed Indic",
            "> dataset material.",
            "",
        ]
    if report.readiness_notes:
        lines += ["## Awaiting native-speaker review", ""]
        lines += [f"- {note}" for note in report.readiness_notes]
        lines += [""]

    lines += [
        "## Estimated cost",
        "",
        "| candidate | tokens (low-high) | USD (low-high) |",
        "|---|---|---|",
    ]
    for estimate in report.cost_estimates:
        money = "free" if estimate.is_free else f"${estimate.usd_low:.4f}-${estimate.usd_high:.4f}"
        lines.append(
            f"| `{estimate.candidate_key}` | "
            f"{estimate.total_tokens_low:,}-{estimate.total_tokens_high:,} | {money} |"
        )
    lines += ["", "## Results", ""]

    for result in report.results:
        worst = worst_subset_answerability(result, GATE_K)
        lines += [
            f"### `{result.candidate_key}`",
            "",
            f"- kind: `{result.candidate_kind.value}`",
            f"- model: `{result.model_id}` at `{result.dimensions}` dims",
            f"- chunks indexed: {result.chunk_count}",
            f"- decision-grade: {'YES' if result.is_decision_grade else 'NO'}",
        ]
        if worst is not None:
            lines.append(
                f"- **worst subset: `{worst[0]}` at answerability@{GATE_K}={worst[1]:.3f}**"
            )
        for reason in result.non_decision_reasons:
            lines.append(f"  - not decision-grade: {reason}")
        lines += [
            "",
            f"| subset | queries | answerability@{GATE_K} | recall@{GATE_K} | nDCG@{GATE_K} |",
            "|---|---|---|---|---|",
        ]
        for name, score in sorted(result.subset_scores.items()):
            lines.append(
                f"| `{name}` | {score.query_count} | "
                f"{score.answerability.get(GATE_K, 0.0):.3f} | "
                f"{score.recall.get(GATE_K, 0.0):.3f} | "
                f"{score.ndcg.get(GATE_K, 0.0):.3f} |"
            )
        lines += ["", "Gates:", ""]
        for outcome in evaluate_gates(result):
            mark = "PASS" if outcome.passed else "----"
            lines.append(f"- `{mark}` **{outcome.gate}** — {outcome.detail}")
        lines += [""]

    return "\n".join(lines)


def candidate_cost_rows(
    candidates: Sequence[Candidate], estimates: Sequence[CostEstimate]
) -> tuple[tuple[str, str, str], ...]:
    """`(key, tokens, usd)` rows, for printing an approval request before spending."""
    by_key = {estimate.candidate_key: estimate for estimate in estimates}
    rows: list[tuple[str, str, str]] = []
    for candidate in candidates:
        estimate = by_key.get(candidate.key)
        if estimate is None:
            continue
        money = "free" if estimate.is_free else f"${estimate.usd_low:.4f}-${estimate.usd_high:.4f}"
        rows.append(
            (
                candidate.key,
                f"{estimate.total_tokens_low:,}-{estimate.total_tokens_high:,}",
                money,
            )
        )
    return tuple(rows)
