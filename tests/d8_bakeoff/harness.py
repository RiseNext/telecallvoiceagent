"""The bake-off harness: chunk, embed, rank, score.

The pipeline deliberately mirrors production rather than shortcutting it:

    passage -> normalise -> chunk (FROZEN policy) -> embed as DOCUMENT
    query   -> normalise             -> embed as QUERY
    rank chunks by cosine -> collapse to passages -> score against graded gold

**Chunks are ranked, then collapsed to passages.** Judgements are per passage, but
production retrieves *chunks*, so ranking passages directly would measure something
we do not ship. Collapsing keeps each passage's best-scoring chunk and drops the
rest, which is what a caller would experience once duplicate sources are merged.

**The harness constructs no provider and reads no credential.** It takes a factory.
That is what makes "importing this package cannot spend money" true rather than
merely intended, and it is why the offline tests can drive the whole pipeline.

**Decision-grade is computed, not asserted.** A result is decision-grade only when the
candidate is a real model *and* every scored subset is native-speaker review-complete.
Both halves are structural: `CandidateKind.is_decision_grade` excludes the offline
kinds, and `Dataset.readiness` excludes unreviewed Indic text. Nothing here can be
persuaded otherwise by a flag.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from rn_domain.chunking import FROZEN_CHUNKING_V1, ChunkingPolicy, chunk_document
from rn_domain.text import normalise_text
from rn_providers.embeddings import EmbeddingProvider, EmbeddingVector
from tests.d8_bakeoff.candidates import Candidate, CandidateKind
from tests.d8_bakeoff.dataset import Dataset, Passage, Subset
from tests.d8_bakeoff.metrics import REPORTED_K, SubsetScore, score_queries

__all__ = [
    "CandidateResult",
    "ProviderFactory",
    "RankedUnit",
    "as_mapping",
    "build_index",
    "lexical_similarity",
    "pooled_answerability",
    "run_candidate",
    "worst_subset_answerability",
]

#: Builds the provider for a candidate. Injected so the harness never holds a key.
type ProviderFactory = Callable[[Candidate], EmbeddingProvider]

#: How many ranked passages are retained per query. Must be at least the largest
#: reported `k`, plus headroom so collapsing chunks cannot starve the tail.
_RANK_DEPTH = max(REPORTED_K) * 4


@dataclass(frozen=True, slots=True)
class RankedUnit:
    """One chunk in the index, with the passage it came from."""

    passage_id: str
    chunk_index: int
    text: str


@dataclass(slots=True)
class CandidateResult:
    """Everything one candidate produced over one dataset."""

    candidate_key: str
    candidate_kind: CandidateKind
    model_id: str | None
    dimensions: int | None
    chunking_policy_version: str
    chunk_count: int
    #: Reported per subset. The pooled figure is derivable and deliberately secondary.
    subset_scores: dict[str, SubsetScore] = field(default_factory=dict)
    #: Wall-clock seconds spent embedding. A measurement of this run on this machine,
    #: not a latency claim about production.
    corpus_embed_seconds: float = 0.0
    query_embed_seconds: float = 0.0
    #: Tokens the provider actually reported, when it reports any. This is what
    #: replaces the estimated band in `candidates.py` with a measurement.
    reported_prompt_tokens: int | None = None
    #: Populated when the result may not inform ADR-011, with the reasons.
    non_decision_reasons: tuple[str, ...] = ()

    @property
    def is_decision_grade(self) -> bool:
        return not self.non_decision_reasons


def lexical_similarity(left: str, right: str) -> float:
    """Character-trigram Jaccard overlap. The baseline, computed locally.

    Approximates what a `pg_trgm` similarity query would do — and `pg_trgm` is already
    installed in every environment, which is exactly why the baseline matters: if a
    paid model cannot beat this on our corpus, the cheapest correct answer to D-8 might
    not involve embeddings at all.

    Note the expected weakness, which is the point of having it: trigram overlap cannot
    match across scripts, so it should score near zero on the cross-script subsets. A
    paid model that also scores near zero there has told us something important.
    """
    left_grams = _trigrams(normalise_text(left).casefold())
    right_grams = _trigrams(normalise_text(right).casefold())
    if not left_grams or not right_grams:
        return 0.0
    intersection = len(left_grams & right_grams)
    if not intersection:
        return 0.0
    return intersection / len(left_grams | right_grams)


def _trigrams(text: str) -> frozenset[str]:
    padded = f"  {text}  "
    if len(padded) < 3:
        return frozenset({padded})
    return frozenset(padded[index : index + 3] for index in range(len(padded) - 2))


def _cosine(left: EmbeddingVector, right: EmbeddingVector) -> float:
    """Cosine similarity.

    Computed in full rather than assuming unit vectors: the seam does not promise
    normalisation, providers differ, and a reduced-width embedding needs
    re-normalising after truncation. Assuming unit norm would silently mis-rank
    exactly the reduced-width candidates the bake-off exists to compare.
    """
    if len(left) != len(right):
        raise ValueError(f"Cannot compare vectors of width {len(left)} and {len(right)}.")
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def build_index(
    passages: Sequence[Passage], *, policy: ChunkingPolicy = FROZEN_CHUNKING_V1
) -> tuple[RankedUnit, ...]:
    """Chunk every passage with the frozen policy, in a deterministic order.

    A passage that chunks to nothing is skipped rather than represented by an empty
    unit — an empty string embeds to a meaningless vector that would then rank
    somewhere arbitrary.
    """
    units: list[RankedUnit] = []
    for passage in passages:
        for chunk in chunk_document(passage.text, policy=policy):
            units.append(
                RankedUnit(passage_id=passage.id, chunk_index=chunk.index, text=chunk.text)
            )
    return tuple(units)


def _collapse_to_passages(scored: Sequence[tuple[float, RankedUnit]]) -> list[str]:
    """Highest-scoring chunk per passage, ordered by score descending.

    Ties break on `(passage_id, chunk_index)` so a run is reproducible: without a
    deterministic tie-break, two candidates with identical scores could report
    different orderings and appear to differ.
    """
    ordered = sorted(scored, key=lambda item: (-item[0], item[1].passage_id, item[1].chunk_index))
    seen: set[str] = set()
    collapsed: list[str] = []
    for _, unit in ordered:
        if unit.passage_id in seen:
            continue
        seen.add(unit.passage_id)
        collapsed.append(unit.passage_id)
        if len(collapsed) >= _RANK_DEPTH:
            break
    return collapsed


async def run_candidate(
    *,
    candidate: Candidate,
    dataset: Dataset,
    provider_factory: ProviderFactory | None = None,
    policy: ChunkingPolicy = FROZEN_CHUNKING_V1,
) -> CandidateResult:
    """Run one candidate end to end and score it per subset.

    Args:
        provider_factory: Required for embedding candidates, ignored for the lexical
            baseline. Absent for an embedding candidate is a `ValueError` rather than a
            silent fallback to the baseline, which would produce a plausible-looking
            report attributing trigram scores to a paid model.

    Raises:
        ValueError: for a `LOCAL_MODEL` candidate. Those are declared for costing and
            are not implemented; DECISION 7 requires showing the infrastructure
            commitment before any of it is installed.
    """
    if candidate.kind is CandidateKind.LOCAL_MODEL:
        raise ValueError(
            f"Candidate {candidate.key} is declared for costing only. Running it needs "
            "infrastructure that has not been approved — see its `infrastructure_note`."
        )

    units = build_index(dataset.passages, policy=policy)
    result = CandidateResult(
        candidate_key=candidate.key,
        candidate_kind=candidate.kind,
        model_id=candidate.model_id,
        dimensions=candidate.effective_dimensions,
        chunking_policy_version=policy.version,
        chunk_count=len(units),
    )

    if candidate.kind is CandidateKind.LEXICAL_BASELINE:
        ranked_by_query = {
            query.id: _collapse_to_passages(
                [(lexical_similarity(query.text, unit.text), unit) for unit in units]
            )
            for query in dataset.queries
        }
    else:
        if provider_factory is None:
            raise ValueError(
                f"Candidate {candidate.key} needs an embedding provider and none was "
                "supplied. Refusing rather than falling back to the lexical baseline, "
                "which would report trigram scores under a model's name."
            )
        provider = provider_factory(candidate)
        ranked_by_query, result = await _rank_with_embeddings(
            provider=provider, units=units, dataset=dataset, result=result
        )

    for subset in dataset.subsets:
        queries = dataset.queries_in(subset)
        if not queries:
            continue
        result.subset_scores[subset.value] = score_queries(
            subset=subset.value,
            ranked_by_query={query.id: ranked_by_query[query.id] for query in queries},
            relevance_by_query={query.id: dict(query.relevant) for query in queries},
        )

    result.non_decision_reasons = _non_decision_reasons(candidate, dataset, dataset.subsets)
    return result


async def _rank_with_embeddings(
    *,
    provider: EmbeddingProvider,
    units: Sequence[RankedUnit],
    dataset: Dataset,
    result: CandidateResult,
) -> tuple[dict[str, list[str]], CandidateResult]:
    started = time.perf_counter()
    corpus = await provider.embed_documents([unit.text for unit in units])
    result.corpus_embed_seconds = time.perf_counter() - started

    if len(corpus) != len(units):
        raise ValueError(
            f"Embedded {len(corpus)} vectors for {len(units)} chunks — a batching bug "
            "would attach vectors to the wrong chunks and every score below would be "
            "meaningless."
        )

    tokens = corpus.usage.prompt_tokens or 0
    query_started = time.perf_counter()
    ranked_by_query: dict[str, list[str]] = {}
    for query in dataset.queries:
        batch = await provider.embed_query(query.text)
        tokens += batch.usage.prompt_tokens or 0
        vector = batch.only
        ranked_by_query[query.id] = _collapse_to_passages(
            [(_cosine(vector, corpus.vectors[index]), unit) for index, unit in enumerate(units)]
        )
    result.query_embed_seconds = time.perf_counter() - query_started
    result.reported_prompt_tokens = tokens or None
    result.dimensions = provider.dimensions
    result.model_id = provider.model_id
    return ranked_by_query, result


def _non_decision_reasons(
    candidate: Candidate, dataset: Dataset, subsets: Sequence[Subset]
) -> tuple[str, ...]:
    """Why this result may not inform ADR-011. Empty means it may."""
    reasons: list[str] = []
    if not candidate.kind.is_decision_grade:
        reasons.append(
            f"candidate kind '{candidate.kind.value}' is not a production candidate: "
            "it measures the harness, not a model"
        )
    for subset in subsets:
        readiness = dataset.readiness(subset)
        if readiness.blocking_reason:
            reasons.append(readiness.blocking_reason)
    return tuple(reasons)


def pooled_answerability(result: CandidateResult, k: int) -> float:
    """Query-count-weighted answerability across subsets.

    Weighted by query count so a 4-query subset cannot swing the pooled figure as much
    as a 40-query one. Provided for completeness and reported last: the per-subset
    numbers are the ones that decide anything, because a candidate can pool well while
    failing an entire language.
    """
    scores = result.subset_scores.values()
    total = sum(score.query_count for score in scores)
    if not total:
        return 0.0
    return math.fsum(score.answerability_at(k) * score.query_count for score in scores) / total


def worst_subset_answerability(result: CandidateResult, k: int) -> tuple[str, float] | None:
    """The subset this candidate is worst at, and its score.

    The number that should be looked at first. Gate G1 is expressed per subset
    precisely so that this, and not the pooled average, is what a candidate has to
    clear.
    """
    if not result.subset_scores:
        return None
    worst = min(result.subset_scores.values(), key=lambda score: score.answerability_at(k))
    return worst.subset, worst.answerability_at(k)


def as_mapping(result: CandidateResult) -> Mapping[str, object]:
    """A JSON-ready summary. The full per-query detail lives in the report artifact."""
    return {
        "candidate": result.candidate_key,
        "kind": result.candidate_kind.value,
        "model_id": result.model_id,
        "dimensions": result.dimensions,
        "chunking_policy": result.chunking_policy_version,
        "chunks": result.chunk_count,
        "decision_grade": result.is_decision_grade,
        "non_decision_reasons": list(result.non_decision_reasons),
        "reported_prompt_tokens": result.reported_prompt_tokens,
    }
