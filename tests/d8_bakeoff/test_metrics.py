"""The retrieval metrics.

These get their own tests because a scoring bug that flatters one candidate is the one
failure a bake-off cannot absorb: it would not look like a bug, it would look like a
result, and it would end up in an ADR. Every metric is checked against a
hand-computed value rather than against another implementation.
"""

from __future__ import annotations

import math

import pytest

from tests.d8_bakeoff.metrics import (
    REPORTED_K,
    answerability_at_k,
    dcg,
    mean_reciprocal_rank_at_k,
    ndcg_at_k,
    recall_at_k,
    score_queries,
)

pytestmark = [pytest.mark.unit]

GOLD = frozenset({"a", "b"})
RANKED = ["x", "a", "y", "b", "z"]


# ---------------------------------------------------------------------------
# answerability@k — the primary metric
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "expected"),
    [(1, 0.0), (2, 1.0), (3, 1.0), (5, 1.0)],
)
def test_answerability_is_binary_per_query(k: int, expected: float) -> None:
    """One gold hit in the top k is enough — the agent only needs one good chunk to
    answer, which is why this and not recall is the primary metric."""
    assert answerability_at_k(RANKED, GOLD, k) == expected


def test_answerability_with_no_hits_is_zero() -> None:
    assert answerability_at_k(["x", "y"], GOLD, 2) == 0.0


@pytest.mark.parametrize("k", [0, -1])
def test_a_non_positive_k_scores_zero_everywhere(k: int) -> None:
    assert answerability_at_k(RANKED, GOLD, k) == 0.0
    assert recall_at_k(RANKED, GOLD, k) == 0.0
    assert ndcg_at_k(RANKED, {"a": 2}, k) == 0.0


# ---------------------------------------------------------------------------
# recall@k
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "expected"),
    [(1, 0.0), (2, 0.5), (4, 1.0), (5, 1.0)],
)
def test_recall_counts_the_fraction_of_gold_found(k: int, expected: float) -> None:
    assert recall_at_k(RANKED, GOLD, k) == pytest.approx(expected)


def test_recall_against_an_empty_gold_set_is_zero_not_a_division_error() -> None:
    assert recall_at_k(RANKED, frozenset(), 4) == 0.0


# ---------------------------------------------------------------------------
# MRR
# ---------------------------------------------------------------------------


def test_reciprocal_rank_uses_the_first_hit() -> None:
    assert mean_reciprocal_rank_at_k(RANKED, GOLD, 5) == pytest.approx(0.5)
    assert mean_reciprocal_rank_at_k(["a", "b"], GOLD, 5) == pytest.approx(1.0)


def test_reciprocal_rank_is_zero_when_the_hit_is_below_k() -> None:
    """The distinction that makes MRR worth reporting alongside answerability: an answer
    at position 8 costs real prompt budget even though it is technically retrieved."""
    assert mean_reciprocal_rank_at_k(RANKED, GOLD, 1) == 0.0


# ---------------------------------------------------------------------------
# nDCG
# ---------------------------------------------------------------------------


def test_dcg_matches_the_hand_computed_value() -> None:
    # grades [2, 0, 1] -> (2^2-1)/log2(2) + 0 + (2^1-1)/log2(4)
    expected = 3.0 / math.log2(2.0) + 0.0 + 1.0 / math.log2(4.0)
    assert dcg([2, 0, 1]) == pytest.approx(expected)


def test_perfect_ordering_scores_one() -> None:
    relevant = {"a": 2, "b": 1}
    assert ndcg_at_k(["a", "b", "z"], relevant, 3) == pytest.approx(1.0)


def test_inverted_ordering_scores_below_one() -> None:
    """Exponential gain means a grade-2 passage genuinely outranks a grade-1 one; with
    linear gain two grade-1s could outscore one grade-2, which is not what the
    judgements mean."""
    relevant = {"a": 2, "b": 1}
    assert ndcg_at_k(["b", "a"], relevant, 2) < 1.0


def test_ndcg_with_no_hits_is_zero() -> None:
    assert ndcg_at_k(["x", "y"], {"a": 2}, 2) == 0.0


def test_two_partial_hits_do_not_beat_one_full_hit() -> None:
    """The concrete consequence of exponential gain, stated as a comparison."""
    relevant = {"full": 2, "part1": 1, "part2": 1}
    full_first = ndcg_at_k(["full"], relevant, 1)
    parts_first = ndcg_at_k(["part1", "part2"], relevant, 2)
    assert full_first > parts_first


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_score_queries_averages_per_subset() -> None:
    score = score_queries(
        subset="en",
        ranked_by_query={"q1": ["a"], "q2": ["z"]},
        relevance_by_query={"q1": {"a": 2}, "q2": {"b": 2}},
        ks=(1,),
    )
    assert score.query_count == 2
    # One query answerable, one not.
    assert score.answerability[1] == pytest.approx(0.5)
    assert score.subset == "en"


def test_score_queries_retains_per_query_detail() -> None:
    """The useful question after a bake-off is "which queries did the winner miss", and
    an average cannot answer it."""
    score = score_queries(
        subset="en",
        ranked_by_query={"q1": ["a"], "q2": ["z"]},
        relevance_by_query={"q1": {"a": 2}, "q2": {"b": 2}},
        ks=(1,),
    )
    assert {item.query_id for item in score.per_query} == {"q1", "q2"}
    missed = next(item for item in score.per_query if item.query_id == "q2")
    assert missed.answerability[1] == 0.0


def test_a_query_ranked_but_not_judged_is_refused() -> None:
    """A silently-skipped query changes the denominator without changing the reported
    query count, which makes two candidates incomparable without anyone noticing."""
    with pytest.raises(ValueError, match="ranked-only"):
        score_queries(
            subset="en",
            ranked_by_query={"q1": ["a"], "q2": ["b"]},
            relevance_by_query={"q1": {"a": 2}},
        )


def test_a_query_judged_but_not_ranked_is_refused() -> None:
    with pytest.raises(ValueError, match="judged-only"):
        score_queries(
            subset="en",
            ranked_by_query={"q1": ["a"]},
            relevance_by_query={"q1": {"a": 2}, "q2": {"b": 2}},
        )


def test_an_empty_subset_scores_zero_rather_than_raising() -> None:
    """A subset with no queries is a legitimate state while a dataset is being built."""
    score = score_queries(subset="en", ranked_by_query={}, relevance_by_query={})
    assert score.query_count == 0
    assert score.answerability_at(8) == 0.0


def test_the_reported_cutoffs_are_the_documented_ones() -> None:
    """4 is the retrieval default, 8 the likely production k, 16 the configured max."""
    assert REPORTED_K == (4, 8, 16)
