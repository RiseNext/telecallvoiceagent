"""The harness, the candidate manifest, and the report — all offline.

The assertions that matter are the refusals. A bake-off harness that can be nudged into
producing a plausible-looking number from the wrong source is worse than no harness, so:

* an offline candidate can never be decision-grade;
* an unreviewed dataset subset can never be decision-grade;
* an embedding candidate with no provider **raises** rather than falling back to the
  lexical baseline, which would report trigram scores under a model's name;
* a local-model candidate raises, because it is declared for costing and not approved
  to run.

Nothing here touches a network. The paid candidates are never constructed.
"""

from __future__ import annotations

import asyncio

import pytest

from rn_domain.chunking import FROZEN_CHUNKING_V1
from rn_providers.fakes import FakeEmbeddingProvider
from tests.d8_bakeoff.candidates import (
    CANDIDATES,
    PRICE_VERIFIED_ON,
    Candidate,
    CandidateKind,
    estimate_cost,
    offline_candidates,
    paid_candidates,
    project_to_target,
    token_band,
    total_paid_cost,
)
from tests.d8_bakeoff.dataset import (
    Dataset,
    MaterialTier,
    Passage,
    Query,
    Script,
    Subset,
    ValidationStatus,
    load_dataset,
)
from tests.d8_bakeoff.harness import (
    CandidateResult,
    _non_decision_reasons,
    build_index,
    lexical_similarity,
    pooled_answerability,
    run_candidate,
    worst_subset_answerability,
)
from tests.d8_bakeoff.report import (
    GATE_K,
    build_report,
    evaluate_gates,
    render_markdown,
    write_report,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset()


def _fake_factory(candidate: Candidate) -> FakeEmbeddingProvider:
    width = candidate.effective_dimensions or 128
    return FakeEmbeddingProvider(dimensions=width, model_id=f"fake-{candidate.key}")


def _candidate(key: str) -> Candidate:
    return next(item for item in CANDIDATES if item.key == key)


@pytest.fixture(scope="module")
def lexical_result(dataset: Dataset) -> CandidateResult:
    """One lexical-baseline run, shared by every test that only needs *a* result.

    Module-scoped because a full run indexes 143 passages and scores 804 queries — about
    seven seconds — and half a dozen tests below want the same run to inspect a different
    property of it. Re-running per test cost a minute of suite time for identical output.

    Deliberately not shared with `test_ranking_is_reproducible`, which needs two genuinely
    independent runs: reusing one would make it assert that a value equals itself.

    `asyncio.run` rather than an async fixture because the project pins
    `asyncio_default_fixture_loop_scope = "function"`, so a module-scoped async fixture would
    outlive the loop it was created on.
    """
    return asyncio.run(run_candidate(candidate=_candidate("lexical-trigram"), dataset=dataset))


@pytest.fixture(scope="module")
def fake_result(dataset: Dataset) -> CandidateResult:
    """One deterministic-fake run, shared for the same reason as `lexical_result`."""
    return asyncio.run(
        run_candidate(
            candidate=_candidate("offline-fake-256"),
            dataset=dataset,
            provider_factory=_fake_factory,
        )
    )


# ---------------------------------------------------------------------------
# The candidate manifest
# ---------------------------------------------------------------------------


def test_every_paid_candidate_carries_a_verified_price() -> None:
    """A paid candidate without a verified price cannot appear in a cost estimate, and
    an estimate missing one candidate silently understates the total being approved."""
    for candidate in paid_candidates():
        assert candidate.usd_per_million_tokens is not None
        assert candidate.verified_on == PRICE_VERIFIED_ON


def test_the_verified_prices_are_the_ones_read_from_primary_docs() -> None:
    """Read on 2026-07-30 from the OpenAI pricing page. Pinned so a silent edit shows up
    as a test change rather than as a quietly different cost estimate."""
    by_model = {
        candidate.model_id: candidate.usd_per_million_tokens for candidate in paid_candidates()
    }
    assert by_model["text-embedding-3-small"] == 0.02
    assert by_model["text-embedding-3-large"] == 0.13


def test_no_candidate_requests_a_width_above_its_native_width() -> None:
    for candidate in CANDIDATES:
        if candidate.dimensions and candidate.native_dimensions:
            assert candidate.dimensions <= candidate.native_dimensions


def test_a_paid_candidate_without_a_price_is_refused() -> None:
    with pytest.raises(ValueError, match="verified price"):
        Candidate(
            key="bad",
            kind=CandidateKind.PAID_API,
            description="x",
            model_id="m",
        )


def test_a_reduced_width_on_a_non_reducible_model_is_refused() -> None:
    with pytest.raises(ValueError, match="not documented to support"):
        Candidate(
            key="bad",
            kind=CandidateKind.PAID_API,
            description="x",
            model_id="m",
            dimensions=512,
            supports_dimension_reduction=False,
            usd_per_million_tokens=0.02,
            verified_on="2026-07-30",
        )


def test_offline_candidates_are_never_decision_grade() -> None:
    """Structural, not conventional: a number produced without a real model cannot end
    up quoted as one."""
    for candidate in offline_candidates():
        assert not candidate.kind.is_decision_grade
        assert not candidate.requires_approval


def test_paid_and_local_candidates_require_approval() -> None:
    for candidate in CANDIDATES:
        if candidate.kind in {CandidateKind.PAID_API, CandidateKind.LOCAL_MODEL}:
            assert candidate.requires_approval


def test_local_model_candidates_record_their_infrastructure_cost() -> None:
    """DECISION 7: no heavyweight local ML infrastructure without showing the cost
    first. These entries exist to be shown, so an empty note defeats the purpose."""
    local = [item for item in CANDIDATES if item.kind is CandidateKind.LOCAL_MODEL]
    assert local
    for candidate in local:
        assert candidate.infrastructure_note.strip()


def test_ada_002_is_not_a_candidate() -> None:
    """It does not support `dimensions`, it is five times the price of 3-small, and the
    comparison usually cited to justify it is anti-fact 17."""
    assert all(candidate.model_id != "text-embedding-ada-002" for candidate in CANDIDATES)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def test_indic_scripts_are_estimated_at_more_tokens_per_character() -> None:
    """The counter-intuitive direction, and the reason a "~4 chars per token" rule of
    thumb would understate a Hindi corpus several-fold."""
    text = "x" * 100
    latin_low, latin_high = token_band(text, Script.LATIN)
    deva_low, deva_high = token_band(text, Script.DEVANAGARI)
    assert deva_low > latin_low
    assert deva_high > latin_high


def test_a_token_band_is_a_band() -> None:
    low, high = token_band("some text of moderate length", Script.LATIN)
    assert low < high


def test_free_candidates_estimate_to_zero(dataset: Dataset) -> None:
    for candidate in offline_candidates():
        estimate = estimate_cost(candidate, dataset)
        assert estimate.is_free
        assert estimate.usd_low == 0.0
        assert estimate.usd_high == 0.0


def test_paid_candidates_estimate_above_zero(dataset: Dataset) -> None:
    for candidate in paid_candidates():
        estimate = estimate_cost(candidate, dataset)
        assert not estimate.is_free
        assert 0.0 < estimate.usd_low <= estimate.usd_high


def test_the_larger_model_costs_more(dataset: Dataset) -> None:
    small = estimate_cost(next(c for c in CANDIDATES if c.key == "openai-3-small-1536"), dataset)
    large = estimate_cost(next(c for c in CANDIDATES if c.key == "openai-3-large-3072"), dataset)
    assert large.usd_high > small.usd_high


def test_projecting_to_target_scales_up(dataset: Dataset) -> None:
    """The seed dataset understates the real bill by more than an order of magnitude, so
    approving its cost would be approving the wrong number."""
    candidate = next(c for c in CANDIDATES if c.key == "openai-3-small-1536")
    estimate = estimate_cost(candidate, dataset)
    projected = project_to_target(estimate, dataset)
    assert projected.total_tokens_high > estimate.total_tokens_high
    assert projected.usd_high > estimate.usd_high


def test_projecting_a_free_candidate_stays_free(dataset: Dataset) -> None:
    estimate = estimate_cost(offline_candidates()[0], dataset)
    projected = project_to_target(estimate, dataset)
    assert projected.is_free
    assert projected.usd_high == 0.0


def test_total_paid_cost_sums_the_candidates(dataset: Dataset) -> None:
    low, high = total_paid_cost(dataset)
    assert 0.0 < low <= high


# ---------------------------------------------------------------------------
# Lexical baseline
# ---------------------------------------------------------------------------


def test_identical_text_scores_one() -> None:
    assert lexical_similarity("website development", "website development") == pytest.approx(1.0)


def test_overlapping_text_outscores_unrelated_text() -> None:
    query = "how long does a website take"
    near = "a website takes four to six weeks"
    far = "support is open monday to saturday"
    assert lexical_similarity(query, near) > lexical_similarity(query, far)


def test_the_baseline_cannot_match_across_scripts() -> None:
    """Its expected weakness, and the point of having it: a paid model that also scores
    near zero on cross-script has told us something we could not have read anywhere."""
    assert lexical_similarity("वेबसाइट", "website") == pytest.approx(0.0, abs=0.05)


def test_empty_text_scores_zero_rather_than_raising() -> None:
    assert lexical_similarity("", "anything") == 0.0


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


def test_the_index_is_built_with_the_frozen_policy(dataset: Dataset) -> None:
    units = build_index(dataset.passages)
    assert units
    assert len({unit.passage_id for unit in units}) == len(dataset.passages)


def test_the_lexical_baseline_runs_end_to_end(
    dataset: Dataset, lexical_result: CandidateResult
) -> None:
    result = lexical_result
    assert result.chunk_count > 0
    assert result.chunking_policy_version == FROZEN_CHUNKING_V1.version
    assert set(result.subset_scores) == {subset.value for subset in dataset.subsets}
    assert not result.is_decision_grade


def test_the_fake_embedding_candidate_runs_end_to_end(fake_result: CandidateResult) -> None:
    result = fake_result
    assert result.dimensions == 256
    assert result.model_id is not None
    assert result.subset_scores
    assert not result.is_decision_grade


async def test_an_embedding_candidate_with_no_provider_raises(dataset: Dataset) -> None:
    """Refusing rather than falling back to the lexical baseline, which would report
    trigram scores under a model's name."""
    with pytest.raises(ValueError, match="needs an embedding provider"):
        await run_candidate(
            candidate=next(c for c in CANDIDATES if c.key == "offline-fake-256"),
            dataset=dataset,
        )


async def test_a_local_model_candidate_raises(dataset: Dataset) -> None:
    """Declared for costing only. DECISION 7 requires showing the infrastructure
    commitment before any of it is installed."""
    with pytest.raises(ValueError, match="costing only"):
        await run_candidate(
            candidate=next(c for c in CANDIDATES if c.kind is CandidateKind.LOCAL_MODEL),
            dataset=dataset,
            provider_factory=_fake_factory,
        )


def test_non_decision_reasons_name_both_causes(fake_result: CandidateResult) -> None:
    """Two independent causes, and fixing one must not clear the other.

    Review completed on 2026-08-11, so the real corpus now supplies only the *candidate*
    reason — which is exactly the moment this test earns its keep. If the two causes were
    ever collapsed into one, an offline candidate would start reporting as decision-grade
    the instant the last subset was signed off, and the report would say so in bold.

    The review half is therefore still exercised, against a fixture, because the property
    "an unreviewed subset also disqualifies a result" has to survive the corpus no longer
    demonstrating it.
    """
    joined = " ".join(fake_result.non_decision_reasons)
    assert "not a production candidate" in joined
    assert "native-speaker review" not in joined, (
        "review is complete; a lingering review reason would mean readiness is not being read"
    )
    assert not fake_result.is_decision_grade

    unreviewed = Dataset(
        version=1,
        passages=(
            Passage(
                id="p1",
                text="We build websites.",
                language="en",
                script=Script.LATIN,
                validation=ValidationStatus.NATIVE_REVIEWED,
                tier=MaterialTier.SOURCE_GROUNDED,
                source_reference="test fixture",
                reviewed_by="A Reviewer",
            ),
        ),
        queries=(
            Query(
                id="q1",
                text="वेबसाइट बनाते हैं क्या",
                subset=Subset.HI_DEVANAGARI,
                script=Script.DEVANAGARI,
                validation=ValidationStatus.SYNTHETIC_UNREVIEWED,
                tier=MaterialTier.CONTROLLED_SYNTHETIC,
                relevant={"p1": 2},
            ),
        ),
    )
    paid = next(item for item in CANDIDATES if item.kind is CandidateKind.PAID_API)
    reasons = _non_decision_reasons(paid, unreviewed, unreviewed.subsets)
    assert reasons, "an unreviewed subset must disqualify even a real model"
    assert "native-speaker review" in " ".join(reasons)
    assert "not a production candidate" not in " ".join(reasons)


async def test_ranking_is_reproducible(dataset: Dataset) -> None:
    """Two runs must agree, or two candidates could appear to differ because of a
    non-deterministic tie-break rather than because of their vectors."""
    candidate = next(c for c in CANDIDATES if c.key == "lexical-trigram")
    first = await run_candidate(candidate=candidate, dataset=dataset)
    second = await run_candidate(candidate=candidate, dataset=dataset)
    for subset in first.subset_scores:
        assert (
            first.subset_scores[subset].answerability == second.subset_scores[subset].answerability
        )


def test_the_worst_subset_is_reported(lexical_result: CandidateResult) -> None:
    """The number that should be looked at first — a candidate can pool well while
    failing an entire language."""
    result = lexical_result
    worst = worst_subset_answerability(result, GATE_K)
    assert worst is not None
    name, value = worst
    assert name in result.subset_scores
    assert value == min(score.answerability_at(GATE_K) for score in result.subset_scores.values())


def test_pooled_answerability_is_between_the_extremes(lexical_result: CandidateResult) -> None:
    result = lexical_result
    values = [score.answerability_at(GATE_K) for score in result.subset_scores.values()]
    pooled = pooled_answerability(result, GATE_K)
    assert min(values) <= pooled <= max(values)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_gates_report_unevaluated_ones_as_unevaluated(lexical_result: CandidateResult) -> None:
    """A report showing one green gate and omitting six would read as a full pass."""
    result = lexical_result
    outcomes = evaluate_gates(result)
    names = [outcome.gate for outcome in outcomes]
    assert any("G1-quality" in name for name in names)
    for gate in ("G0-servability", "G2-latency", "G3-halfvec-recall-delta", "G5-partitioning"):
        outcome = next(item for item in outcomes if item.gate == gate)
        assert not outcome.passed
        assert "NOT EVALUATED" in outcome.detail


def test_a_report_from_offline_candidates_is_not_decision_grade(
    dataset: Dataset, lexical_result: CandidateResult
) -> None:
    """A complete review does not promote an offline result, and that is the point.

    Until 2026-08-11 this report was disqualified twice over — offline candidate *and*
    unreviewed subsets — so it could not distinguish the two. Review is now complete and
    `readiness_notes` is legitimately empty, which leaves the candidate kind carrying the
    disqualification alone. If that ever stopped being sufficient, a lexical trigram
    baseline could be quoted in ADR-011 as a model choice.
    """
    result = lexical_result
    report = build_report(
        dataset=dataset,
        results=[result],
        cost_estimates=[estimate_cost(offline_candidates()[1], dataset)],
        chunking_policy_version=FROZEN_CHUNKING_V1.version,
    )
    assert not report.is_decision_grade
    assert report.decision_grade_results == ()
    assert report.dataset_review_complete
    assert report.readiness_notes == ()


def test_the_markdown_leads_with_the_decision_grade_warning(
    dataset: Dataset, lexical_result: CandidateResult
) -> None:
    result = lexical_result
    report = build_report(
        dataset=dataset,
        results=[result],
        cost_estimates=[],
        chunking_policy_version=FROZEN_CHUNKING_V1.version,
    )
    rendered = render_markdown(report)
    assert "decision-grade: NO" in rendered
    assert "cannot be used to choose the production embedding model" in rendered
    # The review section is rendered only when something is outstanding. Review closed on
    # 2026-08-11, so its absence is now the correct output — and asserting the absence is
    # what stops the section quietly reappearing on a corpus nobody re-reviewed.
    assert "Awaiting native-speaker review" not in rendered


def test_a_written_report_is_named_by_its_grade(
    dataset: Dataset, tmp_path: object, lexical_result: CandidateResult
) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    result = lexical_result
    report = build_report(
        dataset=dataset,
        results=[result],
        cost_estimates=[],
        chunking_policy_version=FROZEN_CHUNKING_V1.version,
    )
    path = write_report(report, directory=tmp_path)
    assert path.is_file()
    assert "not-decision-grade" in path.name
    import json

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["report_is_decision_grade"] is False
    assert document["chunking_policy_version"] == FROZEN_CHUNKING_V1.version
