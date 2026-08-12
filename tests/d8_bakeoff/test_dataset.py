"""The dataset loader, its validation, and the review accounting behind DECISION 6.

The tests that matter most are the ones asserting the dataset is **not** review-complete
and therefore cannot support a decision. That is not a limitation being documented — it
is the mechanism. DECISION 6 permits building the benchmark before native-speaker review
and forbids claiming synthetic Hindi or Telugu is validated, and the only way to make
that stick is to compute it rather than remember it.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from tests.d8_bakeoff.dataset import (
    Dataset,
    MaterialTier,
    Passage,
    PassageRole,
    Query,
    Script,
    Subset,
    ValidationStatus,
    load_dataset,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset()


# ---------------------------------------------------------------------------
# The committed dataset
# ---------------------------------------------------------------------------


def test_the_committed_dataset_loads(dataset: Dataset) -> None:
    assert dataset.version >= 1
    assert dataset.passages
    assert dataset.queries


def test_every_reporting_subset_has_queries(dataset: Dataset) -> None:
    """All eight subsets must be exercised, or the per-subset reporting is untested and
    a whole language could be silently missing from a run."""
    assert set(dataset.subsets) == set(Subset)


def test_cross_script_queries_really_do_cross_scripts(dataset: Dataset) -> None:
    """The subset that matters most, asserted rather than assumed.

    A "cross-script" query whose gold passages are in its own script is not testing
    anything — and it would inflate the hardest subset's score with easy cases.
    """
    lookup = dataset.by_id
    for query in dataset.queries_in(Subset.CROSS_SCRIPT):
        golds = [lookup[identifier] for identifier in query.gold_ids]
        assert any(gold.script is not query.script for gold in golds), (
            f"{query.id} is in the cross-script subset but every gold passage shares its script"
        )


def test_the_corpus_contains_no_prices(dataset: Dataset) -> None:
    """PRD §6.5: prices are authoritative data served by `get_service_pricing`, never
    retrieved from a knowledge chunk.

    A benchmark corpus containing prices would reward a design this platform refuses to
    ship, and it would make the price-shaped ingestion flag fire on the evaluation set.

    This holds today because the supplied Rise Next material contains **no numeric prices**
    — the business quotes customised pricing — which buys a stronger guarantee than "prices
    are labelled as traps": there is no figure in the corpus to retrieve at all. If a tenant
    ever supplies real prices, this test and the `no_numeric_prices_in_corpus` gate are the
    conversation that has to happen first. Do not weaken either to make a build pass.
    """
    from rn_domain.sanitisation import looks_price_shaped

    offenders = [passage.id for passage in dataset.passages if looks_price_shaped(passage.text)]
    assert not offenders, f"price-shaped content in the knowledge corpus: {offenders}"


def test_instruction_shaped_content_is_confined_to_injection_passages(
    dataset: Dataset,
) -> None:
    """Instruction-shaped text belongs in the corpus — but only wearing the right label.

    Both directions are asserted, and each catches a different mistake:

    * An **ordinary** passage that is instruction-shaped would be quarantined out of
      retrieval in production (SECURITY §5.4), so a benchmark that scores candidates on
      retrieving it measures something the platform will never serve.
    * An **injection** passage that is *not* instruction-shaped is a negative that tests
      nothing: the whole point is that the detector flags it and a candidate must still not
      rank it. This half also makes the corpus a regression suite for the detector — if
      `rn_domain.sanitisation` stops flagging these payloads, this fails.
    """
    from rn_domain.sanitisation import looks_instruction_shaped

    mislabelled = [
        passage.id
        for passage in dataset.passages
        if looks_instruction_shaped(passage.text) and passage.role is not PassageRole.INJECTION
    ]
    assert not mislabelled, f"instruction-shaped content outside role=injection: {mislabelled}"

    toothless = [
        passage.id
        for passage in dataset.passages
        if passage.role is PassageRole.INJECTION and not looks_instruction_shaped(passage.text)
    ]
    assert not toothless, f"injection passages the detector does not flag: {toothless}"


# ---------------------------------------------------------------------------
# DECISION 6: review accounting
# ---------------------------------------------------------------------------


#: Subsets a competent speaker has signed off, and the review machinery has therefore
#: promoted. Grows as review batches land; every entry is a claim that a named human read
#: the text, so adding one without a corresponding approval in `phrasebook.yaml` is a lie the
#: rest of the suite will catch.
REVIEW_COMPLETE_SUBSETS = tuple(Subset)

#: The two templates the reviewer **corrected** rather than approving as written.
#:
#: Named because they are the rows where a review claim is easiest to get wrong. In both the
#: reviewer supplied the replacement wording as their approved form, so both are
#: `native_reviewed`. `ReviewDecision.resulting_state` maps a bare `needs_edit` to `pending`,
#: which is right for a correction nobody has since signed off — and would be wrong here,
#: recording a review as absent when a named human had given one.
CORRECTED_AND_APPROVED = ("hi-deva-industries", "xs-deva-out-of-scope")


def test_the_dataset_is_review_complete(dataset: Dataset) -> None:
    """Review closed on 2026-08-11: every subset, every template, every service-name set.

    A query needs both its template *and* its service names, because review propagates as the
    **weakest** input — approving the hi-deva templates alone had left 21 of its 49 queries
    unreviewed. All eight subsets now clear both halves.

    This asserts the *positive* direction, which is the one that was never exercised on the
    real corpus before: for most of this project's life the honest assertion was "not
    complete", and a readiness computation that only ever says "not ready" would have passed
    every test while being useless.
    """
    assert dataset.is_review_complete
    assert dataset.review_complete_subsets == REVIEW_COMPLETE_SUBSETS
    assert len(dataset.review_complete_subsets) == 8


def test_review_completeness_does_not_make_the_corpus_benchmark_ready(
    dataset: Dataset,
) -> None:
    """The distinction that stops a green review being read as a green benchmark.

    Review completeness answers "is the Indic text real", and it is now yes. It says nothing
    about whether the corpus is large enough or adversarial enough to carry a model choice,
    and two gates still say no. Without this test, `is_review_complete` flipping to true is
    exactly the moment someone concludes D-8 is finished.
    """
    from tests.d8_bakeoff.quality import corpus_is_benchmark_ready, evaluate_corpus

    assert not corpus_is_benchmark_ready(dataset)
    blocking = {gate.name for gate in evaluate_corpus(dataset) if gate.blocking and not gate.passed}
    assert blocking == {"size", "adversarial_present"}, blocking


def test_every_template_is_reviewed_and_attributed(dataset: Dataset) -> None:
    """No template is left behind, and none claims review without a reviewer.

    Walks the intake file rather than the generated queries: a template that stayed `pending`
    while its queries somehow became `native_reviewed` is the exact laundering bug
    `test_no_pending_input_is_propagated_as_native_reviewed` guards, and this is the other
    end of it.
    """
    from tests.d8_bakeoff.corpus.source_material import ReviewState, load_phrasebook

    book = load_phrasebook()
    unreviewed = sorted(
        item.id for item in book.templates if item.review_status is not ReviewState.NATIVE_REVIEWED
    )
    assert unreviewed == [], unreviewed
    assert len(book.templates) == 101

    for template in book.templates:
        assert template.reviewed_by, template.id
        assert template.reviewed_on, template.id

    names_unreviewed = sorted(
        item.service_id
        for item in book.service_names
        if item.review_status is not ReviewState.NATIVE_REVIEWED
    )
    assert names_unreviewed == [], names_unreviewed

    by_id = {item.id: item for item in book.templates}
    for template_id in CORRECTED_AND_APPROVED:
        assert by_id[template_id].review_status is ReviewState.NATIVE_REVIEWED
        assert by_id[template_id].reviewed_by == "Rise Next team"


def test_the_corrected_cross_script_template_carries_the_approved_wording(
    dataset: Dataset,
) -> None:
    """Pin the exact approved Devanagari string, codepoint for codepoint.

    The wording arrived through a review message rather than through the file, so nothing
    else in the suite would notice it drifting — and the failure mode is silent: a corpus
    that still builds, still passes every gate, and asks a question the reviewer never
    approved. The comma is load-bearing here only in the sense that it is *what was
    approved*; asserting the whole string is what makes that checkable.
    """
    import unicodedata

    from tests.d8_bakeoff.corpus.source_material import load_phrasebook

    approved = "जो सर्विस आपकी लिस्ट में नहीं है, वो भी करते हैं क्या"
    template = next(
        item for item in load_phrasebook().templates if item.id == "xs-deva-out-of-scope"
    )
    assert template.text == approved
    assert unicodedata.normalize("NFC", template.text) == template.text
    assert template.subset == "cross-script"
    assert template.intent.value == "out_of_scope"

    generated = [query for query in dataset.queries if query.derived_from == "xs-deva-out-of-scope"]
    assert generated
    assert all(query.validation is ValidationStatus.NATIVE_REVIEWED for query in generated)


def test_an_unreviewed_indic_query_still_reports_a_named_blocking_reason() -> None:
    """The negative direction, kept alive after the real corpus stopped exercising it.

    Until 2026-08-11 this ran against the committed corpus. Now every subset is
    review-complete, so a loop over the real data would pass by never entering its body —
    a test that deletes itself the moment the thing it guards starts mattering. It is built
    from a fixture instead, so the accounting is still checked in both directions.

    The reason must **name its subset**: `evaluate_corpus` splits on that string to report
    which subsets are outstanding, so a reason that omits it produces a gate detail that says
    something is wrong without saying where.
    """
    dataset = Dataset(
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
    readiness = dataset.readiness(Subset.HI_DEVANAGARI)
    assert not readiness.is_review_complete
    assert readiness.unreviewed_queries == ("q1",)
    assert readiness.blocking_reason
    assert Subset.HI_DEVANAGARI.value in readiness.blocking_reason
    assert not dataset.is_review_complete


def test_a_review_complete_subset_reports_no_blocking_reason(dataset: Dataset) -> None:
    """The other direction, so the accounting cannot be one-way.

    A readiness computation that only ever says "not ready" would pass every test above
    while being useless: the point of the mechanism is that it *releases* a subset once the
    review genuinely lands.
    """
    for subset in REVIEW_COMPLETE_SUBSETS:
        readiness = dataset.readiness(subset)
        assert readiness.is_review_complete
        assert readiness.blocking_reason is None
        assert readiness.unreviewed_queries == ()
        assert readiness.unreviewed_passages == ()


def test_a_review_complete_subset_names_its_reviewer_on_every_query(
    dataset: Dataset,
) -> None:
    """`native_reviewed` without a named reviewer is refused at load, so this asserts the
    stronger property: the attribution actually propagated from the phrasebook rather than
    the queries having reached that status some other way."""
    for subset in REVIEW_COMPLETE_SUBSETS:
        queries = dataset.queries_in(subset)
        assert queries
        for query in queries:
            assert query.validation is ValidationStatus.NATIVE_REVIEWED, query.id
            assert query.reviewed_by, query.id
            assert not query.provenance.human_review_required, query.id


def test_no_pending_input_is_propagated_as_native_reviewed(dataset: Dataset) -> None:
    """The bug that would quietly launder unreviewed text into decision evidence.

    A generated query is only as reviewed as its **weakest** input. Taking the maximum
    instead of the minimum is a one-character mistake that would let a reviewed English
    template plus an unreviewed Devanagari service name produce a "reviewed" Hindi query —
    and nothing downstream would notice, because the row would look identical to a genuine
    one. This walks back from every `native_reviewed` query to the phrasebook entries that
    produced it and insists they were all approved too.

    Individually spot-checked queries are exempt by construction: a human read the finished
    text, which outranks anything its inputs did or did not have.
    """
    from tests.d8_bakeoff.corpus.source_material import ReviewState, load_phrasebook

    book = load_phrasebook()
    templates = {template.id: template for template in book.templates}

    for query in dataset.queries:
        if query.validation is not ValidationStatus.NATIVE_REVIEWED:
            continue
        if not query.review_inherited:
            continue  # judged individually; see `SpotCheck`
        template = templates.get(query.derived_from or "")
        assert template is not None, query.id
        assert template.review_status is ReviewState.NATIVE_REVIEWED, (
            f"{query.id} claims native review but its template {template.id} is "
            f"{template.review_status.value}"
        )
        if not template.intent.is_service_scoped:
            continue
        unreviewed = [
            names.service_id
            for names in book.service_names
            if names.review_status is not ReviewState.NATIVE_REVIEWED
        ]
        assert not unreviewed, (
            f"{query.id} is service-scoped and claims native review, but these service "
            f"names are not reviewed: {unreviewed}"
        )


def test_every_capability_passage_names_a_real_supplied_capability(dataset: Dataset) -> None:
    """No invented business capability can reach the corpus.

    The capability atoms are the bulk of the passages, and they are the easiest place for a
    plausible-sounding sub-service to appear that the business never offered. Every id is
    checked back against `risenext.yaml`.
    """
    from tests.d8_bakeoff.corpus.source_material import load_source_material

    supplied = {capability.id for capability in load_source_material().capabilities}
    generated = {
        passage.id
        for passage in dataset.passages
        if passage.provenance.generated_from == "capability_atom"
    }
    assert generated, "expected capability atoms in the corpus"
    assert generated <= supplied, f"capabilities not in the source: {sorted(generated - supplied)}"


def test_adversarial_passages_stay_clearly_synthetic(dataset: Dataset) -> None:
    """Deliberately-wrong content must never be mistakable for business fact.

    Every adversarial passage is ineligible for gold and carries the adversarial tier, so no
    query can score against one and no reader can take it for something Rise Next said.
    """
    adversarial = [p for p in dataset.passages if p.tier is MaterialTier.ADVERSARIAL]
    assert adversarial
    gold = {g for query in dataset.queries for g in query.gold_ids}
    for passage in adversarial:
        assert not passage.role.may_be_gold, passage.id
        assert passage.id not in gold, passage.id


def test_source_grounded_passages_all_name_a_source(dataset: Dataset) -> None:
    """The specific mislabelling that would let invented content be quoted as fact.

    Enforced at construction too, but asserted here against the *committed* corpus so a
    hand-edited data file cannot slip past the constructor.
    """
    grounded = [p for p in dataset.passages if p.tier is MaterialTier.SOURCE_GROUNDED]
    assert grounded
    for passage in grounded:
        assert passage.source_reference, passage.id
        assert passage.provenance.source_id, passage.id
        assert passage.provenance.source_version is not None, passage.id


def test_english_does_not_need_native_review_but_other_subsets_do() -> None:
    assert not Subset.EN.needs_native_review
    for subset in Subset:
        if subset is not Subset.EN:
            assert subset.needs_native_review


def test_a_reviewed_english_subset_becomes_review_complete() -> None:
    """The positive control for the readiness computation.

    Without this, "nothing is review-complete" could be true because the computation is
    broken rather than because the data is unreviewed — and the distinction is the whole
    point of the mechanism.
    """
    dataset = Dataset(
        version=1,
        passages=(
            Passage(
                id="p1",
                text="We build websites.",
                language="en",
                script=Script.LATIN,
                validation=ValidationStatus.SOURCE_MATERIAL,
                tier=MaterialTier.SOURCE_GROUNDED,
                source_reference="test fixture",
            ),
        ),
        queries=(
            Query(
                id="q1",
                text="do you build websites",
                subset=Subset.EN,
                script=Script.LATIN,
                validation=ValidationStatus.SOURCE_MATERIAL,
                tier=MaterialTier.SOURCE_GROUNDED,
                relevant={"p1": 2},
            ),
        ),
    )
    assert dataset.readiness(Subset.EN).is_review_complete
    assert dataset.is_review_complete


def test_an_unreviewed_indic_subset_is_blocked_even_with_reviewed_queries() -> None:
    """Both halves matter: a reviewed query pointing at unreviewed synthetic Hindi is
    still a claim about Hindi built on unreviewed Hindi."""
    dataset = Dataset(
        version=1,
        passages=(
            Passage(
                id="p1",
                text="हम वेबसाइट बनाते हैं।",
                language="hi",
                script=Script.DEVANAGARI,
                validation=ValidationStatus.SYNTHETIC_UNREVIEWED,
                tier=MaterialTier.SOURCE_GROUNDED,
                source_reference="test fixture",
            ),
        ),
        queries=(
            Query(
                id="q1",
                text="वेबसाइट",
                subset=Subset.HI_DEVANAGARI,
                script=Script.DEVANAGARI,
                validation=ValidationStatus.NATIVE_REVIEWED,
                reviewed_by="a reviewer",
                tier=MaterialTier.SOURCE_GROUNDED,
                relevant={"p1": 2},
            ),
        ),
    )
    readiness = dataset.readiness(Subset.HI_DEVANAGARI)
    assert not readiness.is_review_complete
    assert readiness.unreviewed_passages == ("p1",)
    assert readiness.unreviewed_queries == ()


def test_english_source_material_is_acceptable_gold_for_a_cross_script_subset() -> None:
    """A cross-script query legitimately points at an English passage, and nobody
    translated it, so there is nothing to review about it."""
    dataset = Dataset(
        version=1,
        passages=(
            Passage(
                id="p1",
                text="Our offices are in Hyderabad.",
                language="en",
                script=Script.LATIN,
                validation=ValidationStatus.SOURCE_MATERIAL,
                tier=MaterialTier.SOURCE_GROUNDED,
                source_reference="test fixture",
            ),
        ),
        queries=(
            Query(
                id="q1",
                text="कार्यालय",
                subset=Subset.CROSS_SCRIPT,
                script=Script.DEVANAGARI,
                validation=ValidationStatus.NATIVE_REVIEWED,
                reviewed_by="a reviewer",
                tier=MaterialTier.SOURCE_GROUNDED,
                relevant={"p1": 2},
            ),
        ),
    )
    assert dataset.readiness(Subset.CROSS_SCRIPT).is_review_complete


def test_tier_d_material_can_never_become_review_complete() -> None:
    """The tier gate, and the reason it is checked *before* the status check.

    A `native_reviewed` stamp on invented material means only "this invented sentence is
    fluent" — there is no real fact for a reviewer to vouch for. If the status check came
    first, a review pass would launder tier D into decision evidence, which is precisely
    what tier D exists to prevent.
    """
    dataset = Dataset(
        version=1,
        passages=(
            Passage(
                id="p1",
                text="We do something nobody supplied.",
                language="en",
                script=Script.LATIN,
                validation=ValidationStatus.NATIVE_REVIEWED,
                reviewed_by="a real named reviewer",
                tier=MaterialTier.NON_DECISION_SYNTHETIC,
            ),
        ),
        queries=(
            Query(
                id="q1",
                text="what do you do",
                subset=Subset.EN,
                script=Script.LATIN,
                validation=ValidationStatus.NATIVE_REVIEWED,
                reviewed_by="a real named reviewer",
                tier=MaterialTier.NON_DECISION_SYNTHETIC,
                relevant={"p1": 2},
            ),
        ),
    )
    assert not dataset.readiness(Subset.EN).is_review_complete
    assert not dataset.is_review_complete


@pytest.mark.parametrize(
    "role",
    [
        PassageRole.DISTRACTOR.value,
        PassageRole.STALE.value,
        PassageRole.INJECTION.value,
        PassageRole.PRICE_BEARING.value,
    ],
)
def test_a_non_gold_eligible_passage_cannot_be_gold(role: str, tmp_path: pathlib.Path) -> None:
    """A hard negative as gold inverts the test.

    The candidate would be *rewarded* for retrieving exactly the thing it must not retrieve
    — and for `price_bearing` that is worse than a scoring bug: whichever model won would
    have been selected for treating retrieval as pricing authority, which PRD §6.5 forbids.
    """
    _write(
        tmp_path,
        f"""
        dataset_version: 1
        passages:
          - id: p1
            language: en
            script: latin
            validation: source_material
            role: {role}
            text: A plausible but wrong answer.
        """,
        """
        dataset_version: 1
        queries:
          - id: q1
            text: something
            subset: en
            script: latin
            validation: source_material
            relevant:
              p1: 2
        """,
    )
    with pytest.raises(ValueError, match="non-gold-eligible"):
        load_dataset(tmp_path)


def test_claiming_review_without_naming_a_reviewer_is_refused() -> None:
    """Otherwise "reviewed" is a word someone typed, and there is nobody to ask when a
    judgement is disputed."""
    with pytest.raises(ValueError, match="names no reviewer"):
        Passage(
            id="p1",
            text="x",
            language="hi",
            script=Script.DEVANAGARI,
            validation=ValidationStatus.NATIVE_REVIEWED,
        )
    with pytest.raises(ValueError, match="names no reviewer"):
        Query(
            id="q1",
            text="x",
            subset=Subset.EN,
            script=Script.LATIN,
            validation=ValidationStatus.NATIVE_REVIEWED,
            relevant={"p1": 1},
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_a_query_with_no_gold_set_is_refused() -> None:
    """It would score 0 for every candidate, dragging every average down identically and
    looking like a model problem."""
    with pytest.raises(ValueError, match="no relevant passages"):
        Query(
            id="q1",
            text="x",
            subset=Subset.EN,
            script=Script.LATIN,
            validation=ValidationStatus.SOURCE_MATERIAL,
            relevant={},
        )


@pytest.mark.parametrize("grade", [0, 3, -1])
def test_an_out_of_range_grade_is_refused(grade: int) -> None:
    with pytest.raises(ValueError, match="grades run"):
        Query(
            id="q1",
            text="x",
            subset=Subset.EN,
            script=Script.LATIN,
            validation=ValidationStatus.SOURCE_MATERIAL,
            relevant={"p1": grade},
        )


def _write(directory: pathlib.Path, corpus: str, queries: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "corpus.yaml").write_text(textwrap.dedent(corpus), encoding="utf-8")
    (directory / "queries.yaml").write_text(textwrap.dedent(queries), encoding="utf-8")


_MINIMAL_CORPUS = """
    dataset_version: 1
    passages:
      - id: p1
        language: en
        script: latin
        validation: source_material
        text: We build websites.
"""


def test_a_dangling_gold_reference_is_refused(tmp_path: pathlib.Path) -> None:
    """An unreachable gold id makes a query unscorable — and it looks exactly like a
    retrieval failure, which is the worst way for a dataset bug to present."""
    _write(
        tmp_path,
        _MINIMAL_CORPUS,
        """
        dataset_version: 1
        queries:
          - id: q1
            text: websites
            subset: en
            script: latin
            validation: source_material
            relevant:
              does-not-exist: 2
        """,
    )
    with pytest.raises(ValueError, match="unknown passages"):
        load_dataset(tmp_path)


def test_mismatched_dataset_versions_are_refused(tmp_path: pathlib.Path) -> None:
    """A query set must be scored against the corpus it was written for."""
    _write(
        tmp_path,
        _MINIMAL_CORPUS,
        """
        dataset_version: 2
        queries:
          - id: q1
            text: websites
            subset: en
            script: latin
            validation: source_material
            relevant:
              p1: 2
        """,
    )
    with pytest.raises(ValueError, match="version"):
        load_dataset(tmp_path)


def test_duplicate_passage_ids_are_refused(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        dataset_version: 1
        passages:
          - id: p1
            language: en
            script: latin
            validation: source_material
            text: One.
          - id: p1
            language: en
            script: latin
            validation: source_material
            text: Two.
        """,
        """
        dataset_version: 1
        queries:
          - id: q1
            text: websites
            subset: en
            script: latin
            validation: source_material
            relevant:
              p1: 2
        """,
    )
    with pytest.raises(ValueError, match="Duplicate passage"):
        load_dataset(tmp_path)


def test_a_missing_file_is_a_clear_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        load_dataset(tmp_path)
