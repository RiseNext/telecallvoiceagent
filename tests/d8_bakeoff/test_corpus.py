"""Corpus generation: intake loading, variants, hard negatives, and the build.

The intake files are absent in this repository — nobody has supplied RiseNext content yet —
so these tests build minimal *fixture* intake files in `tmp_path`. That is deliberate and it
is the honest way to test the generator: a fixture proves the transform works, and it cannot
be mistaken for real content because it never touches `source/`.

The assertions worth reading are the refusals. A corpus generator that accepts a
half-filled intake file, or mislabels an invented fact as source-grounded, or lets a price
become gold, produces a benchmark that measures the wrong thing while looking fine.
"""

from __future__ import annotations

import pathlib
import re
import textwrap

import pytest

from rn_domain.sanitisation import looks_instruction_shaped, looks_price_shaped
from tests.d8_bakeoff.corpus.build import build_dataset, write_dataset
from tests.d8_bakeoff.corpus.negatives import NegativeKind, generate_negatives
from tests.d8_bakeoff.corpus.source_material import (
    FILL_SENTINEL,
    Authority,
    Intent,
    ReviewState,
    load_phrasebook,
    load_source_material,
    load_spot_checks,
)
from tests.d8_bakeoff.corpus.variants import (
    MAX_TYPO_EDITS,
    is_latin_script,
    typo_variants,
)
from tests.d8_bakeoff.dataset import MaterialTier, PassageRole, Subset, load_dataset

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fixture intake files
# ---------------------------------------------------------------------------

_SOURCE = """
    source_material_version: 1
    provenance:
      supplied_by: fixture team
      supplied_on: 2026-07-30
      notes: none
    company:
      name: Fixture Software
      description: We build software for Indian businesses.
      additional:
        - id: about-process
          title: How a project runs
          text: A discovery call, then a written proposal, then two-week cycles.
          authority: descriptive
          never_rag: false
          source_reference: fixture brochure p1
          review_status: reviewed
    services:
      - id: web
        name: Website development
        description: We build business websites and online storefronts.
        capabilities:
          - design
          - build
          - launch
        technologies:
          - typescript
        common_questions:
          - how long does it take
        near_duplicate_of: app
        authority: descriptive
        never_rag: false
        source_reference: fixture brochure p2
        review_status: reviewed
      - id: app
        name: Mobile app development
        description: We build Android and iOS applications.
        capabilities:
          - cross-platform build
          - store submission
        technologies: []
        common_questions:
          - which platforms
        authority: descriptive
        never_rag: false
        source_reference: fixture brochure p3
        review_status: reviewed
    faqs:
      - id: faq-apps
        question: Do you create mobile applications?
        answer: Yes. We build Android and iOS applications for Indian businesses.
        service_id: app
        authority: descriptive
        never_rag: false
        source_reference: fixture brochure p4
        review_status: reviewed
    pricing_policy:
      - id: policy-pricing
        title: How pricing works
        text: Every project is quoted individually once we understand the scope.
        authority: policy
        never_rag: false
        source_reference: fixture brochure p5
        review_status: reviewed
    financing_disclaimer:
      - id: policy-not-a-lender
        title: Not a lender
        text: We assist with paperwork and bank coordination. We do not lend money.
        authority: policy
        never_rag: false
        source_reference: fixture brochure p6
        review_status: reviewed
    policies:
      - id: policy-never-promise
        title: What we never promise
        text: We never promise guaranteed approvals, rankings, or fixed dates before scope.
        kind: never_promise
        authority: policy
        never_rag: false
        source_reference: fixture brochure p7
        review_status: reviewed
      - id: policy-never-ranking
        title: We never promise a ranking
        text: We never promise a guaranteed first page ranking on any search engine.
        kind: never_promise_item
        topic: google_ranking
        authority: policy
        never_rag: false
        source_reference: fixture brochure p7
        review_status: reviewed
      - id: policy-never-revisions
        title: We never promise unlimited revisions
        text: We never promise unlimited revisions on any deliverable.
        kind: never_promise_item
        topic: unlimited_revisions
        authority: policy
        never_rag: false
        source_reference: fixture brochure p7
        review_status: reviewed
    lead_requirements:
      - id: lead-fields
        title: Lead fields
        text: Full name, company name, mobile number, email address.
        authority: structural
        never_rag: true
        source_reference: fixture CRM sheet
        review_status: reviewed
    authoritative_values:
      - id: price-web
        service_id: web
        kind: price
        text: Website development starts at Rs. 49,999 per project.
        authority: authoritative
        never_rag: true
        source_reference: fixture rate card
        review_status: reviewed
    superseded:
      - id: old-web
        supersedes_current: web
        text: Website development used to take twelve weeks and cost more.
        source_reference: fixture brochure v1
        review_status: reviewed
"""

_PHRASEBOOK = """
    phrasebook_version: 1
    provenance:
      supplied_by: fixture reviewer
      supplied_on: 2026-07-30
      notes: none
    service_names:
      - service_id: web
        latin: website development
        devanagari: वेबसाइट डेवलपमेंट
        telugu: వెబ్‌సైట్ డెవలప్‌మెంట్
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - service_id: app
        latin: mobile app development
        devanagari: मोबाइल ऐप डेवलपमेंट
        telugu: మొబైల్ యాప్ డెవలప్‌మెంట్
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
    templates:
      - id: en-what-is
        subset: en
        intent: what_is
        style: canonical
        text: "what is {service}"
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - id: en-pricing
        subset: en
        intent: pricing
        style: canonical
        text: "how much does {service} cost"
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - id: en-lending
        subset: en
        intent: lending
        style: canonical
        text: "do you give loans"
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - id: en-guarantee-ranking
        subset: en
        intent: guarantees
        style: conversational
        text: "can you guarantee first page ranking"
        topic: google_ranking
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - id: en-guarantee-generic
        subset: en
        intent: guarantees
        style: terse
        text: "any guarantees at all"
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - id: en-cap-atom
        subset: en
        intent: capability_specific
        style: canonical
        text: "do you provide {capability}"
        review_status: native_reviewed
        reviewed_by: fixture reviewer
        reviewed_on: 2026-07-30
      - id: hi-deva-what-is
        subset: hi-deva
        intent: what_is
        style: canonical
        text: "{service} क्या है"
        review_status: pending
      - id: xs-company
        subset: cross-script
        intent: company
        style: canonical
        text: "आप क्या करते हैं"
        review_status: pending
"""


def _write_intake(
    directory: pathlib.Path, *, source: str = _SOURCE, book: str = _PHRASEBOOK
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "risenext.yaml").write_text(textwrap.dedent(source), encoding="utf-8")
    (directory / "phrasebook.yaml").write_text(textwrap.dedent(book), encoding="utf-8")


@pytest.fixture
def intake(tmp_path: pathlib.Path) -> pathlib.Path:
    _write_intake(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Intake loading and its refusals
# ---------------------------------------------------------------------------


def test_the_intake_files_load(intake: pathlib.Path) -> None:
    material = load_source_material(intake / "risenext.yaml")
    assert material.company_name == "Fixture Software"
    assert {service.id for service in material.services} == {"web", "app"}
    assert material.supplied_by == "fixture team"

    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    assert len(phrasebook.templates) == 8
    assert phrasebook.names_for("web") is not None


def test_a_missing_intake_file_says_what_to_do(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError, match="template"):
        load_source_material(tmp_path / "risenext.yaml")


def test_an_unfilled_template_is_refused(tmp_path: pathlib.Path) -> None:
    """The likely first state of a copied template, refused so it cannot become the corpus."""
    _write_intake(tmp_path, source=_SOURCE.replace("Fixture Software", FILL_SENTINEL))
    with pytest.raises(ValueError, match=re.escape(FILL_SENTINEL)):
        load_source_material(tmp_path / "risenext.yaml")


def test_authoritative_without_never_rag_is_refused(tmp_path: pathlib.Path) -> None:
    """The single most consequential mislabelling the schema can catch.

    An authoritative price that stays RAG-eligible retrieves confidently and gets quoted to a
    caller as a commitment the business never made (PRD §6.5).
    """
    broken = _SOURCE.replace(
        "        authority: authoritative\n        never_rag: true",
        "        authority: authoritative\n        never_rag: false",
    )
    _write_intake(tmp_path, source=broken)
    with pytest.raises(ValueError, match="never_rag"):
        load_source_material(tmp_path / "risenext.yaml")


def test_a_service_scoped_template_without_a_slot_is_refused(tmp_path: pathlib.Path) -> None:
    """Otherwise it generates the same query for every service."""
    broken = _PHRASEBOOK.replace('text: "what is {service}"', 'text: "what is it"')
    _write_intake(tmp_path, book=broken)
    with pytest.raises(ValueError, match=re.escape("no {service} slot")):
        load_phrasebook(tmp_path / "phrasebook.yaml")


def test_a_business_wide_template_with_a_slot_is_refused(tmp_path: pathlib.Path) -> None:
    broken = _PHRASEBOOK.replace('text: "do you give loans"', 'text: "do you fund {service}"')
    _write_intake(tmp_path, book=broken)
    with pytest.raises(ValueError, match="business as a whole"):
        load_phrasebook(tmp_path / "phrasebook.yaml")


def test_an_unknown_near_duplicate_is_refused(tmp_path: pathlib.Path) -> None:
    _write_intake(
        tmp_path, source=_SOURCE.replace("near_duplicate_of: app", "near_duplicate_of: ghost")
    )
    with pytest.raises(ValueError, match="not a known service"):
        load_source_material(tmp_path / "risenext.yaml")


def test_a_review_claim_with_no_reviewer_is_refused(tmp_path: pathlib.Path) -> None:
    broken = _PHRASEBOOK.replace(
        "        review_status: native_reviewed\n        reviewed_by: fixture reviewer\n        reviewed_on: 2026-07-30\n    templates:",
        "        review_status: native_reviewed\n    templates:",
    )
    _write_intake(tmp_path, book=broken)
    with pytest.raises(ValueError, match="nobody"):
        load_phrasebook(tmp_path / "phrasebook.yaml")


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def test_typo_variants_are_deterministic() -> None:
    text = "how long does website development take"
    assert typo_variants(text) == typo_variants(text)


def test_typo_variants_are_bounded_and_actually_differ() -> None:
    produced = typo_variants("how long does website development take", limit=5)
    assert 0 < len(produced) <= MAX_TYPO_EDITS
    for variant in produced:
        assert variant.text != "how long does website development take"


def test_typo_variants_are_not_generated_for_indic_script() -> None:
    """Dropping or swapping a codepoint in Devanagari can detach a matra from its consonant
    and produce something no speaker would utter. Judging that needs a speaker, not a table.
    """
    assert typo_variants("वेबसाइट डेवलपमेंट में कितना समय लगता है") == ()
    assert typo_variants("వెబ్‌సైట్ డెవలప్‌మెంట్ ఎంత సమయం పడుతుంది") == ()


def test_typo_variants_skip_text_that_is_too_short() -> None:
    assert typo_variants("hi") == ()


@pytest.mark.parametrize(
    ("text", "latin"),
    [
        ("website development", True),
        ("website banane mein kitna time", True),
        ("वेबसाइट डेवलपमेंट", False),
        ("వెబ్‌సైట్", False),
        ("12345", False),
    ],
)
def test_script_detection(text: str, latin: bool) -> None:
    assert is_latin_script(text) is latin


# ---------------------------------------------------------------------------
# Hard negatives
# ---------------------------------------------------------------------------


def test_every_negative_kind_is_generated(intake: pathlib.Path) -> None:
    material = load_source_material(intake / "risenext.yaml")
    negatives = generate_negatives(material)
    kinds = {passage.negative_kind for passage in negatives}
    assert NegativeKind.SIMILAR_SERVICE in kinds
    assert NegativeKind.WRONG_CAPABILITY in kinds
    assert NegativeKind.STALE in kinds
    assert NegativeKind.INSTRUCTION_SHAPED in kinds
    assert NegativeKind.PRICE_BEARING in kinds


def test_no_negative_is_gold_eligible(intake: pathlib.Path) -> None:
    """The invariant the loader also enforces, asserted at the source of the passages."""
    material = load_source_material(intake / "risenext.yaml")
    for passage in generate_negatives(material):
        assert not passage.role.may_be_gold, passage.id
        assert passage.tier is MaterialTier.ADVERSARIAL


def test_injection_negatives_are_detected_by_the_ingestion_flagger(
    intake: pathlib.Path,
) -> None:
    """Makes the corpus a regression suite for the detector as well as an input to the
    benchmark: if `rn_domain.sanitisation` stops flagging these, this fails."""
    material = load_source_material(intake / "risenext.yaml")
    injections = [
        passage for passage in generate_negatives(material) if passage.role is PassageRole.INJECTION
    ]
    assert injections
    for passage in injections:
        assert looks_instruction_shaped(passage.text), passage.id


def test_price_negatives_are_detected_as_price_shaped(intake: pathlib.Path) -> None:
    material = load_source_material(intake / "risenext.yaml")
    priced = [
        passage
        for passage in generate_negatives(material)
        if passage.role is PassageRole.PRICE_BEARING
    ]
    assert priced
    for passage in priced:
        assert looks_price_shaped(passage.text), passage.id


def test_a_similar_service_negative_carries_the_wrong_description(
    intake: pathlib.Path,
) -> None:
    """The hard part: on-topic text, overlapping vocabulary, wrong subject."""
    material = load_source_material(intake / "risenext.yaml")
    similar = next(
        passage
        for passage in generate_negatives(material)
        if passage.negative_kind is NegativeKind.SIMILAR_SERVICE
    )
    assert "Website development" in similar.text
    assert "Android and iOS" in similar.text


def test_no_negatives_without_services() -> None:
    from tests.d8_bakeoff.corpus.source_material import SourceMaterial

    empty = SourceMaterial(
        version=1,
        supplied_by="x",
        supplied_on="2026-07-30",
        company_name="X",
        company_description="Y",
        services=(),
        facts=(),
    )
    assert generate_negatives(empty) == ()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def test_the_build_multiplies_templates_across_services(intake: pathlib.Path) -> None:
    """The whole point of the phrasebook: a few reviewed inputs become many queries."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    # `en-what-is` is service-scoped and fans out over two services; `en-support` and
    # `xs-company` are business-wide and do not.
    assert result.query_count > len(phrasebook.templates)
    assert result.passage_count > len(material.services)


def test_generated_queries_record_their_template_and_inheritance(
    intake: pathlib.Path,
) -> None:
    """`derived_from` + `review_inherited` are what make template-level review auditable
    rather than a claim."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)
    for row in result.queries:
        assert row["derived_from"]
        assert row["review_inherited"] is True
        assert row["tier"] == MaterialTier.CONTROLLED_SYNTHETIC.value


def test_review_propagates_as_the_weakest_input(intake: pathlib.Path) -> None:
    """A reviewed template plus an unreviewed one must not both yield reviewed queries.

    Taking the maximum instead of the minimum is the bug that would let a reviewed English
    template plus an unreviewed Hindi service name produce a "reviewed" Hindi query.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    by_template = {row["derived_from"]: row for row in result.queries}
    # `en-what-is` is native_reviewed and its service names are too.
    assert by_template["en-what-is"]["validation"] == "native_reviewed"
    # `hi-deva-what-is` is pending, so nothing derived from it may claim review.
    assert by_template["hi-deva-what-is"]["validation"] == "synthetic_unreviewed"


def test_the_build_is_reproducible(intake: pathlib.Path) -> None:
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    first = build_dataset(material, phrasebook)
    second = build_dataset(material, phrasebook)
    assert first.passages == second.passages
    assert first.queries == second.queries


def test_a_price_bearing_passage_is_never_gold_in_generated_output(
    intake: pathlib.Path,
) -> None:
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    price_ids = {
        row["id"] for row in result.passages if row["role"] == PassageRole.PRICE_BEARING.value
    }
    assert price_ids
    for row in result.queries:
        assert not (set(row["relevant"]) & price_ids), row["id"]


def test_cross_script_gold_is_in_a_different_script(intake: pathlib.Path) -> None:
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    scripts = {row["id"]: row["script"] for row in result.passages}
    cross = [row for row in result.queries if row["subset"] == Subset.CROSS_SCRIPT.value]
    assert cross
    for row in cross:
        assert any(scripts[gold] != row["script"] for gold in row["relevant"]), row["id"]


def test_written_output_reloads_through_the_dataset_loader(
    intake: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The round-trip that matters: generated YAML must satisfy every loader invariant."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # A minimal seed pair, so the loader has its required half.
    (data_dir / "corpus.yaml").write_text("dataset_version: 1\npassages: []\n", encoding="utf-8")
    (data_dir / "queries.yaml").write_text("dataset_version: 1\nqueries: []\n", encoding="utf-8")
    write_dataset(result, directory=data_dir)

    loaded = load_dataset(data_dir)
    assert len(loaded.passages) == result.passage_count
    assert len(loaded.queries) == result.query_count
    assert loaded.version == result.dataset_version


def test_half_a_generated_pair_is_refused(tmp_path: pathlib.Path) -> None:
    """Half a pair means someone deleted or failed to write one side, and loading the
    survivor would silently change what the benchmark measures."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "corpus.yaml").write_text("dataset_version: 1\npassages: []\n", encoding="utf-8")
    (data_dir / "queries.yaml").write_text("dataset_version: 1\nqueries: []\n", encoding="utf-8")
    (data_dir / "generated_corpus.yaml").write_text(
        "dataset_version: 2\npassages: []\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="present together"):
        load_dataset(data_dir)


def test_intents_declare_whether_they_are_service_scoped() -> None:
    assert Intent.WHAT_IS.is_service_scoped
    assert not Intent.COMPANY.is_service_scoped
    assert not Intent.TECHNOLOGY.is_service_scoped


def test_review_states_order_correctly() -> None:
    assert not ReviewState.PENDING.is_reviewed
    assert ReviewState.REVIEWED.is_reviewed
    assert ReviewState.NATIVE_REVIEWED.is_reviewed


def test_authority_separates_what_may_be_retrieved_from_what_may_not() -> None:
    """The classification PRD §6.5 makes a correctness requirement rather than a style one.

    Four values, and the split is not "price or not". A pricing *policy* is retrievable and
    must be — it is the correct answer to a pricing question. A price *value* is not.
    """
    assert Authority.DESCRIPTIVE.is_rag_eligible
    assert Authority.POLICY.is_rag_eligible
    assert not Authority.AUTHORITATIVE.is_rag_eligible
    assert not Authority.STRUCTURAL.is_rag_eligible
    assert Authority.AUTHORITATIVE.must_be_never_rag
    assert Authority.STRUCTURAL.must_be_never_rag
    assert not Authority.POLICY.must_be_never_rag


def test_a_rag_eligible_fact_marked_never_rag_is_refused(tmp_path: pathlib.Path) -> None:
    """The inverse of the authoritative mistake, and the one that would quietly delete the
    correct answer to every pricing question from the corpus."""
    broken = _SOURCE.replace(
        "        authority: policy\n        never_rag: false\n        source_reference:"
        " fixture brochure p5",
        "        authority: policy\n        never_rag: true\n        source_reference:"
        " fixture brochure p5",
    )
    _write_intake(tmp_path, source=broken)
    with pytest.raises(ValueError, match="nothing to answer from"):
        load_source_material(tmp_path / "risenext.yaml")


def test_structural_facts_never_become_passages(intake: pathlib.Path) -> None:
    """CRM field definitions are schema, not content.

    A lead-capture field list retrieved as knowledge would be answered to a caller as though
    it were something the business says. It is also the passage a naive `never_rag` filter
    would turn into a *price-bearing* negative, which would label a schema as a price.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    assert {fact.id for fact in material.structural_facts} == {"lead-fields"}
    assert not [row for row in result.passages if "lead-fields" in row["id"]]


def test_only_supplied_prices_become_price_bearing_passages(intake: pathlib.Path) -> None:
    """`price_values` is narrower than "every never_rag fact", and the narrowing matters:
    the fixture's structural lead-field list is also never-RAG and must not be swept in."""
    material = load_source_material(intake / "risenext.yaml")
    priced = [
        passage
        for passage in generate_negatives(material)
        if passage.role is PassageRole.PRICE_BEARING
    ]
    assert [passage.id for passage in priced] == ["neg-price-price-web"]


def test_a_source_with_no_prices_produces_no_price_bearing_passages(
    tmp_path: pathlib.Path,
) -> None:
    """The state of the real Rise Next material: customised quotations, no numeric prices.

    The corpus then satisfies something stronger than "prices are labelled traps" — there is
    no price in it to retrieve at all. Asserted here so a future change that starts
    manufacturing prices to satisfy the adversarial-role gate fails loudly.
    """
    priceless = _SOURCE[: _SOURCE.index("    authoritative_values:")]
    _write_intake(tmp_path, source=priceless)
    material = load_source_material(tmp_path / "risenext.yaml")

    assert material.price_values == ()
    assert not [
        passage
        for passage in generate_negatives(material)
        if passage.role is PassageRole.PRICE_BEARING
    ]


# ---------------------------------------------------------------------------
# Capability atoms
# ---------------------------------------------------------------------------


def test_every_named_capability_becomes_its_own_passage(intake: pathlib.Path) -> None:
    """The decomposition that carries the corpus.

    Before it, all of a service's sub-services lived in one passage, so "do you build
    e-commerce platforms?" and "do you do cloud deployment?" had identical gold and were the
    same retrieval problem. The supplied material names them individually; the corpus now
    does too.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    expected = {capability.id for capability in material.capabilities}
    assert expected
    assert expected <= {row["id"] for row in result.passages}
    for row in result.passages:
        if row["id"] in expected:
            assert row["tier"] == MaterialTier.SOURCE_GROUNDED.value
            assert row["provenance"]["source_type"] == "capabilities"
            assert row["provenance"]["generated_from"] == "capability_atom"


def test_a_capability_passage_says_only_what_the_source_says(intake: pathlib.Path) -> None:
    """The whole passage is a name and its owner, because that is all the source supplies.

    An earlier version also carried the service's full description into every capability
    passage, to give a four-word chunk something to embed. It made every capability of a
    service a near-duplicate of every other and of the service itself — 63 pairs, caught by
    `no_passage_duplication`. Padding a short fact with shared text does not enrich it.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    web = material.service("web")
    assert web is not None
    row = next(r for r in result.passages if r["id"] == "cap-web-design")
    assert (
        row["text"]
        == "design. Rise Next provides design as part of its Website development services."
    )
    assert web.description not in row["text"]


def test_capability_ids_are_derived_from_the_name_not_a_counter(
    intake: pathlib.Path,
) -> None:
    """So inserting a capability mid-list does not renumber every id after it — which would
    silently invalidate every review decision recorded against those ids."""
    material = load_source_material(intake / "risenext.yaml")
    ids = {capability.name: capability.id for capability in material.capabilities}
    assert ids["design"] == "cap-web-design"
    assert ids["store submission"] == "cap-app-store-submission"


def test_a_capability_query_points_at_its_own_capability_and_its_service(
    intake: pathlib.Path,
) -> None:
    """Grade 2 for the capability, grade 1 for the service that owns it."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    row = next(r for r in result.queries if r["derived_from"] == "en-cap-atom")
    assert row["intent"] == Intent.CAPABILITY_SPECIFIC.value
    capability_gold = {k: v for k, v in row["relevant"].items() if k.startswith("cap-")}
    assert set(capability_gold.values()) == {2}
    assert any(k.startswith("svc-") and v == 1 for k, v in row["relevant"].items())


def test_a_capability_template_fans_out_over_capabilities_not_services(
    intake: pathlib.Path,
) -> None:
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook, include_typo_variants=False)

    generated = [r for r in result.queries if r["derived_from"] == "en-cap-atom"]
    assert len(generated) == len(material.capabilities)
    assert len({r["text"] for r in generated}) == len(generated)


def test_a_template_may_not_carry_both_slots(tmp_path: pathlib.Path) -> None:
    """Fanning over services x capabilities is 483 near-identical queries per template, and
    the capability already determines its service — so the second slot adds only duplicates.
    """
    broken = _PHRASEBOOK.replace(
        'text: "do you provide {capability}"',
        'text: "do you provide {capability} for {service}"',
    )
    _write_intake(tmp_path, book=broken)
    with pytest.raises(ValueError, match="both a"):
        load_phrasebook(tmp_path / "phrasebook.yaml")


def test_a_capability_intent_without_a_capability_slot_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    broken = _PHRASEBOOK.replace(
        'text: "do you provide {capability}"', 'text: "do you provide that"'
    )
    _write_intake(tmp_path, book=broken)
    with pytest.raises(ValueError, match=re.escape("no {capability} slot")):
        load_phrasebook(tmp_path / "phrasebook.yaml")


def test_capability_sampling_is_service_balanced() -> None:
    """A truncated sample would take everything from the first service and nothing from the
    last, so a sampled subset would silently test only one service. Striding walks the list.
    """
    from tests.d8_bakeoff.corpus.build import _sampled
    from tests.d8_bakeoff.corpus.source_material import Capability

    capabilities = [
        Capability(service_id=f"s{index // 10}", service_name="S", name=f"c{index}")
        for index in range(70)
    ]
    sample = _sampled(capabilities, 7)
    assert len(sample) == 7
    # Every one of the seven synthetic services is represented exactly once.
    assert len({item.service_id for item in sample}) == 7
    assert _sampled(capabilities, None) == capabilities
    assert _sampled(capabilities, 999) == capabilities


def test_a_misattributed_capability_negative_is_never_gold(intake: pathlib.Path) -> None:
    """Every noun in it is genuine and only the attribution is false — which is what makes
    it the hardest negative the source can produce, and why it must never score."""
    material = load_source_material(intake / "risenext.yaml")
    negatives = generate_negatives(material)
    misattributed = [p for p in negatives if p.id.startswith("neg-misattributed-")]
    assert misattributed
    for passage in misattributed:
        assert not passage.role.may_be_gold
        assert passage.tier is MaterialTier.ADVERSARIAL


def test_a_topic_scoped_query_gets_only_the_clause_it_asks_about(
    intake: pathlib.Path,
) -> None:
    """The mechanism that keeps a split-up list from diluting a gold set.

    Splitting the never-promise list into nine passages made every guarantee question's gold
    nine passages wide, and over a 143-passage corpus that turned `answerability@8` into a
    formality every candidate passes. Topic matching narrows it back to the clause that
    actually answers the question — and the unrelated clauses become hard negatives, which is
    strictly better than being spurious gold.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook, include_typo_variants=False)

    ranking = next(r for r in result.queries if r["derived_from"] == "en-guarantee-ranking")
    assert ranking["relevant"]["fact-policy-never-ranking"] == 2
    assert "fact-policy-never-revisions" not in ranking["relevant"]
    # The overview list still partially answers it.
    assert ranking["relevant"]["fact-policy-never-promise"] == 1


def test_a_generic_query_falls_back_to_the_overview_list(intake: pathlib.Path) -> None:
    """A question that named nothing specific is honestly answered by the whole list, and by
    no individual clause — otherwise the corpus would assert that "any guarantees at all?"
    is about search rankings."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook, include_typo_variants=False)

    generic = next(r for r in result.queries if r["derived_from"] == "en-guarantee-generic")
    assert generic["relevant"] == {"fact-policy-never-promise": 1}


# ---------------------------------------------------------------------------
# Spot checks
# ---------------------------------------------------------------------------


def _spot_file(directory: pathlib.Path, body: str) -> pathlib.Path:
    path = directory / "spot_checks.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_an_absent_spot_check_file_is_a_legitimate_state(tmp_path: pathlib.Path) -> None:
    """Spot checks are the last artefact to arrive, and a corpus that could not build
    without them could never produce the batch a reviewer needs in order to make them."""
    assert load_spot_checks(tmp_path / "spot_checks.yaml") == {}


def test_an_approved_spot_check_makes_one_query_individually_reviewed(
    intake: pathlib.Path,
) -> None:
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    baseline = build_dataset(material, phrasebook, include_typo_variants=False)
    target = next(r for r in baseline.queries if r["derived_from"] == "hi-deva-what-is")
    assert target["validation"] == "synthetic_unreviewed", "template is pending in the fixture"

    checks = load_spot_checks(
        _spot_file(
            intake,
            f"""
            spot_checks:
              - query_id: {target["id"]}
                decision: approved
                reviewed_by: Priya Nair
                reviewed_on: 2026-08-11
            """,
        )
    )
    result = build_dataset(material, phrasebook, spot_checks=checks, include_typo_variants=False)
    row = next(r for r in result.queries if r["id"] == target["id"])

    # A human read *this rendered query*, which is exactly what native_reviewed means — so it
    # is granted even though the template it came from is still pending.
    assert row["validation"] == "native_reviewed"
    assert row["review_inherited"] is False
    assert row["reviewed_by"] == "Priya Nair"
    assert row["provenance"]["human_review_required"] is False


def test_a_rejected_spot_check_outranks_an_approved_template(intake: pathlib.Path) -> None:
    """The reason spot checks *bound* template propagation rather than merely sampling it.

    A reviewer who looked at the actual substitution and said no has seen more than a
    template-level approval that never saw it.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    baseline = build_dataset(material, phrasebook, include_typo_variants=False)
    target = next(r for r in baseline.queries if r["derived_from"] == "en-what-is")
    assert target["validation"] == "native_reviewed", "template is approved in the fixture"

    checks = load_spot_checks(
        _spot_file(
            intake,
            f"""
            spot_checks:
              - query_id: {target["id"]}
                decision: rejected
                reviewed_by: Priya Nair
                reviewed_on: 2026-08-11
                notes: no caller would phrase it this way for this service
            """,
        )
    )
    result = build_dataset(material, phrasebook, spot_checks=checks, include_typo_variants=False)
    row = next(r for r in result.queries if r["id"] == target["id"])

    assert row["validation"] == "synthetic_unreviewed"
    assert row["review_inherited"] is False
    assert row["notes"]


def test_a_spot_check_signed_by_a_model_is_refused(intake: pathlib.Path) -> None:
    path = _spot_file(
        intake,
        """
        spot_checks:
          - query_id: q-en-what-is-web
            decision: approved
            reviewed_by: GPT-4
            reviewed_on: 2026-08-11
        """,
    )
    with pytest.raises(ValueError, match="not a reviewer"):
        load_spot_checks(path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("decision", "needs_edit", "approved or rejected"),
        ("reviewed_on", "", "reviewed_on"),
    ],
)
def test_an_incomplete_spot_check_is_refused(
    intake: pathlib.Path, field: str, value: str, match: str
) -> None:
    """`needs_edit` is refused on purpose: a generated query cannot be edited in place, so a
    correction recorded here would be overwritten by the next build. Wrong wording is a
    template problem."""
    rows = {
        "query_id": "q-en-what-is-web",
        "decision": "approved",
        "reviewed_by": "Priya Nair",
        "reviewed_on": "2026-08-11",
    }
    rows[field] = value
    path = _spot_file(
        intake,
        "spot_checks:\n"
        + "".join(f"  {'- ' if k == 'query_id' else '  '}{k}: '{v}'\n" for k, v in rows.items()),
    )
    with pytest.raises(ValueError, match=match):
        load_spot_checks(path)


def test_a_spot_check_for_an_unknown_query_is_warned_about(intake: pathlib.Path) -> None:
    """A dangling judgement means the sampling changed or a template was renamed. Silently
    ignoring it would let a subset look spot-checked while the reviewed rows no longer
    exist."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    checks = load_spot_checks(
        _spot_file(
            intake,
            """
            spot_checks:
              - query_id: q-does-not-exist
                decision: approved
                reviewed_by: Priya Nair
                reviewed_on: 2026-08-11
            """,
        )
    )
    result = build_dataset(material, phrasebook, spot_checks=checks)
    assert any("q-does-not-exist" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_every_generated_row_carries_provenance(intake: pathlib.Path) -> None:
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    for row in (*result.passages, *result.queries):
        provenance = row["provenance"]
        assert provenance["source_id"], row["id"]
        assert provenance["source_version"] == 1, row["id"]
        assert provenance["source_type"], row["id"]
        assert provenance["generated_from"], row["id"]
        assert "human_review_required" in provenance, row["id"]


def test_variants_of_one_template_share_one_source_id(intake: pathlib.Path) -> None:
    """The property that stops a paraphrase becoming an independent business fact.

    Seven phrasings of one question are one question. Counting distinct `source_id`s is
    therefore the honest measure of how much a subset tests, and a corpus cannot look like
    broad coverage by rephrasing.
    """
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    from_what_is = [row for row in result.queries if row["derived_from"] == "en-what-is"]
    assert len(from_what_is) > 1
    assert {row["provenance"]["source_id"] for row in from_what_is} == {"en-what-is"}
    # …and the transform that produced each one is still distinguishable.
    assert len({row["provenance"]["generated_from"] for row in from_what_is}) > 1


def test_unreviewed_output_always_requires_human_review(intake: pathlib.Path) -> None:
    """DECISION 6, as arithmetic rather than as a rule someone has to remember."""
    material = load_source_material(intake / "risenext.yaml")
    phrasebook = load_phrasebook(intake / "phrasebook.yaml")
    result = build_dataset(material, phrasebook)

    for row in result.queries:
        needs_review = row["provenance"]["human_review_required"]
        assert needs_review == (row["validation"] == "synthetic_unreviewed"), row["id"]
