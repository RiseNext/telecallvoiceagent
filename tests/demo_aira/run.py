"""The offline Aira retrieval demo.

    uv run python -m tests.demo_aira.run "what services does the company offer?"
    uv run python -m tests.demo_aira.run --results 5 "what is your refund policy?"

Builds the index from the reviewed D-8 corpus, then runs **one real scripted
conversation** — the real registry, the real dispatcher, the real conversation loop —
and prints what each layer actually produced: the index build report, the tool envelope
the model was given, and the assistant's turns.

Costs nothing and reaches nothing. No database, no network, no credential, no paid API.
The embedding model is `FakeEmbeddingProvider`, which is lexical and English-only in
practice, so **no number or ranking printed here is evidence about retrieval quality**;
D-8 is open and this demo does not inform it.

`sys.stdout.write` rather than `print`: ruff's T20 rule bans `print` repository-wide,
including here, because a stray debug print in a structured-logging codebase is noise
nobody attributes.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from rn_agent.tools.builtin.search import MAX_RESULTS
from rn_domain.text import truncate_to_graphemes
from tests.demo_aira.pipeline import (
    DEMO_DIMENSIONS,
    DemoRun,
    build_demo_tenant,
    run_demo_conversation,
)

#: How much of a retrieved passage the CLI prints. The passages are up to 1000
#: graphemes; a terminal transcript of three of them in full is unreadable. Only the
#: *display* is trimmed — the model was given the whole thing.
_PREVIEW_GRAPHEMES = 220


def _configure_stdout() -> None:
    """Make the corpus printable on a console that is not already UTF-8.

    A Windows console defaults to a legacy code page, and this corpus is Devanagari and
    Telugu as well as Latin — so the demo would die on a `UnicodeEncodeError` while
    printing the very content it exists to show. `errors="replace"` rather than
    `strict`: an unrenderable glyph should cost one character, not the whole run.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _write(line: str = "") -> None:
    sys.stdout.write(f"{line}\n")


def _rule(title: str) -> None:
    _write()
    _write(f"-- {title} " + "-" * max(0, 68 - len(title)))


async def _run(question: str, results: int) -> DemoRun:
    tenant = await build_demo_tenant()
    report = tenant.index.report

    _rule("index")
    _write(f"corpus passages       {tenant.corpus.passage_count}")
    _write(f"documents indexed     {report.documents}")
    _write(f"chunks indexed        {report.chunks_indexed}")
    _write(f"quarantined           {report.quarantined}  (instruction-shaped, withheld)")
    _write(f"price-flagged         {report.price_flagged}")
    _write(f"chunking policy       {report.chunking_policy_version}")
    _write(f"embedding model       {report.embedding_model} ({report.dimensions}d)")

    # Retrieval on its own, before the conversation, so the ranking is visible rather
    # than only implied by what the tool returned.
    _rule("retrieval")
    _write(f'query: "{question}"')
    _write()
    retrieved = await tenant.retriever.search(query=question, k=results)
    if not retrieved.chunks:
        _write("  (nothing matched)")
    for position, chunk in enumerate(retrieved.chunks, start=1):
        _write(f"  {position}. [{chunk.score:.3f}] {chunk.knowledge_base_name} · {chunk.chunk_id}")
        _write(f"     {truncate_to_graphemes(chunk.content, _PREVIEW_GRAPHEMES)}")
        if chunk.flags:
            _write(f"     flags: {', '.join(chunk.flags)}")
    if retrieved.underfilled:
        _write(f"  (underfilled: {len(retrieved.chunks)} of {retrieved.requested_k} requested)")

    run = await run_demo_conversation(question, tenant=tenant)

    _rule("conversation (real loop, real dispatcher, scripted model)")
    _write(f"stop reason           {run.result.stop_reason.value}")
    _write(f"disclosure            {run.result.disclosure.kind.value}")
    _write(f"tools called          {', '.join(run.result.tool_names_called) or '(none)'}")
    for record in run.result.tool_executions:
        _write(f"  {record.tool_name} -> {record.envelope.outcome.value}")
        for item in _envelope_results(record.envelope.data):
            _write(f"     · {item['source']}: {truncate_to_graphemes(item['content'], 120)}")
    _write()
    for turn in run.result.assistant_turns:
        _write(f"  aira: {turn}")
    _write()
    _write("  (the assistant's words above are scripted; the tool result above is real)")

    _rule("honesty")
    _write("FakeEmbeddingProvider is a character-trigram hasher with no semantic and no")
    _write("cross-script capability. It exercises the machinery; it measures no model.")
    _write("D-8 is open: no embedding model, width, column type or index has been chosen.")
    return run


def _envelope_results(data: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Pull `{content, source}` pairs out of an envelope, tolerating any other shape."""
    if not data:
        return []
    raw = data.get("results")
    if not isinstance(raw, list):
        return []
    return [
        {"content": str(item.get("content", "")), "source": str(item.get("source", ""))}
        for item in raw
        if isinstance(item, Mapping)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.demo_aira.run",
        description="Offline Aira retrieval demo. No network, no cost, no database.",
    )
    parser.add_argument("question", help="What to ask the agent.")
    parser.add_argument(
        "--results",
        type=int,
        default=3,
        choices=range(1, MAX_RESULTS + 1),
        help=f"How many passages to retrieve (1-{MAX_RESULTS}).",
    )
    args = parser.parse_args(argv)

    _configure_stdout()
    _write(f"Aira offline retrieval demo | fake embedder, {DEMO_DIMENSIONS}d | no network")
    asyncio.run(_run(args.question, args.results))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
