"""Corpus workflow CLI. Deterministic, offline, spends nothing.

    # What has been supplied, what the corpus looks like, which gates pass.
    uv run python -m tests.d8_bakeoff.corpus.cli status

    # Build data/generated_*.yaml from source/. Needs both intake files.
    uv run python -m tests.d8_bakeoff.corpus.cli build

    # Export review bundles, one per subset, for native speakers to fill in.
    uv run python -m tests.d8_bakeoff.corpus.cli export-review

    # Read reviewed bundles and merge decisions back into source/phrasebook.yaml.
    uv run python -m tests.d8_bakeoff.corpus.cli apply-review

    # Export the individual-query spot-check batch (the bound on template review).
    uv run python -m tests.d8_bakeoff.corpus.cli export-spot-checks

`status` works with nothing supplied at all and says so plainly, which is the state the
repository is in today. Nothing here calls a network or a model.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from tests.d8_bakeoff.corpus.build import build_dataset, write_dataset
from tests.d8_bakeoff.corpus.source_material import (
    SOURCE_DIR,
    describe_missing,
    load_phrasebook,
    load_source_material,
    load_spot_checks,
)
from tests.d8_bakeoff.dataset import MaterialTier, PassageRole, Subset, load_dataset
from tests.d8_bakeoff.quality import SPOT_CHECK_FRACTION, evaluate_corpus, render_gates
from tests.d8_bakeoff.review import (
    REVIEW_DIR,
    apply_decisions,
    build_bundle,
    build_spot_check_batch,
    merge_into_phrasebook,
    write_bundle,
    write_spot_check_batch,
)


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _status() -> int:
    missing = describe_missing()
    _out("intake files:")
    for name in ("risenext.yaml", "phrasebook.yaml"):
        state = "MISSING - awaiting human input" if name in missing else "present"
        _out(f"  source/{name:20} {state}")
    _out()

    dataset = load_dataset()
    _out(
        f"corpus: {len(dataset.passages)} passages, {len(dataset.queries)} queries, "
        f"version {dataset.version}"
    )
    _out()
    _out("queries by subset (reviewed / total):")
    for subset in Subset:
        queries = dataset.queries_in(subset)
        readiness = dataset.readiness(subset)
        reviewed = len(queries) - len(readiness.unreviewed_queries)
        flag = "" if readiness.is_review_complete else "   AWAITING REVIEW"
        _out(f"  {subset.value:16} {reviewed:4} / {len(queries):<4}{flag}")
    _out()

    _out("passages by tier:")
    for tier in MaterialTier:
        count = sum(1 for passage in dataset.passages if passage.tier is tier)
        _out(f"  {tier.value:24} {count:4}")
    _out()
    _out("passages by role:")
    for role in PassageRole:
        count = sum(1 for passage in dataset.passages if passage.role is role)
        _out(f"  {role.value:24} {count:4}")
    _out()

    _out("corpus quality gates:")
    _out(render_gates(evaluate_corpus(dataset)))
    return 0


def _build() -> int:
    missing = describe_missing()
    if missing:
        _out(f"cannot build: {', '.join(missing)} not supplied.")
        _out(f"Copy the matching *.template.yaml in {SOURCE_DIR} and fill it in.")
        _out("See tests/d8_bakeoff/source/README.md.")
        # Non-zero: a build that silently produced nothing would look like success.
        return 1

    material = load_source_material()
    phrasebook = load_phrasebook()
    spot_checks = load_spot_checks()
    result = build_dataset(material, phrasebook, spot_checks=spot_checks)
    corpus_path, queries_path = write_dataset(result)

    _out(f"source material: {len(material.services)} services, {len(material.facts)} facts")
    _out(f"phrasebook: {len(phrasebook.templates)} templates")
    _out(f"spot checks: {len(spot_checks)} individual query judgements")
    _out(f"generated: {result.passage_count} passages, {result.query_count} queries")
    for subset, count in sorted(result.per_subset.items()):
        _out(f"  {subset:16} {count:4} queries")
    _out()
    for warning in result.warnings:
        _out(f"  warning: {warning}")
    if result.warnings:
        _out()
    _out(f"wrote {corpus_path}")
    _out(f"wrote {queries_path}")
    _out()
    _out("corpus quality gates:")
    _out(render_gates(evaluate_corpus(load_dataset())))
    return 0


def _export_review() -> int:
    if "phrasebook.yaml" in describe_missing():
        _out("cannot export: source/phrasebook.yaml not supplied.")
        return 1
    phrasebook = load_phrasebook()
    dataset = load_dataset()
    written = 0
    for subset in Subset:
        bundle = build_bundle(dataset, phrasebook, subset)
        if not bundle.rows:
            continue
        path = write_bundle(bundle)
        _out(
            f"  {subset.value:16} {bundle.template_count:3} templates covering "
            f"{bundle.covered_queries:4} queries  ->  {path.name}"
        )
        written += 1
    if not written:
        _out("no templates to review.")
        return 1
    _out()
    _out(f"wrote {written} bundle(s) to {REVIEW_DIR}")
    _out("Reviewers edit `decision`, `reviewed_by` and `reviewed_on` in place.")
    return 0


def _apply_review() -> int:
    bundles = sorted(REVIEW_DIR.glob("pending-*.yaml")) if REVIEW_DIR.is_dir() else []
    if not bundles:
        _out(f"no review bundles found in {REVIEW_DIR}.")
        return 1
    phrasebook_path = SOURCE_DIR / "phrasebook.yaml"
    if not phrasebook_path.is_file():
        _out("cannot apply: source/phrasebook.yaml not supplied.")
        return 1

    total = 0
    for path in bundles:
        decisions = apply_decisions(path)
        if not decisions:
            _out(f"  {path.name}: no decisions yet")
            continue
        updated = merge_into_phrasebook(phrasebook_path, decisions)
        _out(f"  {path.name}: {len(decisions)} decisions, {updated} templates updated")
        total += updated
    _out()
    _out(f"{total} template(s) updated. Rebuild so the corpus inherits the new review state:")
    _out("  uv run python -m tests.d8_bakeoff.corpus.cli build")
    return 0


def _export_spot_checks() -> int:
    """Write the one batch of queries that still need individual judgement.

    Separate from `export-review` because it answers a different question. A review bundle
    asks "is this phrasing natural?" about a template; a spot check asks "did the
    substitution come out right?" about a finished query — and only the second can catch a
    frame that works for "website development" and breaks for "cloud migration".
    """
    dataset = load_dataset()
    batch = build_spot_check_batch(
        dataset, fraction=SPOT_CHECK_FRACTION, existing=load_spot_checks()
    )
    if not batch:
        _out("every subset already meets the individual-review floor. Nothing to export.")
        return 0
    path = write_spot_check_batch(batch)
    for subset, queries in sorted(batch.items()):
        total = len(dataset.queries_in(Subset(subset)))
        _out(f"  {subset:16} {len(queries):3} queries to judge   (of {total} in the subset)")
    _out()
    _out(f"wrote {sum(len(q) for q in batch.values())} rows to {path}")
    _out("Reviewers set `decision` (approved/rejected), `reviewed_by`, `reviewed_on`.")
    _out("Then copy completed rows into source/spot_checks.yaml and rebuild.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.d8_bakeoff.corpus.cli",
        description="D-8 corpus workflow. Offline; makes no paid or network call.",
    )
    parser.add_argument(
        "command",
        choices=("status", "build", "export-review", "apply-review", "export-spot-checks"),
    )
    args = parser.parse_args(argv)
    return {
        "status": _status,
        "build": _build,
        "export-review": _export_review,
        "apply-review": _apply_review,
        "export-spot-checks": _export_spot_checks,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
