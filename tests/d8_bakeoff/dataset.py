"""The D-8 evaluation dataset: schema, loader and readiness accounting.

Two YAML files, both committed, both reviewed like code:

* `data/corpus.yaml` — passages, each with a language, a script and a **validation
  status**.
* `data/queries.yaml` — queries, each with a subset and a graded relevance mapping
  onto passage ids.

**The validation status is the load-bearing field.** DECISION 6 permits building the
benchmark before native-speaker review, and forbids claiming synthetic Hindi or
Telugu is validated. So the status is per item, it is required, and it propagates:
a subset is *review-complete* only when every passage and every query in it is
`native_reviewed`, and a report is decision-grade only when every subset it scored
is review-complete. That turns "do not claim synthetic text is validated" from an
instruction someone has to remember into arithmetic the harness performs.

**Graded relevance, not binary.** A query about website pricing is *fully* answered
by the pricing passage (grade 2) and *partially* by the services overview (grade 1).
Binary judgements would score a candidate that returns the overview identically to
one that returns nothing useful, and nDCG needs the gradation to mean anything.

**Cross-script is a first-class subset, not a slice.** The whole reason D-8 exists is
that no provider publishes per-language Indic numbers (L-8), and the specific thing
nobody publishes is whether a query in one script retrieves a passage in another. An
aggregate score hides it completely, so the subsets name it.

**Provenance travels with every row.** `Provenance` records which intake entry a passage
or query came from, which version of the intake file, and which transform produced it —
so a paraphrase can never quietly become an independent business fact. Three variants of
one FAQ carry one `source_id` between them, and a corpus that looks like it covers a topic
three times can be shown to cover it once.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import yaml

__all__ = [
    "DATA_DIR",
    "UNATTRIBUTED",
    "Dataset",
    "MaterialTier",
    "Passage",
    "PassageRole",
    "Provenance",
    "Query",
    "Script",
    "Subset",
    "SubsetReadiness",
    "ValidationStatus",
    "load_dataset",
]

DATA_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "data"

#: Highest relevance grade a judgement may carry.
MAX_GRADE: Final[int] = 2


class Script(StrEnum):
    """The writing system a text is in. Distinct from its language.

    Hindi written in Latin characters and Hindi written in Devanagari are the same
    language in two scripts, and an embedding model can be good at one and useless at
    the other. Conflating them is how a bake-off reports "Hindi works" about a corpus
    that was entirely romanised.
    """

    LATIN = "latin"
    DEVANAGARI = "devanagari"
    TELUGU = "telugu"
    MIXED = "mixed"


class MaterialTier(StrEnum):
    """Where an item came from, and therefore what it may be used to claim.

    The four tiers exist because "synthetic" covers two very different things: a
    paraphrase mechanically derived from a reviewed fact, and a passage somebody made up.
    Collapsing them would either block the whole corpus on individual review of every
    generated variant, or let invented facts count as evidence. Neither is acceptable.
    """

    SOURCE_GROUNDED = "source_grounded"
    """Derived from real supplied business content. The only tier whose *facts* are
    trustworthy. Requires a `source_reference`."""

    CONTROLLED_SYNTHETIC = "controlled_synthetic"
    """Mechanically derived from source-grounded material by a documented, deterministic
    transform — a paraphrase, a script variant, a typo variant, a code-mix. The facts are
    inherited; only the *phrasing* is generated, which is what makes template-level review
    sufficient for it (see `Query.derived_from`)."""

    ADVERSARIAL = "adversarial"
    """A deliberate hard case: a near-duplicate service, a semantically close distractor, a
    stale/superseded passage, an instruction-shaped chunk, a price-bearing chunk. Present so
    the benchmark can measure whether a candidate ranks it above the truth. **Never gold.**"""

    NON_DECISION_SYNTHETIC = "non_decision_synthetic"
    """Invented outright. Fine for exercising machinery. **Never counts as decision
    evidence, whatever its review status** — there is nothing real for a reviewer to vouch
    for. This is the default when a tier is unstated, deliberately: a missing tier must fail
    safe rather than silently inflate the corpus."""

    @property
    def can_support_a_decision(self) -> bool:
        """Whether an item in this tier may contribute to a decision-grade report.

        `ADVERSARIAL` is included: a hard negative is a real, deliberate test case, and its
        *correctness as a negative* is exactly what a reviewer validates.
        """
        return self is not MaterialTier.NON_DECISION_SYNTHETIC


class PassageRole(StrEnum):
    """What a passage is for. Only `GOLD_CANDIDATE` may appear in a query's gold set."""

    GOLD_CANDIDATE = "gold_candidate"
    """Ordinary content. May be gold for a query."""

    DISTRACTOR = "distractor"
    """Semantically close but wrong — a similar service, or the right company with a
    capability it does not have. Must never be gold."""

    STALE = "stale"
    """A superseded version of content that exists elsewhere in fresh form. Retrieval
    returning it is the failure `documents.status = 'active'` exists to prevent."""

    INJECTION = "injection"
    """Instruction-shaped content. Must never be gold, and in production would be
    quarantined out of retrieval entirely (SECURITY §5.4)."""

    PRICE_BEARING = "price_bearing"
    """Contains a money-shaped number. Present because retrieval **will** encounter such
    chunks in the wild, and the benchmark must show what happens when it does. It is never
    gold for a pricing query: pricing authority is `get_service_pricing`, never RAG
    (PRD §6.5)."""

    @property
    def may_be_gold(self) -> bool:
        return self is PassageRole.GOLD_CANDIDATE


class ValidationStatus(StrEnum):
    """Who has vouched for this item's language quality."""

    SOURCE_MATERIAL = "source_material"
    """Taken from the tenant's own published English material. Authoritative as-is;
    no language review needed, because nobody translated anything."""

    SYNTHETIC_UNREVIEWED = "synthetic_unreviewed"
    """Authored for the benchmark and **not reviewed by a competent speaker.** Usable
    for exercising machinery. **Not usable as evidence about a language.**"""

    NATIVE_REVIEWED = "native_reviewed"
    """Reviewed and corrected by a competent speaker, recorded in `reviewed_by` and
    `reviewed_on`. The only status that makes an Indic subset decision-grade."""

    @property
    def is_review_complete(self) -> bool:
        return self in {ValidationStatus.SOURCE_MATERIAL, ValidationStatus.NATIVE_REVIEWED}


class Subset(StrEnum):
    """The reporting axis. Every metric is reported per subset, never only pooled."""

    EN = "en"
    HI_DEVANAGARI = "hi-deva"
    HI_ROMANISED = "hi-latn"
    TE_TELUGU = "te-telu"
    TE_ROMANISED = "te-latn"
    CODEMIX_EN_HI = "codemix-en-hi"
    CODEMIX_EN_TE = "codemix-en-te"
    CROSS_SCRIPT = "cross-script"
    """A query whose gold passages are in a different script from the query itself.
    The hardest case, the least documented, and the one an aggregate hides."""

    @property
    def needs_native_review(self) -> bool:
        """Whether this subset makes a claim about an Indian language.

        `EN` does not, so English source material is decision-grade immediately.
        Everything else does.
        """
        return self is not Subset.EN


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where an item came from, in the vocabulary of the intake files.

    Distinct from `source_reference`, which is prose a human wrote for a human ("Rise Next
    source material 2026-07-30, section: PRICING POLICY"). This is the machine-checkable
    version: which intake entry, which version of the intake file, which section, and which
    transform produced the text.

    **It exists to stop a paraphrase becoming an independent business fact.** A generated
    variant is not new information — it is one reviewed fact, said differently. When the
    only trace is prose, three variants of one FAQ read like three facts, and a benchmark
    that "covers" a topic three times covers it once. `source_id` collapses them back.
    """

    #: The intake entry this traces to — a `SourceFact` id, a `ServiceEntry` id, or a
    #: `QueryTemplate` id. The join key back to `source/`.
    source_id: str | None = None

    #: `source_material_version` or `phrasebook_version` at build time. A corpus built
    #: against version 1 and a review performed against version 2 are about different text,
    #: and without this the mismatch is undetectable.
    source_version: int | None = None

    #: The intake section — `policies`, `faqs`, `pricing_policy`, `phrasebook`, … Carried so
    #: a retrieval result can be traced to the *kind* of business content it came from.
    source_type: str | None = None

    #: The deterministic transform that produced this text: `service_capabilities`,
    #: `typo:transpose`, `negative:near_duplicate`. Reproducibility.
    #:
    #: Not the same thing as `derived_from`, and the difference matters. `derived_from` names
    #: the *reviewed artifact* whose review this row inherits — the thing an auditor goes and
    #: looks at. `generated_from` names the *transform* — the thing an engineer re-runs.
    generated_from: str | None = None

    #: Whether a human still has to look at this. Defaults to `True`: an item that arrived
    #: without saying is one nobody has vouched for.
    #:
    #: Redundant with `validation` by construction, and deliberately so — it is the field the
    #: supplying team reads, and `Passage`/`Query` refuse the combination that would make it
    #: a lie (`False` on something not review-complete).
    human_review_required: bool = True


#: The provenance of an item that did not declare any: no origin, and review still required.
#:
#: Shared rather than constructed per row because it is immutable, and named rather than
#: inlined because "unattributed" is the state a reader needs to recognise.
UNATTRIBUTED: Final[Provenance] = Provenance()


def _check_provenance(what: str, provenance: Provenance, validation: ValidationStatus) -> None:
    """Refuse a provenance record that contradicts the validation status.

    One direction only. `human_review_required=True` on reviewed material is merely
    cautious — someone may want a second pass. `False` on unreviewed material is the claim
    that matters, and it is exactly the claim DECISION 6 forbids: it would let generated
    Hindi present itself as validated without any reviewer having seen it.
    """
    if not provenance.human_review_required and not validation.is_review_complete:
        raise ValueError(
            f"{what} declares human_review_required=false but its validation is "
            f"{validation.value}. Nobody has vouched for it, so it still needs review."
        )


@dataclass(frozen=True, slots=True)
class Passage:
    """One corpus passage — a chunk candidate, before chunking."""

    id: str
    text: str
    language: str
    script: Script
    validation: ValidationStatus
    reviewed_by: str | None = None
    reviewed_on: str | None = None
    #: Defaults to the safest tier, so an unstated tier cannot inflate the corpus.
    tier: MaterialTier = MaterialTier.NON_DECISION_SYNTHETIC
    role: PassageRole = PassageRole.GOLD_CANDIDATE
    #: Where the fact came from. Required for `SOURCE_GROUNDED`.
    source_reference: str | None = None
    #: The source fact or template this was mechanically derived from, if it was.
    derived_from: str | None = None
    #: Machine-checkable origin. See `Provenance`.
    provenance: Provenance = UNATTRIBUTED

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("A passage needs an id.")
        if not self.text.strip():
            raise ValueError(f"Passage {self.id} has no text.")
        if self.validation is ValidationStatus.NATIVE_REVIEWED and not self.reviewed_by:
            # Otherwise "reviewed" is a word someone typed, and there is nobody to ask
            # when a judgement is disputed.
            raise ValueError(f"Passage {self.id} claims review but names no reviewer.")
        if self.tier is MaterialTier.SOURCE_GROUNDED and not self.source_reference:
            # "Source-grounded" with no source is the specific mislabelling that would let
            # invented content be quoted as fact.
            raise ValueError(
                f"Passage {self.id} claims to be source-grounded but names no source_reference."
            )
        if self.tier is MaterialTier.CONTROLLED_SYNTHETIC and not self.derived_from:
            raise ValueError(
                f"Passage {self.id} claims to be a controlled variant but names no "
                "derived_from; a variant with no origin cannot inherit its facts."
            )
        _check_provenance(f"Passage {self.id}", self.provenance, self.validation)


@dataclass(frozen=True, slots=True)
class Query:
    """One query with its graded gold set."""

    id: str
    text: str
    subset: Subset
    script: Script
    validation: ValidationStatus
    #: passage id -> grade in 1..MAX_GRADE. A grade of 0 is expressed by omission.
    relevant: Mapping[str, int]
    reviewed_by: str | None = None
    reviewed_on: str | None = None
    tier: MaterialTier = MaterialTier.NON_DECISION_SYNTHETIC
    #: The phrasebook template id this was generated from, if it was.
    #:
    #: This is the review-efficiency mechanism. Reviewing one template in Hindi validates
    #: the *phrasing* of every query generated from it, so a reviewer validates ~20
    #: templates instead of ~300 queries. `derived_from` is what makes that auditable
    #: rather than a claim: an auditor can see exactly which reviewed artifact vouched for
    #: this query. The propagation is bounded by a spot-check floor — see
    #: `quality.py::SPOT_CHECK_FRACTION`.
    derived_from: str | None = None
    #: Set when review was inherited from `derived_from` rather than performed on this row.
    review_inherited: bool = False
    #: Free-text reviewer notes, carried through the review round-trip.
    notes: str | None = None
    #: Machine-checkable origin. See `Provenance`.
    provenance: Provenance = UNATTRIBUTED
    #: What the query asks — the value of `corpus.source_material.Intent`, as a plain string.
    #:
    #: The second reporting axis after `subset`, and the field that makes the adversarial
    #: benchmarks checkable rather than assumed: without it, "every pricing query's gold is
    #: the pricing policy" is a claim about the corpus that nothing verifies.
    #:
    #: A string rather than the enum so `dataset` does not import from `corpus` — the
    #: dependency runs corpus → dataset, and the dataset must stay loadable from YAML alone.
    intent: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("A query needs an id.")
        if not self.text.strip():
            raise ValueError(f"Query {self.id} has no text.")
        if self.review_inherited and not self.derived_from:
            raise ValueError(
                f"Query {self.id} claims inherited review but names no derived_from; "
                "there would be nothing to audit the inheritance against."
            )
        if not self.relevant:
            # A query with no gold set scores 0 for every candidate, which drags every
            # average down identically and looks like a model problem.
            raise ValueError(f"Query {self.id} has no relevant passages.")
        for passage_id, grade in self.relevant.items():
            if not 1 <= grade <= MAX_GRADE:
                raise ValueError(
                    f"Query {self.id} grades {passage_id} as {grade}; "
                    f"grades run 1..{MAX_GRADE} and 0 is expressed by omission."
                )
        if self.validation is ValidationStatus.NATIVE_REVIEWED and not self.reviewed_by:
            raise ValueError(f"Query {self.id} claims review but names no reviewer.")
        _check_provenance(f"Query {self.id}", self.provenance, self.validation)

    @property
    def gold_ids(self) -> frozenset[str]:
        return frozenset(self.relevant)


@dataclass(frozen=True, slots=True)
class SubsetReadiness:
    """Whether one subset may carry a decision."""

    subset: Subset
    query_count: int
    unreviewed_queries: tuple[str, ...]
    unreviewed_passages: tuple[str, ...]

    @property
    def is_review_complete(self) -> bool:
        return not self.unreviewed_queries and not self.unreviewed_passages

    @property
    def blocking_reason(self) -> str | None:
        if self.is_review_complete:
            return None
        return (
            f"{self.subset.value}: {len(self.unreviewed_queries)} queries and "
            f"{len(self.unreviewed_passages)} passages await native-speaker review"
        )


@dataclass(frozen=True, slots=True)
class Dataset:
    """The loaded, validated dataset."""

    version: int
    passages: tuple[Passage, ...]
    queries: tuple[Query, ...]

    @property
    def by_id(self) -> Mapping[str, Passage]:
        return {passage.id: passage for passage in self.passages}

    def queries_in(self, subset: Subset) -> tuple[Query, ...]:
        return tuple(query for query in self.queries if query.subset is subset)

    @property
    def subsets(self) -> tuple[Subset, ...]:
        """Subsets that actually have queries, in declaration order."""
        present = {query.subset for query in self.queries}
        return tuple(subset for subset in Subset if subset in present)

    def readiness(self, subset: Subset) -> SubsetReadiness:
        """Per-subset review accounting — the mechanism behind DECISION 6."""
        queries = self.queries_in(subset)
        lookup = self.by_id

        if not subset.needs_native_review:
            # An English subset built from the tenant's own material needs no language
            # review; `is_review_complete` on the status already encodes that.
            unreviewed_queries = tuple(
                query.id
                for query in queries
                if not query.tier.can_support_a_decision or not query.validation.is_review_complete
            )
        else:
            unreviewed_queries = tuple(
                query.id
                for query in queries
                if not query.tier.can_support_a_decision
                or query.validation is not ValidationStatus.NATIVE_REVIEWED
            )

        touched: set[str] = set()
        for query in queries:
            touched.update(query.gold_ids)
        unreviewed_passages = tuple(
            sorted(
                passage_id
                for passage_id in touched
                if passage_id in lookup
                and not _passage_is_review_complete(lookup[passage_id], subset)
            )
        )
        return SubsetReadiness(
            subset=subset,
            query_count=len(queries),
            unreviewed_queries=unreviewed_queries,
            unreviewed_passages=unreviewed_passages,
        )

    @property
    def review_complete_subsets(self) -> tuple[Subset, ...]:
        return tuple(subset for subset in self.subsets if self.readiness(subset).is_review_complete)

    @property
    def is_review_complete(self) -> bool:
        """Whether **every** present subset may carry a decision."""
        return all(self.readiness(subset).is_review_complete for subset in self.subsets)


def _passage_is_review_complete(passage: Passage, subset: Subset) -> bool:
    """Whether a passage is good enough to support a claim about `subset`.

    An English source-material passage is fine inside a Hindi subset — a cross-script
    query legitimately points at an English gold passage, and nobody translated it, so
    there is nothing to review. What is *not* fine is unreviewed synthetic Indic text
    anywhere in a subset that makes a claim about an Indian language.

    **The tier check comes first and is unconditional.** An item nobody grounded in real
    content cannot become decision evidence by being reviewed — there is nothing real for
    a reviewer to vouch for, so a `native_reviewed` stamp on invented material means only
    "this invented sentence is fluent". That is tier D's whole purpose, and putting the
    check ahead of the status check is what stops a review pass from laundering it.
    """
    if not passage.tier.can_support_a_decision:
        return False
    if passage.validation is ValidationStatus.NATIVE_REVIEWED:
        return True
    if passage.validation is ValidationStatus.SOURCE_MATERIAL:
        return passage.script is Script.LATIN or not subset.needs_native_review
    return False


def load_dataset(directory: pathlib.Path | None = None) -> Dataset:
    """Load and validate the dataset. Raises `ValueError` on any inconsistency.

    **Two pairs of files are merged, and they are kept separate on disk on purpose.**

    * `corpus.yaml` / `queries.yaml` — the hand-written seed. **Retired and empty since
      2026-08-11**: it was invented content, which no review can turn into decision
      evidence, and the official Rise Next material now carries the corpus. The files stay
      because the pair is required, and an empty file says "retired" where a missing one
      would say "something went wrong".
    * `generated_corpus.yaml` / `generated_queries.yaml` — built from `source/` by
      `corpus.build`, and safe to delete and regenerate.

    Each *pair* must agree on `dataset_version`, because a query set scored against the
    wrong corpus is the failure the version exists to prevent. The two pairs need not agree
    with each other: generated queries reference only generated passages, so a version bump
    on one side cannot silently mis-pair anything. The merged dataset reports the generated
    version when generated files are present, since that is the artifact under measurement.

    Validation is deliberately strict and total: a dataset that half-loads produces a
    benchmark that half-measures, and the failure would show up as an unexplained score
    difference rather than as an error.
    """
    root = directory or DATA_DIR
    seed_version, seed_passages, seed_queries = _load_pair(
        root / "corpus.yaml", root / "queries.yaml", required=True
    )
    generated_version, generated_passages, generated_queries = _load_pair(
        root / "generated_corpus.yaml", root / "generated_queries.yaml", required=False
    )

    passages = (*seed_passages, *generated_passages)
    queries = (*seed_queries, *generated_queries)
    corpus_version = generated_version if generated_version is not None else seed_version
    if corpus_version is None:  # pragma: no cover - `required=True` guarantees a version
        raise ValueError("The seed dataset carries no dataset_version.")

    seen: set[str] = set()
    for passage in passages:
        if passage.id in seen:
            raise ValueError(f"Duplicate passage id: {passage.id}")
        seen.add(passage.id)
    query_ids: set[str] = set()
    for query in queries:
        if query.id in query_ids:
            raise ValueError(f"Duplicate query id: {query.id}")
        query_ids.add(query.id)
        dangling = sorted(query.gold_ids - seen)
        if dangling:
            # A dangling gold id is unreachable, so the query can never score — and
            # it looks exactly like a retrieval failure.
            raise ValueError(f"Query {query.id} references unknown passages: {dangling}")

    by_id = {passage.id: passage for passage in passages}
    for query in queries:
        ineligible = sorted(
            passage_id for passage_id in query.gold_ids if not by_id[passage_id].role.may_be_gold
        )
        if ineligible:
            # A distractor or an injection chunk as gold inverts the test: the candidate
            # would be *rewarded* for retrieving the thing it must not retrieve.
            raise ValueError(
                f"Query {query.id} lists non-gold-eligible passages as gold: {ineligible}. "
                "Distractors, stale, injection and price-bearing passages are never gold."
            )

    return Dataset(version=corpus_version, passages=passages, queries=queries)


def _load_pair(
    corpus_path: pathlib.Path, queries_path: pathlib.Path, *, required: bool
) -> tuple[int | None, tuple[Passage, ...], tuple[Query, ...]]:
    """Load one corpus/queries pair, asserting the two agree on version.

    An optional pair must be present or absent **together**. Half a pair means someone
    generated a corpus and not its queries (or deleted one of the two), and loading the
    surviving half would silently change what the benchmark measures.
    """
    corpus_there = corpus_path.is_file()
    queries_there = queries_path.is_file()
    if not required and not corpus_there and not queries_there:
        return None, (), ()
    if not corpus_there or not queries_there:
        missing = corpus_path if not corpus_there else queries_path
        raise ValueError(
            f"Dataset file is missing: {missing}. A corpus and its queries must be "
            "present together — half a pair changes what is measured without saying so."
        )

    corpus_document = _read(corpus_path)
    queries_document = _read(queries_path)
    corpus_version = int(corpus_document["dataset_version"])
    queries_version = int(queries_document["dataset_version"])
    if corpus_version != queries_version:
        raise ValueError(
            f"{corpus_path.name} is version {corpus_version} and {queries_path.name} is "
            f"{queries_version}; a query set must be scored against the corpus it was "
            "written for."
        )
    return (
        corpus_version,
        tuple(_passage(row) for row in corpus_document["passages"] or ()),
        tuple(_query(row) for row in queries_document["queries"] or ()),
    )


def _read(path: pathlib.Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise ValueError(f"Dataset file is missing: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError(f"Dataset file is not a mapping: {path}")
    return loaded


def _provenance(row: Mapping[str, Any]) -> Provenance:
    """Read the `provenance` block, defaulting to "nobody has vouched for this".

    An absent block yields `human_review_required=True`, matching the fail-safe default
    everywhere else in this module: silence is not consent.
    """
    raw = row.get("provenance") or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{row.get('id')} has a malformed `provenance` block.")
    version = raw.get("source_version")
    return Provenance(
        source_id=_optional_str(raw.get("source_id")),
        source_version=int(version) if version is not None else None,
        source_type=_optional_str(raw.get("source_type")),
        generated_from=_optional_str(raw.get("generated_from")),
        human_review_required=bool(raw.get("human_review_required", True)),
    )


def _passage(row: Mapping[str, Any]) -> Passage:
    return Passage(
        id=str(row["id"]),
        text=str(row["text"]).strip(),
        language=str(row["language"]),
        script=Script(row["script"]),
        validation=ValidationStatus(row["validation"]),
        reviewed_by=_optional_str(row.get("reviewed_by")),
        reviewed_on=_optional_str(row.get("reviewed_on")),
        tier=MaterialTier(row.get("tier", MaterialTier.NON_DECISION_SYNTHETIC.value)),
        role=PassageRole(row.get("role", PassageRole.GOLD_CANDIDATE.value)),
        source_reference=_optional_str(row.get("source_reference")),
        derived_from=_optional_str(row.get("derived_from")),
        provenance=_provenance(row),
    )


def _query(row: Mapping[str, Any]) -> Query:
    relevant_raw = row["relevant"]
    if not isinstance(relevant_raw, Mapping):
        raise ValueError(f"Query {row.get('id')} has a malformed `relevant` mapping.")
    return Query(
        id=str(row["id"]),
        text=str(row["text"]).strip(),
        subset=Subset(row["subset"]),
        script=Script(row["script"]),
        validation=ValidationStatus(row["validation"]),
        relevant={str(key): int(value) for key, value in relevant_raw.items()},
        reviewed_by=_optional_str(row.get("reviewed_by")),
        reviewed_on=_optional_str(row.get("reviewed_on")),
        tier=MaterialTier(row.get("tier", MaterialTier.NON_DECISION_SYNTHETIC.value)),
        derived_from=_optional_str(row.get("derived_from")),
        review_inherited=bool(row.get("review_inherited", False)),
        notes=_optional_str(row.get("notes")),
        provenance=_provenance(row),
        intent=_optional_str(row.get("intent")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def passage_texts(passages: Sequence[Passage]) -> tuple[str, ...]:
    """Passage texts in order, for a batched embedding call."""
    return tuple(passage.text for passage in passages)
