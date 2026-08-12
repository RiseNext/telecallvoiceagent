"""Build the dataset from the two intake files. Deterministic, offline, no model.

    build_dataset(material, phrasebook) -> BuildResult

Writes `data/generated_corpus.yaml` and `data/generated_queries.yaml`. These are the whole
corpus: the hand-written seed that used to sit beside them was retired on 2026-08-11 once
the official Rise Next material arrived, because it was invented content and no amount of
review could make invented content support a decision. `data/corpus.yaml` survives as an
empty file — `load_dataset` requires the pair to be present together, and empty is a
statement where missing would be an accident.

**The multiplication is the point.** Templates fan out over one of three axes:

    capability-scoped x 69 named sub-services (all in `en`, sampled elsewhere)
    service-scoped    x 7 services
    business-wide     x 1
        + typo variants on Latin-script queries

which reaches ~800 judged queries from ~100 reviewable templates — and a reviewer validates
the templates, not the output.

**Review propagates, and it is recorded.** A generated query's validation is the *weakest*
of its inputs: the template's review state and (for service-scoped intents) the service
name's. `derived_from` names the template so the inheritance is auditable, and
`review_inherited` marks that it was inherited rather than performed. `quality.py` then
requires a spot-check floor per subset, so propagation is an efficiency rather than a
loophole.

**Every row carries provenance back to the intake entry it came from** — `source_id`,
`source_version`, `source_type`, `generated_from` and `human_review_required`. The last is
*derived* from the validation status rather than stated, so the two cannot drift; the rest
exist so a paraphrase can never be mistaken for an independent business fact. Seven
phrasings of one template share one `source_id`, and counting distinct `source_id`s is
therefore the honest measure of how much a subset tests.

**Nothing here invents business content.** The builder only rearranges what the intake
files supply: it splits, joins, substitutes and corrupts, and every transform is named in
`generated_from`. Where the source is silent the output is silent — a section with no
entries produces no passages, and the gap is reported rather than filled.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import yaml

from tests.d8_bakeoff.corpus.negatives import GeneratedPassage, generate_negatives
from tests.d8_bakeoff.corpus.source_material import (
    Capability,
    Intent,
    Phrasebook,
    QueryTemplate,
    ReviewState,
    ServiceEntry,
    SourceFact,
    SourceMaterial,
    SpotCheck,
)
from tests.d8_bakeoff.corpus.variants import detect_script, typo_variants
from tests.d8_bakeoff.dataset import (
    DATA_DIR,
    MaterialTier,
    PassageRole,
    Provenance,
    Script,
    Subset,
    ValidationStatus,
)

__all__ = [
    "GENERATED_CORPUS",
    "GENERATED_QUERIES",
    "BuildResult",
    "build_dataset",
    "write_dataset",
]

GENERATED_CORPUS: Final[str] = "generated_corpus.yaml"
GENERATED_QUERIES: Final[str] = "generated_queries.yaml"

#: Which script's service name to substitute, per subset. Romanised and code-mixed subsets
#: take the Latin form, because a romanised Hindi caller says "website development", not a
#: transliteration of it — that is a fact about how people speak, and it is why the
#: phrasebook carries all four forms rather than deriving them.
_SUBSET_NAME_SCRIPT: Final[Mapping[str, str]] = {
    Subset.EN.value: "latin",
    Subset.HI_DEVANAGARI.value: "devanagari",
    Subset.HI_ROMANISED.value: "latin",
    Subset.TE_TELUGU.value: "telugu",
    Subset.TE_ROMANISED.value: "latin",
    Subset.CODEMIX_EN_HI.value: "latin",
    Subset.CODEMIX_EN_TE.value: "latin",
    Subset.CROSS_SCRIPT.value: "latin",
}

#: How many capabilities each subset's capability-scoped templates fan out over.
#:
#: `en` takes all of them; every other subset takes a deterministic sample. This is the one
#: place the corpus is deliberately *capped*, and the reason is review capacity rather than
#: cost: fanning all 69 across all 8 subsets yields ~1,600 queries against a 250 target,
#: and the 10%-individual-review floor scales with query count, so it would roughly
#: quadruple the human work for a metric already far past target. Capabilities without a
#: query in a given language are not waste — they serve as realistic distractors, which is
#: what makes the retrieval task hard.
_CAPABILITY_SAMPLE: Final[Mapping[str, int | None]] = {
    Subset.EN.value: None,  # None = all
    Subset.HI_ROMANISED.value: 24,
    Subset.TE_ROMANISED.value: 24,
    Subset.CODEMIX_EN_HI.value: 24,
    Subset.CODEMIX_EN_TE.value: 24,
    Subset.HI_DEVANAGARI.value: 20,
    Subset.TE_TELUGU.value: 20,
    Subset.CROSS_SCRIPT.value: 20,
}


def _sampled(capabilities: Sequence[Capability], limit: int | None) -> Sequence[Capability]:
    """A deterministic, service-balanced sample.

    Strided rather than truncated: `capabilities[:20]` would take everything from the first
    two services and nothing from the last five, so a sampled subset would silently test
    only Technology Solutions. Striding walks the whole list, and because the list is in
    declaration order the stride lands across every service.
    """
    if limit is None or limit >= len(capabilities):
        return capabilities
    step = len(capabilities) / limit
    return [capabilities[int(index * step)] for index in range(limit)]


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Everything the build produced, plus the counts a report needs."""

    passages: tuple[Mapping[str, Any], ...]
    queries: tuple[Mapping[str, Any], ...]
    dataset_version: int
    #: subset -> query count
    per_subset: Mapping[str, int]
    #: Warnings that do not stop the build but that a human should see.
    warnings: tuple[str, ...] = ()

    @property
    def passage_count(self) -> int:
        return len(self.passages)

    @property
    def query_count(self) -> int:
        return len(self.queries)


# ---------------------------------------------------------------------------
# Passages
# ---------------------------------------------------------------------------


def _service_passages(material: SourceMaterial) -> Iterator[GeneratedPassage]:
    """One description passage and one capabilities passage per service.

    Split rather than combined because they answer different intents, and a single passage
    answering everything would make `answers` meaningless and every gold set identical.
    """
    for service in material.services:
        validation = (
            ValidationStatus.SOURCE_MATERIAL
            if service.review_status.is_reviewed
            else ValidationStatus.SYNTHETIC_UNREVIEWED
        )
        yield GeneratedPassage(
            id=f"svc-{service.id}",
            text=f"{service.name}. {service.description}",
            language="en",
            script=Script.LATIN,
            tier=MaterialTier.SOURCE_GROUNDED,
            role=PassageRole.GOLD_CANDIDATE,
            validation=validation,
            source_reference=service.source_reference,
            provenance=_source_provenance(
                material, service.id, "services", "service_description", validation
            ),
        )
        yield GeneratedPassage(
            id=f"svc-{service.id}-capabilities",
            text=(
                f"What {service.name.lower()} includes: "
                + "; ".join(service.capabilities)
                + "."
                + (
                    f" Built with {', '.join(service.technologies)}."
                    if service.technologies
                    else ""
                )
            ),
            language="en",
            script=Script.LATIN,
            tier=MaterialTier.SOURCE_GROUNDED,
            role=PassageRole.GOLD_CANDIDATE,
            validation=validation,
            source_reference=service.source_reference,
            provenance=_source_provenance(
                material, service.id, "services", "service_capabilities", validation
            ),
        )


def _capability_passages(material: SourceMaterial) -> Iterator[GeneratedPassage]:
    """One passage per named sub-service — 69 of them, and the bulk of the corpus.

    **Nothing here is invented.** The passage is the capability name and the name of the
    service that owns it. Both trace to `risenext.yaml`; the transform is a join, not a
    claim.

    **Deliberately short, and that is a correction rather than a shortcut.** The first
    version of this function also carried the owning service's full description into every
    capability passage, on the theory that a four-word chunk embeds noisily. It does — but
    the shared description then dominated the trigrams, and `no_passage_duplication` failed
    with 63 near-identical pairs: every capability of a service looked like every other, and
    like the service itself. The gate was added in the same change and caught it immediately.

    Padding a short fact with shared text does not make it a richer fact; it makes it a
    duplicate. The supplied material gives a name and an owner, so the passage is a name and
    an owner, and the corpus honestly reflects that these are short facts.
    """
    for capability in material.capabilities:
        service = material.service(capability.service_id)
        if service is None:  # pragma: no cover - `capabilities` is derived from `services`
            continue
        validation = (
            ValidationStatus.SOURCE_MATERIAL
            if service.review_status.is_reviewed
            else ValidationStatus.SYNTHETIC_UNREVIEWED
        )
        yield GeneratedPassage(
            id=capability.id,
            text=(
                f"{capability.name}. Rise Next provides {capability.name.lower()} as part of "
                f"its {capability.service_name} services."
            ),
            language="en",
            script=Script.LATIN,
            tier=MaterialTier.SOURCE_GROUNDED,
            role=PassageRole.GOLD_CANDIDATE,
            validation=validation,
            source_reference=service.source_reference,
            provenance=_source_provenance(
                material, service.id, "capabilities", "capability_atom", validation
            ),
        )


def _source_provenance(
    material: SourceMaterial,
    source_id: str,
    source_type: str,
    transform: str,
    validation: ValidationStatus,
) -> Provenance:
    """Provenance for a passage taken straight from the intake file.

    `human_review_required` is derived from the validation status rather than stated, so the
    two can never disagree — a hand-set `false` next to `synthetic_unreviewed` is precisely
    the false claim DECISION 6 exists to prevent, and deriving it removes the opportunity.
    """
    return Provenance(
        source_id=source_id,
        source_version=material.version,
        source_type=source_type,
        generated_from=transform,
        human_review_required=not validation.is_review_complete,
    )


def _fact_passages(material: SourceMaterial) -> Iterator[GeneratedPassage]:
    """Company, FAQ and policy passages — everything RAG-eligible that is not a service."""
    yield GeneratedPassage(
        id="company-about",
        text=f"{material.company_name}. {material.company_description}",
        language="en",
        script=Script.LATIN,
        tier=MaterialTier.SOURCE_GROUNDED,
        role=PassageRole.GOLD_CANDIDATE,
        validation=ValidationStatus.SOURCE_MATERIAL,
        source_reference=f"supplied by {material.supplied_by} on {material.supplied_on}",
        provenance=_source_provenance(
            material, "company", "company", "company_profile", ValidationStatus.SOURCE_MATERIAL
        ),
    )
    for fact in material.rag_eligible_facts:
        if fact.supersedes_current:
            # Superseded material becomes a `stale` negative, not an ordinary passage.
            continue
        validation = (
            ValidationStatus.SOURCE_MATERIAL
            if fact.review_status.is_reviewed
            else ValidationStatus.SYNTHETIC_UNREVIEWED
        )
        yield GeneratedPassage(
            id=f"fact-{fact.id}",
            text=(f"{fact.title}. {fact.text}" if fact.title else fact.text),
            language="en",
            script=Script.LATIN,
            tier=MaterialTier.SOURCE_GROUNDED,
            role=PassageRole.GOLD_CANDIDATE,
            validation=validation,
            source_reference=fact.source_reference,
            provenance=_source_provenance(
                material, fact.id, fact.source_type, "source_fact", validation
            ),
        )


# ---------------------------------------------------------------------------
# Gold assignment
# ---------------------------------------------------------------------------


#: Gold sets for the two intents that are purely about one service.
#:
#: Explicit rather than inferred from text similarity, because inferring gold from
#: similarity is circular: it would score a retrieval system against judgements a retrieval
#: heuristic made. Grade 2 = fully answers on its own; grade 1 = partially answers.
_SERVICE_GOLD: Final[Mapping[Intent, Mapping[str, int]]] = {
    Intent.WHAT_IS: {"svc-{id}": 2, "svc-{id}-capabilities": 1},
    Intent.CAPABILITY: {"svc-{id}-capabilities": 2, "svc-{id}": 1},
}


@dataclass(frozen=True, slots=True)
class _GoldRule:
    """One gold-selection rule, expressed against the intake *schema*.

    **Keyed on the intake section, not on fact ids.** Hardcoding ids would couple this
    module to one particular intake file, so a second tenant's material — or a renamed
    entry — would silently produce goldless queries, which look exactly like retrieval
    failures. Section, `kind` and `topic` are schema, and schema is what a generator may
    depend on.
    """

    source_type: str
    kind: str | None
    grade: int
    #: When set, the fact's `topic` must equal the *template's* topic.
    #:
    #: The mechanism that keeps a split-up list from diluting a gold set. The never-promise
    #: list is nine passages; a question about Google rankings is answered by one of them, and
    #: grading all nine would make answerability@8 over 143 passages a formality. A template
    #: with no topic matches nothing here and falls back to the overview passage — which is
    #: the honest answer to a question that named nothing specific.
    topic_scoped: bool = False


_SECTION_GOLD: Final[Mapping[Intent, tuple[_GoldRule, ...]]] = {
    Intent.COMPANY: (_GoldRule("company_profile", None, 2),),
    Intent.INDUSTRIES: (_GoldRule("industries", None, 2),),
    Intent.PROCESS: (_GoldRule("business_process", None, 2),),
    Intent.TECHNOLOGY: (_GoldRule("technology_capabilities", None, 2),),
    # The pricing benchmark. Gold is the POLICY, because the supplied material contains no
    # numeric prices — so the correct retrieval result for "how much does X cost" is
    # "quotations are customised", and a candidate that cannot surface it leaves a model
    # with nothing to answer from.
    Intent.PRICING: (
        _GoldRule("pricing_policy", None, 2),
        _GoldRule("policies", "never_promise", 1),
        # Only the clause the query is actually about — `exact_pricing` for a "just give me
        # a number" query. Grading all nine would make eight unrelated promises count as
        # partial answers to "how much does this cost".
        _GoldRule("policies", "never_promise_item", 1, topic_scoped=True),
    ),
    # The loan benchmark. Rise Next assists with documentation and bank coordination and is
    # NOT a lender; the disclaimer is the highest-priority constraint in the material.
    Intent.LENDING: (
        _GoldRule("financing_disclaimer", None, 2),
        _GoldRule("faqs", "lending", 1),
    ),
    # A guarantee question that names its subject gets that clause at grade 2 and the full
    # list at 1 — the specific answer beats the general one. A generic "can you guarantee
    # anything?" carries no topic, matches no clause, and gets the list alone.
    Intent.GUARANTEES: (
        _GoldRule("policies", "never_promise", 1),
        _GoldRule("policies", "never_promise_item", 2, topic_scoped=True),
    ),
    Intent.OUT_OF_SCOPE: (_GoldRule("policies", "uncertainty", 2),),
    Intent.POLICY_OVERRIDE: (
        _GoldRule("policies", "never_promise", 2),
        _GoldRule("policies", "never_promise_item", 1, topic_scoped=True),
        _GoldRule("pricing_policy", None, 1),
    ),
    # A timeline question has no per-service answer in the material — and the policy list
    # forbids promising a fixed date before scope. So the right result is the process plus
    # that constraint, which makes this a test of surfacing a limit rather than a duration.
    Intent.HOW_LONG: (
        _GoldRule("business_process", None, 2),
        _GoldRule("policies", "never_promise", 1),
        _GoldRule("policies", "never_promise_item", 1, topic_scoped=True),
    ),
}


#: Intents for which an FAQ about a service is a partial answer, at grade 1.
#:
#: An FAQ carries the question in the *caller's* words rather than the brochure's, which is
#: often closer to how the query is actually phrased — so a candidate that retrieves the FAQ
#: has found something genuinely useful. Grade 1 rather than 2 records that it is the weaker
#: source: the service entry is the complete answer.
_FAQ_PARTIAL_INTENTS: Final[frozenset[Intent]] = frozenset({Intent.WHAT_IS, Intent.CAPABILITY})


def _gold_for(
    intent: Intent,
    service: ServiceEntry | None,
    facts: Sequence[SourceFact],
    capability: Capability | None = None,
    topic: str | None = None,
) -> dict[str, int]:
    """The graded gold set for one (intent, service-or-capability, topic) triple."""
    gold: dict[str, int] = {}

    if intent is Intent.COMPANY:
        gold["company-about"] = 2

    if capability is not None:
        # The capability's own passage fully answers it; its service partially does, because
        # the service description is where the capability's context lives.
        gold[capability.id] = 2
        gold[f"svc-{capability.service_id}"] = 1
        gold[f"svc-{capability.service_id}-capabilities"] = 1

    for rule in _SECTION_GOLD.get(intent, ()):
        for fact in facts:
            if fact.source_type != rule.source_type:
                continue
            if rule.kind is not None and fact.kind != rule.kind:
                continue
            if rule.topic_scoped and (topic is None or fact.topic != topic):
                continue
            gold[f"fact-{fact.id}"] = rule.grade

    if service is not None:
        template = _SERVICE_GOLD.get(intent, {})
        gold.update({key.format(id=service.id): grade for key, grade in template.items()})
        if intent in _FAQ_PARTIAL_INTENTS:
            gold.update(
                {
                    f"fact-{fact.id}": 1
                    for fact in facts
                    if fact.source_type == "faqs" and fact.service_id == service.id
                }
            )

    return gold


def _weakest(*states: ReviewState) -> ReviewState:
    """The weakest review state among the inputs.

    A generated query is only as reviewed as its least-reviewed input. Taking the maximum
    would be the bug that lets a reviewed English template plus an unreviewed Hindi service
    name produce a "reviewed" Hindi query.
    """
    order = [ReviewState.PENDING, ReviewState.REVIEWED, ReviewState.NATIVE_REVIEWED]
    return min(states, key=order.index) if states else ReviewState.PENDING


def _validation_for(subset: str, state: ReviewState) -> ValidationStatus:
    """Map an intake review state onto the dataset's validation vocabulary."""
    if state is ReviewState.NATIVE_REVIEWED:
        return ValidationStatus.NATIVE_REVIEWED
    if state is ReviewState.REVIEWED and subset == Subset.EN.value:
        # English needs content review, not language review.
        return ValidationStatus.SOURCE_MATERIAL
    return ValidationStatus.SYNTHETIC_UNREVIEWED


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_dataset(
    material: SourceMaterial,
    phrasebook: Phrasebook,
    *,
    spot_checks: Mapping[str, SpotCheck] | None = None,
    # Bumped 2 -> 3 with the capability decomposition. The version exists to stop a query set
    # being scored against the corpus it was not written for, and 67 passages became 143 with
    # new ids and regraded gold — a result from before the change is not comparable with one
    # from after, and the version is what makes that visible rather than a footnote.
    dataset_version: int = 3,
    include_typo_variants: bool = True,
) -> BuildResult:
    """Generate passages and queries. Pure — writes nothing."""
    warnings: list[str] = []

    passages: list[GeneratedPassage] = [
        *_fact_passages(material),
        *_service_passages(material),
        *_capability_passages(material),
        *generate_negatives(material),
    ]
    passage_ids = {passage.id for passage in passages}
    gold_candidates = [
        (passage.id, passage.script.value) for passage in passages if passage.role.may_be_gold
    ]

    queries: list[Mapping[str, Any]] = []
    per_subset: dict[str, int] = {}

    for subset in Subset:
        templates = phrasebook.templates_for(subset.value)
        if not templates:
            warnings.append(f"subset {subset.value} has no phrasebook templates")
            continue
        for template in templates:
            for row in _queries_for_template(
                template=template,
                subset=subset,
                material=material,
                phrasebook=phrasebook,
                passage_ids=passage_ids,
                gold_candidates=gold_candidates,
                warnings=warnings,
                include_typo_variants=include_typo_variants,
                spot_checks=spot_checks or {},
            ):
                queries.append(row)
                per_subset[subset.value] = per_subset.get(subset.value, 0) + 1

    # A spot check naming a query the build no longer produces is a dangling judgement — the
    # sampling changed, or a template was renamed — and silently ignoring it would let a
    # subset look spot-checked while the reviewed rows no longer exist.
    generated_ids = {row["id"] for row in queries}
    for query_id in sorted(set(spot_checks or {}) - generated_ids):
        warnings.append(f"spot check names query {query_id}, which this build did not produce")

    return BuildResult(
        passages=tuple(_passage_row(passage) for passage in passages),
        queries=tuple(queries),
        dataset_version=dataset_version,
        per_subset=per_subset,
        warnings=tuple(warnings),
    )


def _queries_for_template(
    *,
    template: QueryTemplate,
    subset: Subset,
    material: SourceMaterial,
    phrasebook: Phrasebook,
    passage_ids: set[str],
    gold_candidates: Sequence[tuple[str, str]],
    warnings: list[str],
    include_typo_variants: bool,
    spot_checks: Mapping[str, SpotCheck],
) -> Iterator[Mapping[str, Any]]:
    # Exactly one of these fans out; the third case is a business-wide template, which
    # generates one query. `QueryTemplate` refuses a template carrying both slots.
    services: Sequence[ServiceEntry | None]
    capabilities: Sequence[Capability | None]
    if template.intent.is_capability_scoped:
        services = (None,)
        capabilities = _sampled(material.capabilities, _CAPABILITY_SAMPLE.get(subset.value))
    else:
        services = material.services if template.intent.is_service_scoped else (None,)
        capabilities = (None,)

    # A template may override which script's service name fills its slot; cross-script
    # templates use it so a Devanagari query does not render with a Latin service name and
    # stop being genuinely cross-script.
    name_script = template.name_script or _SUBSET_NAME_SCRIPT[subset.value]

    for service, capability in ((svc, cap) for svc in services for cap in capabilities):
        service_name: str | None = None
        name_state = ReviewState.NATIVE_REVIEWED
        if service is not None:
            names = phrasebook.names_for(service.id)
            if names is None:
                warnings.append(
                    f"service {service.id} has no phrasebook name entry; "
                    f"skipping template {template.id} for it"
                )
                continue
            service_name = names.for_script(name_script)
            name_state = names.review_status

        # Capability names stay in English in every subset, including the Devanagari and
        # Telugu ones. That is not a shortcut around translating 69 names — it is how these
        # terms are actually said: an Indian business caller asks about "E-Commerce
        # Platforms", not a transliteration of it, inside a Hindi or Telugu sentence frame.
        # It also means the rendered text is genuinely script-mixed, which `detect_script`
        # reports honestly as `mixed` rather than the frame's script.
        text = template.render(service_name, capability.name if capability else None)
        # Derived from the RENDERED text for every subset, never from a per-subset table.
        # The table was wrong for cross-script (it matched everything, so the gold filter
        # removed all of it — a test caught that as zero queries) and it is wrong again for
        # capability queries in an Indic frame, which are mixed rather than pure.
        query_script = Script(detect_script(text))

        gold = _gold_for(template.intent, service, material.facts, capability, template.topic)
        gold = {key: grade for key, grade in gold.items() if key in passage_ids}

        if subset is Subset.CROSS_SCRIPT:
            # Keep only gold whose script differs from the query's, or the hardest subset
            # would be padded with same-script cases that inflate its score.
            allowed = set(_cross_script_ids(query_script.value, gold_candidates))
            gold = {key: grade for key, grade in gold.items() if key in allowed}

        if not gold:
            subject = (
                capability.id
                if capability is not None
                else service.id
                if service is not None
                else "business-wide"
            )
            warnings.append(f"template {template.id} + {subject} produced no gold; dropped")
            continue

        state = _weakest(template.review_status, name_state)
        inherited_validation = _validation_for(subset.value, state)
        if capability is not None:
            base_id = f"q-{template.id}-{capability.slug}"
        elif service is not None:
            base_id = f"q-{template.id}-{service.id}"
        else:
            base_id = f"q-{template.id}"
        for query_id, query_text, transform in (
            (base_id, text, "template"),
            *(
                (f"{base_id}-{v.slug}", v.text, f"typo:{v.slug}")
                for v in (typo_variants(text, limit=1) if include_typo_variants else ())
            ),
        ):
            spot = spot_checks.get(query_id)
            row_validation, inherited = _apply_spot_check(spot, inherited_validation)
            yield _query_row(
                query_id=query_id,
                text=query_text,
                subset=subset,
                script=query_script,
                gold=gold,
                tier=MaterialTier.CONTROLLED_SYNTHETIC,
                validation=row_validation,
                derived_from=template.id,
                reviewed_by=spot.reviewed_by if spot else template.reviewed_by,
                reviewed_on=spot.reviewed_on if spot else template.reviewed_on,
                provenance=_query_provenance(phrasebook, template, transform, row_validation),
                intent=template.intent,
                review_inherited=inherited,
                notes=spot.notes if spot else None,
            )


def _apply_spot_check(
    spot: SpotCheck | None, inherited: ValidationStatus
) -> tuple[ValidationStatus, bool]:
    """Fold an individual judgement into a query's inherited review state.

    Three outcomes, and the middle one is the point of the mechanism:

    * **No spot check** — the query keeps whatever its template and service name earned, and
      stays `review_inherited`.
    * **Approved** — a human read *this rendered query* and said a caller would say it. That
      is exactly what `native_reviewed` means, so it is granted even when the template is
      still pending: the reviewer judged the finished text, not the template.
    * **Rejected** — forced down to `synthetic_unreviewed` regardless of what the template
      earned. A reviewer who looked at the actual substitution and said no outranks a
      template-level approval that never saw it, and that is the whole reason spot checks
      bound template propagation rather than merely sampling it.
    """
    if spot is None:
        return inherited, True
    if spot.is_approved:
        return ValidationStatus.NATIVE_REVIEWED, False
    return ValidationStatus.SYNTHETIC_UNREVIEWED, False


def _query_provenance(
    phrasebook: Phrasebook,
    template: QueryTemplate,
    transform: str,
    validation: ValidationStatus,
) -> Provenance:
    """Provenance for a generated query.

    `source_id` is the **template**, not the query, and that is the point: every phrasing
    generated from one template shares one id, so a subset cannot look like broad coverage
    when it is one question asked seven ways. A count of distinct `source_id`s is the honest
    measure of how much a subset actually tests.
    """
    return Provenance(
        source_id=template.id,
        source_version=phrasebook.version,
        source_type="phrasebook",
        generated_from=transform,
        human_review_required=not validation.is_review_complete,
    )


def _cross_script_ids(query_script: str, candidates: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(passage_id for passage_id, script in candidates if script != query_script)


def _provenance_row(provenance: Provenance) -> Mapping[str, Any]:
    """Serialise provenance.

    `human_review_required` is always written, even when `False`, because its absence is
    what a reader would have to interpret — and the safe interpretation (`True`) is the
    opposite of what an omitted `False` would mean.
    """
    row: dict[str, Any] = {"human_review_required": provenance.human_review_required}
    if provenance.source_id:
        row["source_id"] = provenance.source_id
    if provenance.source_version is not None:
        row["source_version"] = provenance.source_version
    if provenance.source_type:
        row["source_type"] = provenance.source_type
    if provenance.generated_from:
        row["generated_from"] = provenance.generated_from
    return row


def _passage_row(passage: GeneratedPassage) -> Mapping[str, Any]:
    row: dict[str, Any] = {
        "id": passage.id,
        "language": passage.language,
        "script": passage.script.value,
        "validation": passage.validation.value,
        "tier": passage.tier.value,
        "role": passage.role.value,
        "text": passage.text,
    }
    if passage.source_reference:
        row["source_reference"] = passage.source_reference
    if passage.derived_from:
        row["derived_from"] = passage.derived_from
    if passage.negative_kind:
        row["negative_kind"] = passage.negative_kind.value
    row["provenance"] = _provenance_row(passage.provenance)
    return row


def _query_row(
    *,
    query_id: str,
    text: str,
    subset: Subset,
    script: Script,
    gold: Mapping[str, int],
    tier: MaterialTier,
    validation: ValidationStatus,
    derived_from: str,
    reviewed_by: str | None,
    reviewed_on: str | None,
    provenance: Provenance,
    intent: Intent,
    review_inherited: bool = True,
    notes: str | None = None,
) -> Mapping[str, Any]:
    row: dict[str, Any] = {
        "id": query_id,
        "text": text,
        "subset": subset.value,
        "script": script.value,
        "intent": intent.value,
        "validation": validation.value,
        "tier": tier.value,
        "derived_from": derived_from,
        "review_inherited": review_inherited,
        "provenance": _provenance_row(provenance),
        "relevant": dict(gold),
    }
    if validation is ValidationStatus.NATIVE_REVIEWED:
        row["reviewed_by"] = reviewed_by
        row["reviewed_on"] = reviewed_on
    if notes:
        row["notes"] = notes
    return row


def write_dataset(
    result: BuildResult, *, directory: pathlib.Path | None = None
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write the generated halves, and return their paths."""
    root = directory or DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    header = (
        "# GENERATED FILE - do not hand-edit.\n"
        "#\n"
        "# Produced by `python -m tests.d8_bakeoff.corpus.cli build` from\n"
        "# source/risenext.yaml and source/phrasebook.yaml. Edit those and rebuild.\n"
        "#\n"
        "# Hand edits are lost on the next build, and worse, they break the provenance\n"
        "# chain: every row here carries `derived_from` so that a reviewed template can be\n"
        "# audited as the thing that vouched for it.\n\n"
    )

    corpus_path = root / GENERATED_CORPUS
    corpus_path.write_text(
        header
        + yaml.safe_dump(
            {"dataset_version": result.dataset_version, "passages": list(result.passages)},
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )

    queries_path = root / GENERATED_QUERIES
    queries_path.write_text(
        header
        + yaml.safe_dump(
            {"dataset_version": result.dataset_version, "queries": list(result.queries)},
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )
    return corpus_path, queries_path
