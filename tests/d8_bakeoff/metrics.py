"""Retrieval metrics for the D-8 bake-off. Pure functions, unit-tested.

**The primary metric is `answerability@k`**, not recall and not nDCG: the fraction of
queries for which at least one gold passage appears in the top `k`. It is primary
because it is the only one that maps onto the product. A `search_knowledge` call
returns `k` chunks and the agent answers from them, so what matters is "could the
agent have answered", and one good chunk is enough for that. Recall@8 of 0.5 sounds
mediocre and is perfectly adequate when the missing half was redundant.

Recall, nDCG and MRR are reported alongside because they answer different questions —
how much of the gold set was found, how well it was ordered, and how far down the
first hit was — and because a candidate that wins on answerability while losing badly
on MRR is putting the answer at position 8, which costs real prompt budget.

**Every metric is reported per subset.** The pooled average is computed too, and it
is the number least worth looking at: a candidate can pool to 0.92 while scoring 0.55
on Telugu, and shipping that is how an India-first product gets a language wrong.

`k` values are fixed at 4, 8 and 16: 4 is the retrieval default, 8 is the likely
production `k`, and 16 is the ceiling `RETRIEVAL_MAX_K` allows.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "REPORTED_K",
    "QueryScore",
    "SubsetScore",
    "answerability_at_k",
    "dcg",
    "mean_reciprocal_rank_at_k",
    "ndcg_at_k",
    "recall_at_k",
    "score_queries",
]

#: The cut-offs every report carries.
REPORTED_K: Final[tuple[int, ...]] = (4, 8, 16)


def answerability_at_k(ranked: Sequence[str], gold: frozenset[str], k: int) -> float:
    """1.0 if any gold passage is in the top `k`, else 0.0.

    Binary per query on purpose: averaged over a subset it becomes "the fraction of
    questions this candidate could have answered", which is a sentence a
    non-engineer can act on.
    """
    if k <= 0:
        return 0.0
    return 1.0 if any(identifier in gold for identifier in ranked[:k]) else 0.0


def recall_at_k(ranked: Sequence[str], gold: frozenset[str], k: int) -> float:
    """Fraction of the gold set retrieved in the top `k`."""
    if not gold or k <= 0:
        return 0.0
    hits = sum(1 for identifier in ranked[:k] if identifier in gold)
    return hits / len(gold)


def mean_reciprocal_rank_at_k(ranked: Sequence[str], gold: frozenset[str], k: int) -> float:
    """Reciprocal of the rank of the first gold hit, or 0.0 if none in the top `k`.

    Named "mean" for consistency with how it is reported, though this computes the
    per-query value; the mean is taken by `score_queries`.
    """
    for position, identifier in enumerate(ranked[:k], start=1):
        if identifier in gold:
            return 1.0 / position
    return 0.0


def dcg(grades: Sequence[int]) -> float:
    """Discounted cumulative gain with exponential gain, `(2**g - 1) / log2(i + 2)`.

    Exponential rather than linear gain because the grades are ordinal and a
    fully-answering passage is worth materially more than a partially-answering one;
    linear gain would let two grade-1 passages outrank one grade-2, which is not what
    the judgements mean.
    """
    return math.fsum(
        (2.0**grade - 1.0) / math.log2(position + 2.0) for position, grade in enumerate(grades)
    )


def ndcg_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """Normalised DCG at `k` over graded judgements."""
    if not relevant or k <= 0:
        return 0.0
    achieved = dcg([relevant.get(identifier, 0) for identifier in ranked[:k]])
    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    if ideal == 0.0:
        return 0.0
    return achieved / ideal


@dataclass(frozen=True, slots=True)
class QueryScore:
    """One query's scores, retained so a failure can be inspected individually.

    Kept per query rather than only aggregated because the useful question after a
    bake-off is "which queries did the winner miss", and an average cannot answer it.
    """

    query_id: str
    answerability: Mapping[int, float]
    recall: Mapping[int, float]
    ndcg: Mapping[int, float]
    reciprocal_rank: Mapping[int, float]
    #: The top-ranked ids, truncated to the largest reported `k`, for eyeballing.
    top_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubsetScore:
    """Aggregated scores for one subset."""

    subset: str
    query_count: int
    answerability: Mapping[int, float]
    recall: Mapping[int, float]
    ndcg: Mapping[int, float]
    mrr: Mapping[int, float]
    per_query: tuple[QueryScore, ...] = ()

    def answerability_at(self, k: int) -> float:
        return self.answerability.get(k, 0.0)


def score_queries(
    *,
    subset: str,
    ranked_by_query: Mapping[str, Sequence[str]],
    relevance_by_query: Mapping[str, Mapping[str, int]],
    ks: Sequence[int] = REPORTED_K,
) -> SubsetScore:
    """Aggregate per-query rankings into one subset score.

    Raises `ValueError` when a query has a ranking but no judgements, or vice versa.
    A silently-skipped query would change the denominator without changing the
    reported query count, which is the sort of arithmetic error that makes two
    candidates incomparable without anyone noticing.
    """
    ranked_ids = set(ranked_by_query)
    judged_ids = set(relevance_by_query)
    if ranked_ids != judged_ids:
        raise ValueError(
            "Ranked queries and judged queries differ: "
            f"ranked-only={sorted(ranked_ids - judged_ids)}, "
            f"judged-only={sorted(judged_ids - ranked_ids)}"
        )

    largest = max(ks) if ks else 0
    scores: list[QueryScore] = []
    for query_id in sorted(ranked_ids):
        ranked = list(ranked_by_query[query_id])
        relevant = relevance_by_query[query_id]
        gold = frozenset(relevant)
        scores.append(
            QueryScore(
                query_id=query_id,
                answerability={k: answerability_at_k(ranked, gold, k) for k in ks},
                recall={k: recall_at_k(ranked, gold, k) for k in ks},
                ndcg={k: ndcg_at_k(ranked, relevant, k) for k in ks},
                reciprocal_rank={k: mean_reciprocal_rank_at_k(ranked, gold, k) for k in ks},
                top_ids=tuple(ranked[:largest]),
            )
        )

    return SubsetScore(
        subset=subset,
        query_count=len(scores),
        answerability={k: _mean(score.answerability[k] for score in scores) for k in ks},
        recall={k: _mean(score.recall[k] for score in scores) for k in ks},
        ndcg={k: _mean(score.ndcg[k] for score in scores) for k in ks},
        mrr={k: _mean(score.reciprocal_rank[k] for score in scores) for k in ks},
        per_query=tuple(scores),
    )


def _mean(values: Iterable[float]) -> float:
    """Mean of an iterable, 0.0 when empty.

    `statistics.fmean` raises on an empty sequence; a subset with no queries is a
    legitimate state while a dataset is being built, and it should report zero rather
    than abort the whole run.
    """
    materialised = list(values)
    if not materialised:
        return 0.0
    return statistics.fmean(materialised)
