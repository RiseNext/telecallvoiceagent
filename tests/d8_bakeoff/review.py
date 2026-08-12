"""The human-review workflow: export a bundle, review it, apply the decisions.

Reviewing several hundred generated queries one at a time is work nobody finishes, and a
review that does not finish is a review that did not happen. So the bundle is organised
around the thing that makes review efficient: **templates, not queries.**

    export  ->  review/pending-<subset>.yaml     one row per template, with samples
    review  ->  a human edits `decision`, `reviewed_by`, `notes`
    apply   ->  decisions merge back into source/phrasebook.yaml

A reviewer sees ~8 rows per language instead of ~120, and each row shows the template plus
a handful of the queries it actually generated — so they are judging the *effect*, not an
abstraction. Reviewing one template validates the phrasing of everything derived from it,
recorded per query as `derived_from` + `review_inherited` so the inheritance is auditable.

**What the reviewer is asked, and why each question is separate:**

| Field | Question | Why it is its own field |
|---|---|---|
| `natural` | would a caller actually say this? | the failure a translator introduces: correct grammar nobody speaks |
| `semantically_equivalent` | does it still ask what the English asks? | a natural sentence that drifted in meaning breaks the gold set, not the phrasing |
| `script_correct` | right script, correct conjuncts and matras? | catches the Unicode damage this codebase keeps finding |
| `code_mixing_natural` | is the mix where a real speaker would put it? | code-mix is the normal case here, and "all-English nouns" vs "all-Hindi frame" are different errors |
| `grades_correct` | are the relevance grades right? | a judgement about retrieval, not about language — often a different reviewer |
| `negatives_correct` | is this hard negative genuinely wrong? | a "distractor" that is actually a correct answer inverts the test |

**Reviewer identity is required and is not faked.** `apply_decisions` refuses a decision
with no reviewer, refuses a reviewer name that matches a placeholder, and refuses a review
date that is absent. That is not bureaucracy: when a benchmark result is disputed, "who said
this Telugu was natural" has to have an answer, and an unattributable review is
indistinguishable from no review.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import yaml

from tests.d8_bakeoff.corpus.source_material import Phrasebook, QueryTemplate, ReviewState
from tests.d8_bakeoff.dataset import Dataset, Query, Subset

__all__ = [
    "PLACEHOLDER_REVIEWERS",
    "REVIEW_DIR",
    "Decision",
    "ReviewBundle",
    "ReviewDecision",
    "ReviewRow",
    "apply_decisions",
    "build_bundle",
    "build_spot_check_batch",
    "is_placeholder_reviewer",
    "write_bundle",
    "write_spot_check_batch",
]

REVIEW_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent / "review"

#: Names that are not a reviewer. Refused so an unattributable review cannot pass as one.
#:
#: Includes model names deliberately: a model cannot vouch for whether a Telugu sentence is
#: natural, and a corpus reviewed by the thing that generated it is not reviewed.
PLACEHOLDER_REVIEWERS: Final[frozenset[str]] = frozenset(
    {
        "",
        "fill_me",
        "tbd",
        "todo",
        "none",
        "n/a",
        "na",
        "reviewer",
        "a reviewer",
        "anonymous",
        "unknown",
        "self",
        "claude",
        "assistant",
        "ai",
        "llm",
        "chatgpt",
        "gpt",
        "gemini",
        "copilot",
        "bot",
        "model",
    }
)


def is_placeholder_reviewer(name: str) -> bool:
    """Whether `name` names nobody who can be asked about a judgement.

    **Matched per word, not on the whole string**, because the realistic way a model name
    gets typed carries a version: `GPT-4`, `Claude 3.5`, `gemini-pro`. An exact-string check
    passes all three, which defeats the guard at the first attempt — verified by trying it.

    Per *word* and not by substring, equally deliberately. `ai` is on the list, and `Nair`,
    `Rai` and `Vaidya` are ordinary Indian surnames that contain it. A guard that refuses a
    real reviewer's name is worse than no guard: it teaches the reviewer to type something
    else, and what they type will be untraceable.
    """
    words = re.split(r"[^a-z0-9/]+", name.casefold())
    if any(word in PLACEHOLDER_REVIEWERS for word in words if word):
        return True
    return name.strip().casefold() in PLACEHOLDER_REVIEWERS


#: How many generated examples accompany each template in the bundle. Enough to judge the
#: substitution across different service names; few enough that the row stays readable.
SAMPLES_PER_TEMPLATE: Final[int] = 4


class Decision(StrEnum):
    """What a reviewer concluded."""

    PENDING = "pending"
    APPROVED = "approved"
    """Natural, semantically equivalent, correct script. Propagates to derived queries."""
    REJECTED = "rejected"
    """Unusable as written. The template must be rewritten, not patched."""
    NEEDS_EDIT = "needs_edit"
    """Nearly right. `corrected_text` carries the reviewer's replacement."""


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """One template presented for review, with samples of what it generated."""

    template_id: str
    subset: str
    intent: str
    style: str
    template_text: str
    current_status: ReviewState
    generated_count: int
    samples: tuple[str, ...]

    def as_mapping(self) -> Mapping[str, Any]:
        """The YAML shape a reviewer edits.

        The questions come pre-filled as `null` rather than absent, so a reviewer sees what
        they are being asked and an unanswered question is visibly unanswered.
        """
        return {
            "template_id": self.template_id,
            "subset": self.subset,
            "intent": self.intent,
            "style": self.style,
            "template_text": self.template_text,
            "generated_query_count": self.generated_count,
            "samples": list(self.samples),
            # ---- the reviewer fills in from here down ----
            "decision": Decision.PENDING.value,
            "corrected_text": None,
            "natural": None,
            "semantically_equivalent": None,
            "script_correct": None,
            "code_mixing_natural": None,
            "grades_correct": None,
            "reviewed_by": None,
            "reviewed_on": None,
            "notes": None,
        }


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    """Everything a reviewer for one subset needs."""

    subset: str
    rows: tuple[ReviewRow, ...]

    @property
    def template_count(self) -> int:
        return len(self.rows)

    @property
    def covered_queries(self) -> int:
        return sum(row.generated_count for row in self.rows)

    @property
    def efficiency(self) -> float:
        """Queries validated per template reviewed. The number that justifies the design."""
        return self.covered_queries / self.template_count if self.rows else 0.0


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """One applied decision, validated."""

    template_id: str
    decision: Decision
    reviewed_by: str
    reviewed_on: str
    corrected_text: str | None = None
    notes: str | None = None
    natural: bool | None = None
    semantically_equivalent: bool | None = None
    script_correct: bool | None = None
    code_mixing_natural: bool | None = None
    grades_correct: bool | None = None

    @property
    def resulting_state(self) -> ReviewState:
        """What the template's `review_status` becomes.

        Only `APPROVED` yields `native_reviewed`. `NEEDS_EDIT` deliberately does **not**:
        the corrected text has not itself been reviewed in context yet, so it goes back
        through a build and a fresh review rather than being promoted on the strength of the
        correction.
        """
        return (
            ReviewState.NATIVE_REVIEWED
            if self.decision is Decision.APPROVED
            else ReviewState.PENDING
        )


def build_bundle(dataset: Dataset, phrasebook: Phrasebook, subset: Subset) -> ReviewBundle:
    """Build the review bundle for one subset.

    Templates with no generated queries are included rather than skipped: a template that
    generated nothing is usually a bug in the intake file (a missing service name, an intent
    the source material cannot answer), and it is worth a reviewer seeing that.
    """
    by_template: dict[str, list[Query]] = {}
    for query in dataset.queries_in(subset):
        if query.derived_from:
            by_template.setdefault(query.derived_from, []).append(query)

    rows: list[ReviewRow] = []
    for template in phrasebook.templates_for(subset.value):
        generated = by_template.get(template.id, [])
        rows.append(
            ReviewRow(
                template_id=template.id,
                subset=subset.value,
                intent=template.intent.value,
                style=template.style.value,
                template_text=template.text,
                current_status=template.review_status,
                generated_count=len(generated),
                # Sorted for determinism, so re-exporting a bundle does not reshuffle it
                # under a reviewer who is halfway through.
                samples=tuple(query.text for query in sorted(generated, key=lambda item: item.id))[
                    :SAMPLES_PER_TEMPLATE
                ],
            )
        )
    return ReviewBundle(subset=subset.value, rows=tuple(rows))


def build_spot_check_batch(
    dataset: Dataset, *, fraction: float, existing: Mapping[str, Any] | None = None
) -> Mapping[str, tuple[Query, ...]]:
    """The queries that still need **individual** judgement, per subset.

    Returns exactly what each subset is short by, sampled **across templates rather than
    from the top of the list**. That distinction is the whole value of the batch: the queries
    are ordered by id, so a naive head-slice would hand a reviewer twenty renderings of the
    same two templates — which is the one thing template-level review has already covered,
    and precisely the case a spot check exists to *not* re-test. Striding lands the sample on
    different templates and different slot fills, where a bad substitution actually hides.

    Deterministic, so re-exporting mid-review does not reshuffle the batch under someone who
    is halfway through it.
    """
    done = set(existing or {})
    batch: dict[str, tuple[Query, ...]] = {}
    for subset in Subset:
        queries = dataset.queries_in(subset)
        if not queries:
            continue
        needed = max(1, int(len(queries) * fraction))
        already = sum(1 for query in queries if not query.review_inherited)
        shortfall = needed - already
        if shortfall <= 0:
            continue
        candidates = [
            query
            for query in sorted(queries, key=lambda item: item.id)
            if query.review_inherited and query.id not in done
        ]
        if not candidates:
            continue
        step = max(1, len(candidates) // shortfall)
        picked = candidates[::step][:shortfall]
        batch[subset.value] = tuple(picked)
    return batch


def write_spot_check_batch(
    batch: Mapping[str, tuple[Query, ...]], *, directory: pathlib.Path | None = None
) -> pathlib.Path:
    """Write the whole batch as **one** file a reviewer fills in in place.

    One file rather than one per subset, deliberately: this is a short, mixed list — a few
    dozen rows total — and splitting it into eight files turns a single sitting into eight
    chores. The template bundles are per-subset because they are per-language judgements
    that different people take; a spot check is the same person confirming that the
    substitutions came out right.
    """
    root = directory or REVIEW_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / "spot-checks.yaml"

    rows: list[Mapping[str, Any]] = []
    for subset, queries in sorted(batch.items()):
        for query in queries:
            rows.append(
                {
                    "query_id": query.id,
                    "subset": subset,
                    "intent": query.intent,
                    "text": query.text,
                    "from_template": query.derived_from,
                    # ---- the reviewer fills in from here down ----
                    "decision": "pending",
                    "reviewed_by": None,
                    "reviewed_on": None,
                    "notes": None,
                }
            )

    total = sum(len(queries) for queries in batch.values())
    header = (
        "# SPOT-CHECK BATCH — individual judgements on generated queries.\n"
        "#\n"
        f"# {total} queries across {len(batch)} subset(s). This is the bound on\n"
        "# template-level review: approving one template validates the *phrasing* of\n"
        "# everything generated from it, but not whether each SUBSTITUTION reads naturally.\n"
        "# A frame that is fine for 'website development' can be wrong for 'cloud migration'.\n"
        "#\n"
        "# For each row, read `text` as a caller would say it and set `decision`:\n"
        "#\n"
        "#   approved   a caller would say this, exactly as written\n"
        "#   rejected   they would not — say why in `notes`\n"
        "#\n"
        "# There is deliberately no 'needs_edit'. A query is generated, so it cannot be\n"
        "# edited in place — the next build would overwrite it. If the wording is wrong,\n"
        "# the TEMPLATE is wrong: reject here and fix the template in the review bundle.\n"
        "#\n"
        "# `reviewed_by` and `reviewed_on` (YYYY-MM-DD) are REQUIRED on any decision other\n"
        "# than pending, and a model's name is refused. A review nobody can be asked about\n"
        "# is indistinguishable from no review.\n"
        "#\n"
        "# When done, copy the completed rows into source/spot_checks.yaml and rebuild.\n\n"
    )
    path.write_text(
        header + yaml.safe_dump({"spot_checks": rows}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def write_bundle(bundle: ReviewBundle, *, directory: pathlib.Path | None = None) -> pathlib.Path:
    """Write a bundle for a reviewer to edit in place."""
    root = directory or REVIEW_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"pending-{bundle.subset}.yaml"
    header = (
        f"# Review bundle: {bundle.subset}\n"
        f"#\n"
        f"# {bundle.template_count} templates covering {bundle.covered_queries} generated\n"
        f"# queries ({bundle.efficiency:.1f} queries per template reviewed).\n"
        "#\n"
        "# For each row: read `template_text`, look at `samples` to see what it actually\n"
        "# produced, then fill in `decision` and the questions below it.\n"
        "#\n"
        "#   approved    natural, means the same thing, correct script\n"
        "#   needs_edit  nearly right - put your replacement in `corrected_text`\n"
        "#   rejected    unusable as written; it needs rewriting, not patching\n"
        "#\n"
        "# `reviewed_by` and `reviewed_on` (YYYY-MM-DD) are REQUIRED on any decision other\n"
        "# than pending. A review nobody can be asked about is not a review.\n"
        "#\n"
        "# The most useful thing you can flag: a sentence that is grammatically correct and\n"
        "# that no caller would ever say. That is the failure this whole exercise exists to\n"
        "# catch, and it is invisible to everyone who does not speak the language.\n\n"
    )
    path.write_text(
        header
        + yaml.safe_dump(
            {"subset": bundle.subset, "templates": [row.as_mapping() for row in bundle.rows]},
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )
    return path


def apply_decisions(path: pathlib.Path) -> tuple[ReviewDecision, ...]:
    """Read a reviewed bundle and return the decisions, validated.

    Raises `ValueError` on an unattributable or incoherent decision. Refusing here rather
    than downstream matters: a decision that silently loses its reviewer becomes a
    `native_reviewed` stamp with nobody behind it, which is worse than `pending` because it
    stops anyone looking.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError(f"{path} is not a YAML mapping.")

    decisions: list[ReviewDecision] = []
    for row in document.get("templates") or ():
        template_id = str(row.get("template_id", "")).strip()
        if not template_id:
            raise ValueError(f"{path}: a row has no template_id.")
        decision = Decision(str(row.get("decision", Decision.PENDING.value)))
        if decision is Decision.PENDING:
            continue

        reviewer = str(row.get("reviewed_by") or "").strip()
        if is_placeholder_reviewer(reviewer):
            raise ValueError(
                f"{path}: template {template_id} is marked {decision.value} by "
                f"'{reviewer}', which is not a reviewer. A review nobody can be asked "
                "about is indistinguishable from no review."
            )
        reviewed_on = str(row.get("reviewed_on") or "").strip()
        if not reviewed_on:
            raise ValueError(
                f"{path}: template {template_id} is marked {decision.value} with no "
                "reviewed_on date."
            )

        corrected = _optional_str(row.get("corrected_text"))
        if decision is Decision.NEEDS_EDIT and not corrected:
            raise ValueError(
                f"{path}: template {template_id} is marked needs_edit but carries no "
                "corrected_text, so there is nothing to apply."
            )

        decisions.append(
            ReviewDecision(
                template_id=template_id,
                decision=decision,
                reviewed_by=reviewer,
                reviewed_on=reviewed_on,
                corrected_text=corrected,
                notes=_optional_str(row.get("notes")),
                natural=_optional_bool(row.get("natural")),
                semantically_equivalent=_optional_bool(row.get("semantically_equivalent")),
                script_correct=_optional_bool(row.get("script_correct")),
                code_mixing_natural=_optional_bool(row.get("code_mixing_natural")),
                grades_correct=_optional_bool(row.get("grades_correct")),
            )
        )

    for item in decisions:
        if item.decision is Decision.APPROVED and item.natural is False:
            # Approving something the same reviewer marked unnatural is a contradiction, and
            # it is the likeliest way an approval gets applied by accident.
            raise ValueError(
                f"{path}: template {item.template_id} is approved but marked not natural."
            )
    return tuple(decisions)


def merge_into_phrasebook(
    phrasebook_path: pathlib.Path, decisions: Sequence[ReviewDecision]
) -> int:
    """Write decisions back into `phrasebook.yaml`. Returns the number of templates updated.

    Edits in place and preserves everything it does not touch, so a reviewer's corrections
    and the original authoring survive in one file rather than diverging into two.
    """
    document = yaml.safe_load(phrasebook_path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError(f"{phrasebook_path} is not a YAML mapping.")

    by_id = {item.template_id: item for item in decisions}
    updated = 0
    templates = list(document.get("templates") or ())
    for row in templates:
        decision = by_id.get(str(row.get("id")))
        if decision is None:
            continue
        row["review_status"] = decision.resulting_state.value
        row["reviewed_by"] = decision.reviewed_by
        row["reviewed_on"] = decision.reviewed_on
        if decision.corrected_text:
            row["text"] = decision.corrected_text
        if decision.notes:
            row["review_notes"] = decision.notes
        updated += 1

    payload = {**document, "templates": templates}
    phrasebook_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return updated


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def template_ids(templates: Sequence[QueryTemplate]) -> tuple[str, ...]:
    return tuple(item.id for item in templates)
