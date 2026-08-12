"""The bake-off entry point.

    # Cost estimate only. No embedding of any kind, no network, no cost.
    uv run python -m tests.d8_bakeoff.run --estimate-only

    # Offline run: the deterministic fake and the lexical baseline.
    uv run python -m tests.d8_bakeoff.run

    # Paid run. Needs BOTH of these, and refuses without either.
    RN_D8_ALLOW_PAID=1 OPENAI_API_KEY=sk-... \\
        uv run python -m tests.d8_bakeoff.run --paid

**Three independent guards stand between this module and a charge**, deliberately
mirroring how `live` tests are guarded (TESTING §13, which uses three for the same
reason — one is not enough for something that spends money):

1. paid candidates run only with `--paid`;
2. `--paid` additionally requires `RN_D8_ALLOW_PAID=1` in the environment;
3. and an API key must be present, or the run refuses rather than half-running.

The cost estimate is printed before any paid call and the run stops for confirmation
unless `--yes-i-approve-the-cost` is passed, so the first paid invocation cannot be an
accident and the approving human sees the number they are approving.

`sys.stdout.write` rather than `print`: ruff's T20 rule bans `print` repository-wide,
including here, because a stray debug print in a structured-logging codebase is noise
nobody attributes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from rn_domain.chunking import FROZEN_CHUNKING_V1
from rn_providers.embeddings import EmbeddingProvider
from rn_providers.fakes import FakeEmbeddingProvider
from tests.d8_bakeoff.candidates import (
    CANDIDATES,
    TARGET_PASSAGES,
    TARGET_QUERIES,
    Candidate,
    CandidateKind,
    CostEstimate,
    estimate_cost,
    offline_candidates,
    paid_candidates,
    project_to_target,
)
from tests.d8_bakeoff.dataset import Dataset, load_dataset
from tests.d8_bakeoff.harness import CandidateResult, run_candidate
from tests.d8_bakeoff.report import (
    build_report,
    candidate_cost_rows,
    render_markdown,
    write_report,
)

_ALLOW_PAID_ENV = "RN_D8_ALLOW_PAID"
_API_KEY_ENV = "OPENAI_API_KEY"


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _build_provider(candidate: Candidate) -> EmbeddingProvider:
    """Construct the provider for a candidate.

    The vendor adapter is imported **inside this function**, not at module scope. That
    keeps `import tests.d8_bakeoff.run` free of `httpx` and, more importantly, keeps
    the offline path from touching a module whose whole purpose is to make network
    calls.
    """
    if candidate.kind is CandidateKind.OFFLINE_DETERMINISTIC:
        width = candidate.effective_dimensions
        if width is None:
            raise ValueError(f"Offline candidate {candidate.key} declares no width.")
        return FakeEmbeddingProvider(dimensions=width, model_id=f"fake-{candidate.key}")

    if candidate.kind is not CandidateKind.PAID_API:
        raise ValueError(f"Candidate {candidate.key} has no provider implementation.")

    from rn_providers.openai_embeddings import OpenAIEmbeddingProvider

    api_key = os.environ.get(_API_KEY_ENV, "")
    if not api_key.strip():
        raise ValueError(
            f"{_API_KEY_ENV} is not set, so paid candidate {candidate.key} cannot run. "
            "Refusing rather than producing a partial report."
        )
    if candidate.model_id is None:
        raise ValueError(f"Paid candidate {candidate.key} declares no model id.")
    return OpenAIEmbeddingProvider(
        api_key=api_key,
        model=candidate.model_id,
        dimensions=candidate.dimensions,
    )


def _selected(args: argparse.Namespace) -> tuple[Candidate, ...]:
    if args.paid:
        # Offline candidates are kept in a paid run on purpose: the lexical baseline is
        # the number every paid candidate has to beat, and comparing against a baseline
        # measured in a different run is comparing against a different dataset load.
        return (*offline_candidates(), *paid_candidates())
    return offline_candidates()


def _check_paid_permission(args: argparse.Namespace) -> None:
    if not args.paid:
        return
    if os.environ.get(_ALLOW_PAID_ENV) != "1":
        raise SystemExit(
            f"--paid requires {_ALLOW_PAID_ENV}=1 in the environment. This is the "
            "second of three guards; it exists so that a shell history replay cannot "
            "spend money."
        )
    if not os.environ.get(_API_KEY_ENV, "").strip():
        raise SystemExit(f"--paid requires {_API_KEY_ENV} to be set.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tests.d8_bakeoff.run",
        description="Run the D-8 embedding bake-off. Paid candidates are opt-in.",
    )
    parser.add_argument(
        "--paid",
        action="store_true",
        help=f"include paid API candidates (also needs {_ALLOW_PAID_ENV}=1)",
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="print the cost estimate and exit without embedding anything",
    )
    parser.add_argument(
        "--yes-i-approve-the-cost",
        action="store_true",
        help="skip the interactive confirmation before a paid run",
    )
    parser.add_argument(
        "--require-decision-grade",
        action="store_true",
        help="exit non-zero unless at least one result may inform ADR-011",
    )
    return parser


def _describe_dataset(dataset: Dataset) -> None:
    _out(
        f"dataset v{dataset.version}: {len(dataset.passages)} passages, "
        f"{len(dataset.queries)} queries, {len(dataset.subsets)} subsets"
    )
    _out(f"chunking policy: {FROZEN_CHUNKING_V1.version}")
    _out(f"dataset review-complete: {'yes' if dataset.is_review_complete else 'NO'}")
    for subset in dataset.subsets:
        readiness = dataset.readiness(subset)
        state = "review-complete" if readiness.is_review_complete else "AWAITING REVIEW"
        _out(f"  {subset.value:16} {readiness.query_count:3} queries   {state}")
    _out("")


def _print_costs(dataset: Dataset) -> list[CostEstimate]:
    """Print the on-dataset estimate and the projection to target size.

    Returns the on-dataset estimates so the caller can attach them to the report.
    """
    estimates = [estimate_cost(candidate, dataset) for candidate in CANDIDATES]
    _table(
        "estimated cost on THIS dataset (token bands are ESTIMATES, not measurements):", estimates
    )

    # The seed dataset is far smaller than the target, so its cost understates the real
    # bill by that factor. The projection is the number an approval decision needs:
    # approving cents while the real run costs dollars is approving the wrong thing.
    _table(
        f"PROJECTED cost at target size ({TARGET_PASSAGES} passages, "
        f"{TARGET_QUERIES} queries) - assumes the same length and script mix:",
        [project_to_target(estimate, dataset) for estimate in estimates],
    )
    return estimates


def _table(heading: str, estimates: Sequence[CostEstimate]) -> None:
    _out(heading)
    for key, tokens, money in candidate_cost_rows(CANDIDATES, estimates):
        _out(f"  {key:32} {tokens:>22} tokens   {money}")
    low = sum(estimate.usd_low for estimate in estimates if not estimate.is_free)
    high = sum(estimate.usd_high for estimate in estimates if not estimate.is_free)
    _out(f"  {'TOTAL, all paid candidates':32} {'':>22}          ${low:.4f}-${high:.4f}")
    _out("")


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    dataset = load_dataset()
    _describe_dataset(dataset)
    estimates = _print_costs(dataset)

    if args.estimate_only:
        _out("--estimate-only: nothing was embedded and nothing was charged.")
        return 0

    _check_paid_permission(args)
    if args.paid and not args.yes_i_approve_the_cost:
        raise SystemExit(
            "Refusing a paid run without --yes-i-approve-the-cost. The estimate above "
            "is what you would be approving."
        )

    selected = _selected(args)
    _out(f"running {len(selected)} candidate(s): {', '.join(c.key for c in selected)}")

    results: list[CandidateResult] = []
    for candidate in selected:
        result = await run_candidate(
            candidate=candidate,
            dataset=dataset,
            provider_factory=(
                None if candidate.kind is CandidateKind.LEXICAL_BASELINE else _build_provider
            ),
        )
        results.append(result)
        grade = "decision-grade" if result.is_decision_grade else "NOT decision-grade"
        _out(f"  {candidate.key:32} {result.chunk_count:4} chunks   {grade}")

    report = build_report(
        dataset=dataset,
        results=results,
        cost_estimates=[e for e in estimates if e.candidate_key in {c.key for c in selected}],
        chunking_policy_version=FROZEN_CHUNKING_V1.version,
    )
    path = write_report(report)
    markdown = path.with_suffix(".md")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    _out("")
    _out(f"wrote {path}")
    _out(f"wrote {markdown}")

    if args.require_decision_grade and not report.is_decision_grade:
        _out("")
        _out("FAILED --require-decision-grade: no result in this report may inform ADR-011.")
        for note in report.readiness_notes:
            _out(f"  - {note}")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
