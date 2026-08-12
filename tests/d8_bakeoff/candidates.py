"""The D-8 candidate manifest, and the cost estimate that gates running it.

Every provider fact in this file was **verified against primary documentation on
2026-07-30** and carries the date it was verified. CLAUDE.md rule 6 exists because
model memory of provider APIs is stale, and a candidate manifest built from memory
would produce a cost estimate that is wrong in an unknown direction.

Verified, `https://developers.openai.com/api/docs/guides/embeddings` and
`.../api/docs/pricing`:

| model | native width | max input tokens | `dimensions` | USD / 1M tokens |
|---|---|---|---|---|
| `text-embedding-3-small` | 1536 | 8192 | yes | 0.02 |
| `text-embedding-3-large` | 3072 | 8192 | yes | 0.13 |
| `text-embedding-ada-002` | (not stated) | 8192 | not documented | 0.10 |

`ada-002` is **not a candidate**: it does not support `dimensions`, it is five times
the price of `3-small`, and PROVIDER_CONSTRAINTS anti-fact 17 records that the
comparison usually cited to justify it could not be verified. Including it would cost
money to confirm something nobody is proposing.

**Local and open models are declared but not implemented.** Every candidate here that
is not offline-free is marked `requires_approval`, and the local-model entries
additionally record what infrastructure they would need. DECISION 7 forbids
installing heavyweight local ML infrastructure for the benchmark without showing the
cost first, so these entries exist to *be shown*, not to run. `torch` and
`sentence-transformers` are not dependencies of this repository and this package does
not import them.

**Token counts are estimates with a stated band, not measurements.** There is no
tokeniser in this repository — `tiktoken` would be a dependency added to produce a
number the API returns for free on the first real call. So the estimate is a band
derived from per-script characters-per-token assumptions that are explicitly labelled
as assumptions, and the first paid run replaces the band with the `usage` the provider
reports. The band exists to answer "is this five dollars or five hundred", which is
the only question it needs to answer before approval.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tests.d8_bakeoff.dataset import Dataset, Script

__all__ = [
    "CANDIDATES",
    "CHARS_PER_TOKEN_HIGH",
    "CHARS_PER_TOKEN_LOW",
    "PRICE_VERIFIED_ON",
    "TARGET_PASSAGES",
    "TARGET_QUERIES",
    "Candidate",
    "CandidateKind",
    "CostEstimate",
    "estimate_cost",
    "offline_candidates",
    "paid_candidates",
    "project_to_target",
    "token_band",
    "total_paid_cost",
]

#: The date every price and capability in this module was read from primary docs.
PRICE_VERIFIED_ON: Final[str] = "2026-07-30"


class CandidateKind(StrEnum):
    """What running a candidate costs, and whether it can support a decision."""

    OFFLINE_DETERMINISTIC = "offline_deterministic"
    """The lexical hashing fake. Free, offline, exercises the harness.
    **Never decision-grade** — it has no semantic or cross-lingual capability."""

    LEXICAL_BASELINE = "lexical_baseline"
    """Character-trigram overlap, computed locally. Free and offline. Reported as a
    **baseline**: if a paid model cannot beat trigram overlap on this corpus, that is
    the most important finding the bake-off can produce. Not itself a candidate for
    the production vector column, so not decision-grade."""

    PAID_API = "paid_api"
    """A hosted model. Costs money, needs a key, needs explicit approval."""

    LOCAL_MODEL = "local_model"
    """A self-hosted model. Free per call, but needs infrastructure this repository
    does not have and has not been approved to acquire."""

    @property
    def is_decision_grade(self) -> bool:
        """Whether a score from this kind may inform ADR-011.

        The two offline kinds are excluded structurally rather than by convention, so
        a number produced without a real model cannot end up quoted as one.
        """
        return self in {CandidateKind.PAID_API, CandidateKind.LOCAL_MODEL}

    @property
    def is_runnable_offline(self) -> bool:
        return self in {CandidateKind.OFFLINE_DETERMINISTIC, CandidateKind.LEXICAL_BASELINE}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One model at one width — the unit the bake-off compares."""

    key: str
    kind: CandidateKind
    description: str
    #: `None` for the offline kinds, which have no model id worth recording.
    model_id: str | None = None
    #: The width to request. `None` means the model's native width.
    dimensions: int | None = None
    native_dimensions: int | None = None
    supports_dimension_reduction: bool = False
    usd_per_million_tokens: float | None = None
    max_input_tokens: int | None = None
    verified_on: str | None = None
    #: What acquiring the ability to run this would commit us to. Empty for hosted and
    #: offline candidates.
    infrastructure_note: str = ""

    def __post_init__(self) -> None:
        if self.kind is CandidateKind.PAID_API:
            if self.usd_per_million_tokens is None or self.verified_on is None:
                # A paid candidate without a verified price cannot appear in a cost
                # estimate, and an estimate missing one candidate silently understates
                # the total that gets approved.
                raise ValueError(f"Paid candidate {self.key} needs a verified price.")
            if self.dimensions is not None and not self.supports_dimension_reduction:
                raise ValueError(
                    f"Candidate {self.key} requests a width but the model is not "
                    "documented to support the `dimensions` parameter."
                )
            if (
                self.dimensions is not None
                and self.native_dimensions is not None
                and self.dimensions > self.native_dimensions
            ):
                raise ValueError(
                    f"Candidate {self.key} requests {self.dimensions} dims above the "
                    f"model's native {self.native_dimensions}."
                )

    @property
    def requires_approval(self) -> bool:
        """Whether running this needs a human to say yes first."""
        return not self.kind.is_runnable_offline

    @property
    def effective_dimensions(self) -> int | None:
        return self.dimensions if self.dimensions is not None else self.native_dimensions


# ---------------------------------------------------------------------------
# Characters-per-token assumptions.
#
# **These are assumptions with a band, not measurements**, and the direction that
# matters is counter-intuitive: Devanagari and Telugu cost *more* tokens per character
# than English under a byte-pair encoding whose vocabulary is dominated by Latin
# script, because Indic codepoints fall back to multi-byte pieces. The commonly
# repeated "~4 characters per token" is an English figure and using it for Hindi would
# understate the token count several-fold.
#
# The low value produces the low token estimate and the high value the high one, so
# the reported band brackets the uncertainty rather than hiding it. The first paid run
# reports `usage.total_tokens` and the band is replaced by a measurement.
# ---------------------------------------------------------------------------
CHARS_PER_TOKEN_HIGH: Final[Mapping[Script, float]] = {
    Script.LATIN: 4.0,
    Script.DEVANAGARI: 1.5,
    Script.TELUGU: 1.5,
    Script.MIXED: 2.5,
}
CHARS_PER_TOKEN_LOW: Final[Mapping[Script, float]] = {
    Script.LATIN: 3.0,
    Script.DEVANAGARI: 0.5,
    Script.TELUGU: 0.5,
    Script.MIXED: 1.2,
}


CANDIDATES: Final[tuple[Candidate, ...]] = (
    Candidate(
        key="offline-fake-256",
        kind=CandidateKind.OFFLINE_DETERMINISTIC,
        description=(
            "FakeEmbeddingProvider at 256 dims. Exercises the whole harness with no "
            "network and no cost. Width chosen to be obviously not a production "
            "candidate."
        ),
        dimensions=256,
        native_dimensions=256,
    ),
    Candidate(
        key="lexical-trigram",
        kind=CandidateKind.LEXICAL_BASELINE,
        description=(
            "Character-trigram Jaccard overlap, computed locally. The baseline every "
            "paid candidate must beat to justify its cost. Approximates what pg_trgm "
            "would do, and pg_trgm is already installed."
        ),
    ),
    Candidate(
        key="openai-3-small-1536",
        kind=CandidateKind.PAID_API,
        description="text-embedding-3-small at its native width.",
        model_id="text-embedding-3-small",
        dimensions=1536,
        native_dimensions=1536,
        supports_dimension_reduction=True,
        usd_per_million_tokens=0.02,
        max_input_tokens=8192,
        verified_on=PRICE_VERIFIED_ON,
    ),
    Candidate(
        key="openai-3-small-768",
        kind=CandidateKind.PAID_API,
        description=(
            "text-embedding-3-small reduced to 768. Halves storage and index size; "
            "the recall cost is exactly what anti-fact 17 says is unverified."
        ),
        model_id="text-embedding-3-small",
        dimensions=768,
        native_dimensions=1536,
        supports_dimension_reduction=True,
        usd_per_million_tokens=0.02,
        max_input_tokens=8192,
        verified_on=PRICE_VERIFIED_ON,
    ),
    Candidate(
        key="openai-3-small-512",
        kind=CandidateKind.PAID_API,
        description="text-embedding-3-small reduced to 512.",
        model_id="text-embedding-3-small",
        dimensions=512,
        native_dimensions=1536,
        supports_dimension_reduction=True,
        usd_per_million_tokens=0.02,
        max_input_tokens=8192,
        verified_on=PRICE_VERIFIED_ON,
    ),
    Candidate(
        key="openai-3-large-3072",
        kind=CandidateKind.PAID_API,
        description=(
            "text-embedding-3-large at its native width. Above the 2000-dim HNSW cap "
            "for `vector` (HC-24), so if this wins the column type is forced to "
            "`halfvec` — which is a finding, not a preference."
        ),
        model_id="text-embedding-3-large",
        dimensions=3072,
        native_dimensions=3072,
        supports_dimension_reduction=True,
        usd_per_million_tokens=0.13,
        max_input_tokens=8192,
        verified_on=PRICE_VERIFIED_ON,
    ),
    Candidate(
        key="openai-3-large-2000",
        kind=CandidateKind.PAID_API,
        description=(
            "text-embedding-3-large reduced to 2000 — the largest width `vector` can "
            "index under HC-24. The interesting configuration: 3-large quality while "
            "leaving both column types available."
        ),
        model_id="text-embedding-3-large",
        dimensions=2000,
        native_dimensions=3072,
        supports_dimension_reduction=True,
        usd_per_million_tokens=0.13,
        max_input_tokens=8192,
        verified_on=PRICE_VERIFIED_ON,
    ),
    Candidate(
        key="openai-3-large-1024",
        kind=CandidateKind.PAID_API,
        description="text-embedding-3-large reduced to 1024, for the cost/quality curve.",
        model_id="text-embedding-3-large",
        dimensions=1024,
        native_dimensions=3072,
        supports_dimension_reduction=True,
        usd_per_million_tokens=0.13,
        max_input_tokens=8192,
        verified_on=PRICE_VERIFIED_ON,
    ),
    # ---------------------------------------------------------------------
    # Declared, NOT implemented, NOT approved. DECISION 7 requires showing the
    # infrastructure cost before installing anything to run these, so they carry the
    # note and nothing else. This package imports no ML framework.
    # ---------------------------------------------------------------------
    Candidate(
        key="local-multilingual-e5-large",
        kind=CandidateKind.LOCAL_MODEL,
        description=(
            "A multilingual sentence encoder with asymmetric query/passage prefixes. "
            "Candidate on the hypothesis that a multilingual-trained model handles "
            "cross-script Indic retrieval better than a Latin-dominant one. "
            "AVAILABILITY, LICENCE AND WIDTH ALL UNVERIFIED — must be checked against "
            "primary sources before this becomes a real candidate."
        ),
        infrastructure_note=(
            "Needs torch + sentence-transformers (~2-3 GB of wheels) and, for a "
            "usable batch throughput, a GPU host we do not have. Serving it in "
            "production would be a new deployment unit, so it must clear Gate G0 "
            "(servability) before its quality is even interesting."
        ),
    ),
    Candidate(
        key="local-bge-m3",
        kind=CandidateKind.LOCAL_MODEL,
        description=(
            "A multi-granularity multilingual encoder, same hypothesis as above. "
            "AVAILABILITY, LICENCE AND WIDTH ALL UNVERIFIED."
        ),
        infrastructure_note=(
            "Same dependency and hosting commitment as the entry above. Declared so "
            "the trade-off is visible, not so it runs."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """An estimated cost band for one candidate over one dataset."""

    candidate_key: str
    corpus_tokens_low: int
    corpus_tokens_high: int
    query_tokens_low: int
    query_tokens_high: int
    usd_low: float
    usd_high: float
    #: True when the candidate costs nothing to run.
    is_free: bool

    @property
    def total_tokens_low(self) -> int:
        return self.corpus_tokens_low + self.query_tokens_low

    @property
    def total_tokens_high(self) -> int:
        return self.corpus_tokens_high + self.query_tokens_high


def token_band(text: str, script: Script) -> tuple[int, int]:
    """Estimated `(low, high)` token count for one text.

    Both bounds are derived from the characters-per-token assumptions above. Returns
    a band rather than a point because the assumption is unverified, and a point
    estimate would be quoted as a figure.
    """
    characters = len(text)
    high_ratio = CHARS_PER_TOKEN_LOW[script]
    low_ratio = CHARS_PER_TOKEN_HIGH[script]
    low = int(characters / low_ratio) + 1
    high = int(characters / high_ratio) + 1
    return low, high


def estimate_cost(candidate: Candidate, dataset: Dataset) -> CostEstimate:
    """Estimate what running one candidate over the dataset would cost.

    The corpus is embedded once per candidate; every query is embedded once per
    candidate. Reduced-width configurations of the same model are **separate**
    requests and are therefore charged separately — a detail worth stating, because
    "it is the same model" invites assuming otherwise and undercounting by the number
    of widths tested.
    """
    corpus_low = 0
    corpus_high = 0
    for passage in dataset.passages:
        low, high = token_band(passage.text, passage.script)
        corpus_low += low
        corpus_high += high

    query_low = 0
    query_high = 0
    for query in dataset.queries:
        low, high = token_band(query.text, query.script)
        query_low += low
        query_high += high

    rate = candidate.usd_per_million_tokens
    if rate is None:
        return CostEstimate(
            candidate_key=candidate.key,
            corpus_tokens_low=corpus_low,
            corpus_tokens_high=corpus_high,
            query_tokens_low=query_low,
            query_tokens_high=query_high,
            usd_low=0.0,
            usd_high=0.0,
            is_free=True,
        )

    per_token = rate / 1_000_000
    return CostEstimate(
        candidate_key=candidate.key,
        corpus_tokens_low=corpus_low,
        corpus_tokens_high=corpus_high,
        query_tokens_low=query_low,
        query_tokens_high=query_high,
        usd_low=(corpus_low + query_low) * per_token,
        usd_high=(corpus_high + query_high) * per_token,
        is_free=False,
    )


#: The dataset size the bake-off is designed for, per docs/research/D8_BAKEOFF.md.
#:
#: The seed dataset is far smaller, so a cost estimate over it understates the real
#: bill by more than an order of magnitude. Projecting to target is the number an
#: approval decision actually needs — approving a run that costs cents while the real
#: run costs dollars is approving the wrong thing.
TARGET_PASSAGES: Final[int] = 600
TARGET_QUERIES: Final[int] = 250


def project_to_target(
    estimate: CostEstimate,
    dataset: Dataset,
    *,
    target_passages: int = TARGET_PASSAGES,
    target_queries: int = TARGET_QUERIES,
) -> CostEstimate:
    """Scale an estimate from the current dataset up to the target dataset size.

    Linear in item count, which assumes the target items have the same average length
    and script mix as the seed. That is an assumption, and it is the reason this is
    labelled a projection rather than an estimate: it inherits the token-band
    uncertainty *and* adds a composition assumption on top.
    """
    passages = len(dataset.passages) or 1
    queries = len(dataset.queries) or 1
    corpus_scale = target_passages / passages
    query_scale = target_queries / queries

    corpus_low = int(estimate.corpus_tokens_low * corpus_scale)
    corpus_high = int(estimate.corpus_tokens_high * corpus_scale)
    query_low = int(estimate.query_tokens_low * query_scale)
    query_high = int(estimate.query_tokens_high * query_scale)

    if estimate.is_free:
        usd_low = usd_high = 0.0
    else:
        # Recover the per-token rate from the estimate rather than looking the
        # candidate up again: it keeps this function a pure transform of its input and
        # makes it impossible for the projection to use a different price than the
        # estimate it projects.
        tokens = estimate.total_tokens_low or 1
        per_token = estimate.usd_low / tokens
        usd_low = (corpus_low + query_low) * per_token
        usd_high = (corpus_high + query_high) * per_token

    return CostEstimate(
        candidate_key=estimate.candidate_key,
        corpus_tokens_low=corpus_low,
        corpus_tokens_high=corpus_high,
        query_tokens_low=query_low,
        query_tokens_high=query_high,
        usd_low=usd_low,
        usd_high=usd_high,
        is_free=estimate.is_free,
    )


def paid_candidates(candidates: Iterable[Candidate] = CANDIDATES) -> tuple[Candidate, ...]:
    return tuple(item for item in candidates if item.kind is CandidateKind.PAID_API)


def offline_candidates(candidates: Iterable[Candidate] = CANDIDATES) -> tuple[Candidate, ...]:
    return tuple(item for item in candidates if item.kind.is_runnable_offline)


def total_paid_cost(
    dataset: Dataset, candidates: Sequence[Candidate] | None = None
) -> tuple[float, float]:
    """`(low, high)` USD to run every paid candidate once over the dataset."""
    selected = candidates if candidates is not None else paid_candidates()
    estimates = [estimate_cost(candidate, dataset) for candidate in selected]
    return (
        sum(estimate.usd_low for estimate in estimates),
        sum(estimate.usd_high for estimate in estimates),
    )
