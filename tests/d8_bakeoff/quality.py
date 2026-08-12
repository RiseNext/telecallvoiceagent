"""Corpus quality gates. The checks a corpus must pass **before** anyone pays to run it.

The loader already refuses structural nonsense — dangling gold ids, duplicate ids, invalid
grades, a query with no gold, a mismatched dataset version, a distractor listed as gold. A
corpus that loads is therefore already internally consistent. These gates answer a
different question: **is it big enough, balanced enough, adversarial enough and reviewed
enough to be worth spending money on.**

Each gate is `blocking` or advisory. A blocking failure means a paid run would produce a
number nobody should act on, so `corpus_is_benchmark_ready` refuses. Advisory failures are
printed and do not stop anything.

The gate that does the most work is `no_query_inflation`. Every other gate can be satisfied
by adding material; that one cannot, because the cheapest way to hit a query target is to
generate near-duplicates of what already exists — and a subset of 40 rephrasings of the same
question reports as 40 queries while measuring one. It compares within subsets by trigram
overlap and fails on excessive similarity.

Four gates encode business constraints rather than corpus statistics, and they are the ones
whose failure would be invisible in a score:

* `no_numeric_prices_in_corpus` — no passage carries a money-shaped number, so a pricing
  question cannot be answered from retrieval **by construction**.
* `pricing_gold_is_policy` — a pricing question's correct answer is the pricing policy.
* `lending_gold_is_disclaimer` — "do you give loans?" points at the not-a-lender disclaimer.
* `adversarial_intents_present` — every adversarial intent appears in at least one Indic
  subset, because a guardrail that holds only in English is not a guardrail.

What these gates measure is **retrievability, not behaviour.** D-8 runs no model, so it
cannot show that an agent declines to quote a price; it can show that the policy which lets
it decline is reachable from the question, in every language. The behavioural half is an
`agent_eval` case and is deliberately not this suite's job.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from rn_domain.chunking import FROZEN_CHUNKING_V1
from tests.d8_bakeoff.candidates import TARGET_PASSAGES, TARGET_QUERIES
from tests.d8_bakeoff.corpus.source_material import Intent
from tests.d8_bakeoff.dataset import Dataset, MaterialTier, PassageRole, Query, Subset
from tests.d8_bakeoff.harness import lexical_similarity

__all__ = [
    "MIN_ADVERSARIAL_ROLES",
    "MIN_CROSS_SCRIPT_QUERIES",
    "MIN_QUERIES_PER_SUBSET",
    "NEAR_DUPLICATE_PASSAGE_THRESHOLD",
    "NEAR_DUPLICATE_THRESHOLD",
    "SPOT_CHECK_FRACTION",
    "CorpusGate",
    "corpus_is_benchmark_ready",
    "evaluate_corpus",
    "render_gates",
]

#: Below this, a subset's score is noise. 20 queries gives answerability@8 a resolution of
#: 5 percentage points, which is about the coarsest that can distinguish two candidates
#: against a gate set at 0.90.
MIN_QUERIES_PER_SUBSET: Final[int] = 20

#: Cross-script is the subset D-8 exists for, so it does not get to be the thin one.
MIN_CROSS_SCRIPT_QUERIES: Final[int] = 20

#: Trigram-overlap ceiling for two queries in the same subset. Above this they are asking
#: the same thing in the same words, and counting both inflates the subset without adding
#: information. Deliberately not 1.0: exact duplicates are the easy case, and the failure
#: worth catching is 40 rephrasings that differ by a word.
NEAR_DUPLICATE_THRESHOLD: Final[float] = 0.85

#: Fraction of each subset's queries that must be reviewed **individually**, not by
#: inheritance from a template.
#:
#: This is the bound on template-level review. Reviewing one Hindi template legitimately
#: validates the phrasing of every query generated from it — that is the efficiency the
#: phrasebook exists for. What it cannot validate is whether the *substitution* reads
#: naturally in every case, and a slot fill that is fine for "website development" can be
#: wrong for "cloud migration". The spot check is what turns propagation from a loophole
#: into a sampling strategy.
SPOT_CHECK_FRACTION: Final[float] = 0.10

#: Trigram-overlap ceiling for two **passages**.
#:
#: Higher than the query threshold, and deliberately. Two passages about one business
#: legitimately share vocabulary — every capability passage carries its service's
#: description, which is what makes it a self-contained chunk. What must not happen is two
#: passages that are the *same* passage, because retrieval would then have two correct
#: answers where the gold set names one, and every candidate would be scored on a coin flip.
NEAR_DUPLICATE_PASSAGE_THRESHOLD: Final[float] = 0.90

#: Adversarial roles the corpus must actually contain. Each models a distinct failure, so a
#: corpus missing one is blind to it.
#:
#: `PRICE_BEARING` is deliberately **not** here, and its absence is the interesting one. A
#: price-bearing passage can only be built from a price the business actually supplied, and
#: requiring one would create pressure to invent a figure to satisfy a gate — which is the
#: exact failure the gate was written to prevent. `no_numeric_prices_in_corpus` covers the
#: same ground from the other side and covers it more strongly: instead of "prices are
#: labelled as traps", it asserts no price exists to be retrieved at all.
MIN_ADVERSARIAL_ROLES: Final[frozenset[PassageRole]] = frozenset(
    {
        PassageRole.DISTRACTOR,
        PassageRole.STALE,
        PassageRole.INJECTION,
    }
)

#: Why each adversarial role matters, and what supplying it requires. Printed on failure so
#: a gap reads as an actionable request to the business rather than as a build error.
_ROLE_REMEDY: Final[Mapping[PassageRole, str]] = {
    PassageRole.DISTRACTOR: (
        "declare `near_duplicate_of` between two confusable services in the intake file"
    ),
    PassageRole.STALE: (
        "supply superseded content in the intake file's `superseded` section — an old service "
        "description or a withdrawn offer. It cannot be synthesised: invented 'old' text is a "
        "semantic distractor wearing a stale label, and would measure the wrong thing"
    ),
    PassageRole.INJECTION: "add services, from which instruction-shaped passages are built",
}

#: Roles whose absence can only be fixed by the **business handing over content**.
#:
#: `STALE` is the only one, and it is the reason the distinction exists. A stale passage must
#: be what the old version genuinely said; anything written for the purpose is a semantic
#: distractor wearing a stale label, and would report a covered failure mode while measuring
#: a different one. The other two roles are generated from material already supplied, so
#: their absence would be a real defect.
_EXTERNALLY_SUPPLIED_ROLES: Final[frozenset[PassageRole]] = frozenset({PassageRole.STALE})


@dataclass(frozen=True, slots=True)
class CorpusGate:
    """One gate's outcome."""

    name: str
    passed: bool
    detail: str
    blocking: bool = True
    #: Whether closing this needs a human to **supply something** rather than a code change.
    #:
    #: Reported as `BLOCK` instead of `FAIL`, and the distinction is diagnostic only —
    #: `corpus_is_benchmark_ready` treats it exactly like any other failure, because a
    #: corpus missing native-speaker review is no more usable for a decision than one with
    #: a broken gate. What it buys is that a *regression* stands out: if
    #: `no_passage_duplication` ever fails it shows as `FAIL`, and someone can see at a
    #: glance that the corpus broke rather than that it is still waiting on a person.
    #:
    #: **Not an excuse.** A gate does not become blocked by being inconvenient; it is
    #: blocked when the missing input provably cannot be produced from the repository —
    #: superseded business content, or a competent speaker's judgement.
    blocked_on_external_input: bool = False

    @property
    def marker(self) -> str:
        if self.passed:
            return "PASS"
        if not self.blocking:
            return "warn"
        return "BLOCK" if self.blocked_on_external_input else "FAIL"


def evaluate_corpus(dataset: Dataset) -> tuple[CorpusGate, ...]:
    """Run every gate. Order is stable so two runs' output is diffable."""
    return (
        _size(dataset),
        _subset_balance(dataset),
        _cross_script_genuine(dataset),
        _adversarial_present(dataset),
        _no_pricing_as_truth(dataset),
        _no_numeric_prices(dataset),
        _pricing_gold_is_policy(dataset),
        _lending_gold_is_disclaimer(dataset),
        _adversarial_intents_present(dataset),
        _no_query_inflation(dataset),
        _no_passage_duplication(dataset),
        _review_completeness(dataset),
        _spot_check(dataset),
        _tier_declared(dataset),
        _chunking_policy(dataset),
    )


def corpus_is_benchmark_ready(dataset: Dataset) -> bool:
    """Whether a paid run on this corpus would produce a number worth acting on.

    `blocked_on_external_input` is deliberately **not** consulted. A gate that is blocked is
    still a gate that has not passed, and a corpus whose Indic text nobody has read is no
    more able to carry a decision than one with a broken generator. The flag exists to tell a
    reader *who* has to act, never to let a report through.
    """
    return all(gate.passed for gate in evaluate_corpus(dataset) if gate.blocking)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _size(dataset: Dataset) -> CorpusGate:
    """Corpus size against the pre-registered target.

    **`TARGET_PASSAGES` is left at 600 and this gate is left failing**, because the supplied
    Rise Next material is fully decomposed at 143 passages and the only ways to reach 600 are
    to receive more real content or to invent facts. Lowering the number to match the corpus
    would be rationalising after seeing the data, and inventing content would trade the one
    property that makes this corpus worth anything.

    It is marked `blocked_on_external_input` rather than left as a bare `FAIL` because the
    measured evidence says it is not a defect: on the pre-decomposition corpus the two
    offline baselines separated cleanly and every subset sat far below its G1 gate — nothing
    near ceiling. The threshold was estimated, never measured.

    **The replacement — D8_BAKEOFF §11 Option B, "the best candidate beats the lexical
    baseline by a margin on every subset" — was taken to implementation on 2026-08-11 and
    stopped.** As documented it names no number, and four inputs have to be decided before it
    can be written down as a gate. None is derivable from this repository:

    1. **The margin.** §11 reads "margin X", literally. §8's G1 thresholds (0.90 / 0.85) are
       *candidate acceptance* — whether a model is good enough to ship — not corpus adequacy,
       which asks whether the corpus can tell two candidates apart. Reusing one for the other
       would be a category error wearing a real number.
    2. **Which metric, at which k.** `metrics.py` offers four at k ∈ {4, 8, 16}; §11 names
       none.
    3. **Absolute or relative.** On `cross-script` the measured offline pair is 0.258 vs
       0.065: +19.3 pp absolute, but four times over in relative terms. A margin of "0.10"
       passes on one reading of that and fails on the other, and the two readings disagree
       hardest on the subset D-8 exists for.
    4. **What "best candidate" means before the paid run.** `CandidateKind.is_decision_grade`
       excludes both offline kinds structurally. Reading it as a paid candidate makes the gate
       that authorises the paid run depend on that run's results; reading it as the offline
       fake narrows `is_decision_grade`, which belongs in an ADR rather than in a gate body.

    Choosing any of the four here — after the numbers are already known — is precisely what
    §8 calls rationalisation rather than measurement. So `TARGET_PASSAGES` stays at 600, this
    gate stays blocked, and the decision stays visible instead of being absorbed into code.
    """
    passages = len(dataset.passages)
    queries = len(dataset.queries)
    ok = passages >= TARGET_PASSAGES and queries >= TARGET_QUERIES
    return CorpusGate(
        name="size",
        passed=ok,
        detail=(
            f"{passages}/{TARGET_PASSAGES} passages, {queries}/{TARGET_QUERIES} queries"
            + ("" if ok else " — supplied material fully decomposed; needs more real content")
        ),
        blocked_on_external_input=not ok,
    )


def _subset_balance(dataset: Dataset) -> CorpusGate:
    """Every subset present and above the resolution floor.

    Balance rather than equality: some subsets are naturally harder to author than others,
    and forcing equal counts would mean padding. What is not acceptable is a subset thin
    enough that its score is noise, because that is the subset a candidate would be allowed
    to fail invisibly.
    """
    missing = [subset.value for subset in Subset if not dataset.queries_in(subset)]
    thin = {
        subset.value: len(dataset.queries_in(subset))
        for subset in Subset
        if 0 < len(dataset.queries_in(subset)) < MIN_QUERIES_PER_SUBSET
    }
    problems: list[str] = []
    if missing:
        problems.append(f"absent: {missing}")
    if thin:
        problems.append(f"below {MIN_QUERIES_PER_SUBSET}: {thin}")
    return CorpusGate(
        name="subset_balance",
        passed=not problems,
        detail="; ".join(problems) or f"all {len(Subset)} subsets at or above the floor",
    )


def _cross_script_genuine(dataset: Dataset) -> CorpusGate:
    """Cross-script queries must genuinely cross scripts, and there must be enough of them.

    A "cross-script" query whose gold shares its script tests nothing and inflates the
    hardest subset with easy cases — which is the one place inflation would flatter a
    candidate most, because cross-script is where the gate is already relaxed to 0.85.
    """
    queries = dataset.queries_in(Subset.CROSS_SCRIPT)
    lookup = dataset.by_id
    fake = [
        query.id
        for query in queries
        if not any(lookup[gold].script is not query.script for gold in query.gold_ids)
    ]
    enough = len(queries) >= MIN_CROSS_SCRIPT_QUERIES
    return CorpusGate(
        name="cross_script_genuine",
        passed=not fake and enough,
        detail=(
            f"{len(queries)}/{MIN_CROSS_SCRIPT_QUERIES} queries"
            + (f", same-script gold in {fake}" if fake else ", all genuinely cross-script")
        ),
    )


def _adversarial_present(dataset: Dataset) -> CorpusGate:
    """Each adversarial role models a distinct failure, so a missing one is a blind spot.

    A missing role is reported with **what would supply it**, because in every case the
    remedy is business content somebody has to hand over — not code somebody has to write.
    A bare "missing: ['stale']" reads like a bug and invites the wrong fix, which for `stale`
    would be to invent an old service description and thereby measure nothing.
    """
    present = {passage.role for passage in dataset.passages}
    missing = sorted(MIN_ADVERSARIAL_ROLES - present, key=lambda item: item.value)
    counts = {
        role.value: sum(1 for p in dataset.passages if p.role is role)
        for role in sorted(MIN_ADVERSARIAL_ROLES, key=lambda item: item.value)
    }
    return CorpusGate(
        name="adversarial_present",
        passed=not missing,
        detail=(
            "; ".join(f"{role.value} absent — {_ROLE_REMEDY[role]}" for role in missing)
            if missing
            else f"present: {counts}"
        ),
        # Blocked only when *every* missing role is one the business must hand over. A
        # missing `distractor` or `injection` would be a real defect — those are generated
        # from material already supplied — and must stay a bare FAIL.
        blocked_on_external_input=bool(missing) and set(missing) <= _EXTERNALLY_SUPPLIED_ROLES,
    )


def _no_numeric_prices(dataset: Dataset) -> CorpusGate:
    """**No passage anywhere may carry a money-shaped number.**

    Stronger than `no_pricing_as_rag_truth`, which permits priced passages so long as they
    are labelled traps and never gold. This one says the corpus contains no price to
    retrieve at all — which is the guarantee available when the business quotes customised
    pricing and supplies no figures, and it is the guarantee worth having:

    * A labelled trap tests that a *candidate* ranks it low. It still leaves a real number in
      a corpus that a future contributor could relabel, promote, or copy into a fixture.
    * No number at all means a pricing query cannot be answered with a figure from retrieval
      **by construction**. There is nothing to rank.

    The gate is therefore also a guard on the intake file: adding a price to `risenext.yaml`
    fails the build, and correctly so — a price belongs in `service_prices` behind
    `get_service_pricing`, never in a knowledge passage (PRD §6.5). If a tenant genuinely
    needs priced passages in its corpus, this gate is the conversation that has to happen
    first, rather than a number that quietly appears.
    """
    from rn_domain.sanitisation import looks_price_shaped

    priced = sorted(passage.id for passage in dataset.passages if looks_price_shaped(passage.text))
    return CorpusGate(
        name="no_numeric_prices_in_corpus",
        passed=not priced,
        detail=(
            f"{len(priced)} passages carry a money-shaped number: {priced[:5]}"
            if priced
            else "no passage carries a money-shaped number; a price cannot be retrieved at all"
        ),
    )


def _gold_source_types(query: Query, dataset: Dataset) -> set[str]:
    lookup = dataset.by_id
    return {
        lookup[gold].provenance.source_type or "unknown"
        for gold in query.gold_ids
        if gold in lookup
    }


def _intent_gold_gate(
    dataset: Dataset, *, intent: Intent, required_section: str, name: str, why: str
) -> CorpusGate:
    """Every query with `intent` must have gold from `required_section`.

    Checked through provenance rather than passage ids, so it holds for any tenant's intake
    file: the assertion is "a pricing question's answer comes from the pricing policy
    section", which is a statement about the *schema*, not about one company's content.
    """
    queries = [query for query in dataset.queries if query.intent == intent.value]
    wrong = sorted(
        query.id for query in queries if required_section not in _gold_source_types(query, dataset)
    )
    return CorpusGate(
        name=name,
        passed=bool(queries) and not wrong,
        detail=(
            f"no {intent.value} queries in the corpus — {why}"
            if not queries
            else f"{len(wrong)} {intent.value} queries lack {required_section} gold: {wrong[:5]}"
            if wrong
            else f"all {len(queries)} {intent.value} queries point at {required_section}"
        ),
    )


def _pricing_gold_is_policy(dataset: Dataset) -> CorpusGate:
    """The pricing benchmark.

    Every pricing question's correct answer must be the pricing **policy** — "quotations are
    customised" — and the corpus must contain such questions. Note what this can and cannot
    claim: D-8 has no model in the loop, so it measures whether the constraint is
    *retrievable*, not what an agent says. A model cannot decline to quote a figure it was
    never given a policy to decline from, so retrievability is the precondition; the
    behavioural test is an `agent_eval` case and is not this suite's job.
    """
    return _intent_gold_gate(
        dataset,
        intent=Intent.PRICING,
        required_section="pricing_policy",
        name="pricing_gold_is_policy",
        why="add pricing templates to the phrasebook, or the price-hallucination path is untested",
    )


def _lending_gold_is_disclaimer(dataset: Dataset) -> CorpusGate:
    """The loan benchmark.

    Rise Next assists with documentation, applications and bank coordination and **is not a
    lender**. That is the highest-priority business constraint in the supplied material, and
    the corpus must prove the disclaimer is reachable from a "do you give loans?" question in
    every language — including the ones nobody on the team can read.
    """
    return _intent_gold_gate(
        dataset,
        intent=Intent.LENDING,
        required_section="financing_disclaimer",
        name="lending_gold_is_disclaimer",
        why="add lending templates to the phrasebook; the lender disclaimer would go untested",
    )


def _adversarial_intents_present(dataset: Dataset) -> CorpusGate:
    """Every adversarial intent must appear, in at least one Indic subset.

    English-only adversarial coverage is the failure mode this catches, and it is an easy one
    to arrive at honestly: adversarial phrasings are the hardest to author in a language you
    do not speak, so they are the first thing an English-speaking author skips. A guardrail
    that holds only in English is not a guardrail — the caller who is told "we guarantee your
    loan" will have asked in Hindi or Telugu.
    """
    indic = {subset.value for subset in Subset if subset.needs_native_review}
    by_intent: dict[str, set[str]] = {}
    for query in dataset.queries:
        if query.intent:
            by_intent.setdefault(query.intent, set()).add(query.subset.value)

    adversarial = [intent for intent in Intent if intent.is_adversarial]
    absent = sorted(item.value for item in adversarial if item.value not in by_intent)
    english_only = sorted(
        item.value
        for item in adversarial
        if item.value in by_intent and not (by_intent[item.value] & indic)
    )
    problems: list[str] = []
    if absent:
        problems.append(f"absent entirely: {absent}")
    if english_only:
        problems.append(f"English-only: {english_only}")
    return CorpusGate(
        name="adversarial_intents_present",
        passed=not problems,
        detail="; ".join(problems)
        or f"all {len(adversarial)} adversarial intents present in an Indic subset",
    )


def _no_pricing_as_truth(dataset: Dataset) -> CorpusGate:
    """No price-shaped content may be gold, and none may be an ordinary passage.

    The most important gate in this module, and the one whose failure would be invisible in
    a score. PRD §6.5 makes the knowledge/authority split a correctness requirement: a
    price must come from `get_service_pricing`, never from a retrieved chunk. If a
    price-bearing passage is gold for any query, the benchmark is *rewarding* a candidate
    for treating retrieval as pricing authority — and whichever model wins would then be
    tuned toward exactly the behaviour the platform forbids.
    """
    from rn_domain.sanitisation import looks_price_shaped

    gold_ids = {gold for query in dataset.queries for gold in query.gold_ids}
    lookup = dataset.by_id

    priced_gold = sorted(
        passage_id for passage_id in gold_ids if looks_price_shaped(lookup[passage_id].text)
    )
    # A price-shaped passage not marked `price_bearing` would be retrievable as ordinary
    # knowledge, which is the same failure arriving by a different route.
    mislabelled = sorted(
        passage.id
        for passage in dataset.passages
        if looks_price_shaped(passage.text) and passage.role is not PassageRole.PRICE_BEARING
    )
    problems: list[str] = []
    if priced_gold:
        problems.append(f"price-shaped passages listed as gold: {priced_gold}")
    if mislabelled:
        problems.append(f"price-shaped but not role=price_bearing: {mislabelled}")
    return CorpusGate(
        name="no_pricing_as_rag_truth",
        passed=not problems,
        detail="; ".join(problems) or "no price-shaped content is gold or mislabelled",
    )


def _no_query_inflation(dataset: Dataset) -> CorpusGate:
    """No two queries in a subset may be near-duplicates.

    Compared within subsets only: the same question in Hindi and in Telugu is the whole
    point, and comparing across subsets would flag it. Typo variants are excluded — they are
    *deliberately* near-duplicates of their base query, and that is the axis they test.
    """
    offenders: list[str] = []
    for subset in dataset.subsets:
        queries = [
            query
            for query in dataset.queries_in(subset)
            # A typo variant shares its `derived_from` with its base and is meant to be
            # similar; comparing them would fail every corpus that tests noise robustness.
            if not _is_typo_variant(query.id)
        ]
        for left, right in itertools.combinations(queries, 2):
            if lexical_similarity(left.text, right.text) > NEAR_DUPLICATE_THRESHOLD:
                offenders.append(f"{left.id} ~ {right.id}")
    capped = offenders[:5]
    return CorpusGate(
        name="no_query_inflation",
        passed=not offenders,
        detail=(
            f"{len(offenders)} near-duplicate pairs, e.g. {capped}"
            if offenders
            else f"no pair above {NEAR_DUPLICATE_THRESHOLD:.2f} trigram overlap"
        ),
    )


def _is_typo_variant(query_id: str) -> bool:
    return query_id.endswith(("-transpose", "-double", "-drop", "-phonetic"))


def _no_passage_duplication(dataset: Dataset) -> CorpusGate:
    """No two passages may be near-identical.

    The counterpart to `no_query_inflation`, and it arrived with the capability
    decomposition for a concrete reason: splitting one service into fifteen passages is the
    cheapest way to make a corpus *look* larger, and 69 boilerplate passages differing by a
    noun would report as 69 while measuring 7.

    **Two duplicate passages break scoring, not just the count.** Retrieval would have two
    equally correct answers where the gold set names one, so a candidate returning the
    unnamed twin is marked wrong for being right — and which twin it prefers is arbitrary,
    so the metric becomes a coin flip that no amount of averaging removes.

    Adversarial passages are compared too, and must be: a `distractor` that has drifted into
    a near-copy of the passage it distracts from is not a hard negative, it is a second
    correct answer wearing a label that forbids it from scoring.
    """
    passages = list(dataset.passages)
    offenders: list[str] = []
    for left, right in itertools.combinations(passages, 2):
        if lexical_similarity(left.text, right.text) > NEAR_DUPLICATE_PASSAGE_THRESHOLD:
            offenders.append(f"{left.id} ~ {right.id}")
    return CorpusGate(
        name="no_passage_duplication",
        passed=not offenders,
        detail=(
            f"{len(offenders)} near-duplicate passage pairs, e.g. {offenders[:5]}"
            if offenders
            else f"no pair above {NEAR_DUPLICATE_PASSAGE_THRESHOLD:.2f} trigram overlap "
            f"({len(passages)} passages compared)"
        ),
    )


def _review_completeness(dataset: Dataset) -> CorpusGate:
    incomplete = [
        readiness.blocking_reason
        for readiness in (dataset.readiness(subset) for subset in dataset.subsets)
        if readiness.blocking_reason
    ]
    return CorpusGate(
        name="review_completeness",
        passed=not incomplete,
        detail=(
            f"{len(incomplete)} subsets awaiting review: "
            + ", ".join(sorted(r.split(":")[0] for r in incomplete))
            if incomplete
            else "every subset review-complete"
        ),
        # Only a competent speaker can close this, and no amount of code substitutes.
        blocked_on_external_input=bool(incomplete),
    )


def _spot_check(dataset: Dataset) -> CorpusGate:
    """A fraction of every subset must be reviewed individually, not by inheritance.

    The bound on template-level review. Without it, one reviewed template could vouch for a
    subset of any size, and the review effort would stop scaling with the corpus at all.
    """
    shortfalls: dict[str, str] = {}
    for subset in dataset.subsets:
        queries = dataset.queries_in(subset)
        if not queries:
            continue
        direct = sum(1 for query in queries if not query.review_inherited)
        needed = max(1, int(len(queries) * SPOT_CHECK_FRACTION))
        if direct < needed:
            shortfalls[subset.value] = f"{direct}/{needed}"
    return CorpusGate(
        name="spot_check",
        passed=not shortfalls,
        detail=(
            f"individually-reviewed shortfall: {shortfalls}"
            " — run `export-spot-checks` for the batch"
            if shortfalls
            else f"every subset meets the {SPOT_CHECK_FRACTION:.0%} individual-review floor"
        ),
        blocked_on_external_input=bool(shortfalls),
    )


def _tier_declared(dataset: Dataset) -> CorpusGate:
    """How much of the corpus is tier D, which can never support a decision.

    Advisory rather than blocking: tier-D material is legitimate for exercising machinery,
    and `review_completeness` already blocks on it. This gate exists so the proportion is
    *visible* — a corpus that is 90% tier D looks large and is not.
    """
    total = len(dataset.queries) or 1
    non_decision = sum(
        1 for query in dataset.queries if query.tier is MaterialTier.NON_DECISION_SYNTHETIC
    )
    share = non_decision / total
    return CorpusGate(
        name="tier_declared",
        passed=share == 0.0,
        detail=f"{non_decision}/{total} queries ({share:.0%}) are non-decision synthetic",
        blocking=False,
    )


def _chunking_policy(dataset: Dataset) -> CorpusGate:
    """The chunking policy must be the frozen one.

    Not a property of the dataset, but reported alongside it because a corpus measured under
    two chunkings is two incomparable corpora, and this is where someone reads the numbers.
    """
    return CorpusGate(
        name="chunking_policy_version",
        passed=FROZEN_CHUNKING_V1.version == "chunking-v1",
        detail=f"chunking policy is {FROZEN_CHUNKING_V1.version}",
    )


def render_gates(gates: Sequence[CorpusGate]) -> str:
    """A fixed-width block for the CLI."""
    lines = [f"  {gate.marker:5} {gate.name:28} {gate.detail}" for gate in gates]
    failing = [gate for gate in gates if gate.blocking and not gate.passed]
    blocked = [gate for gate in failing if gate.blocked_on_external_input]
    defects = [gate for gate in failing if not gate.blocked_on_external_input]
    lines.append("")
    if not failing:
        lines.append("  all blocking gates pass")
    else:
        lines.append(
            f"  {len(failing)} blocking gate(s) not passing: "
            f"{len(defects)} FAIL (fix in code), "
            f"{len(blocked)} BLOCK (awaiting human or business input)"
        )
    return "\n".join(lines)


def gate_summary(dataset: Dataset) -> Mapping[str, bool]:
    """`{gate name: passed}`, for a report artifact."""
    return {gate.name: gate.passed for gate in evaluate_corpus(dataset)}
