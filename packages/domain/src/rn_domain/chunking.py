"""Document chunking. Pure, deterministic, stdlib only.

A chunk boundary decides what a retrieved unit contains, and therefore what the
agent can answer from. That makes chunking a **policy**, not a utility — so it
lives in the domain, it is versioned, and the version is recorded alongside any
retrieval measurement.

**Why the policy is frozen before the D-8 bake-off.** Comparing embedding models
under different chunking measures chunking, not models. `FROZEN_CHUNKING_V1` is
therefore fixed for the whole bake-off, its version string goes into the result
artifact, and a chunk-size sweep runs *afterwards* with the winning model only. If
this policy changes, every previously recorded retrieval number becomes
incomparable — which is what the version string exists to make visible.

**Budgets are in grapheme clusters, not characters.** `len("हूँ")` is 3 and its
grapheme length is 1. A character-budgeted chunker gives Hindi and Telugu
systematically smaller chunks than English for the same budget, so a per-language
retrieval comparison would be measuring chunk size. See `rn_domain.text`.

**Boundary preference, most to least desirable:** paragraph, then sentence, then
word, then grapheme cluster. The last is a floor rather than a strategy — reaching
it means a single word exceeded the maximum, which happens with a URL or an
unbroken identifier, and splitting one is better than emitting a chunk the
embedding provider will refuse.

Overlap is deliberate duplication: a fact that straddles a boundary is otherwise
retrievable from neither side. The cost is that the overlapping text is stored and
embedded twice, which is a real cost paid for a real failure mode.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from rn_core.errors import InvariantViolation
from rn_domain.text import (
    grapheme_clusters,
    grapheme_length,
    normalise_text,
    split_paragraphs,
)

__all__ = [
    "FROZEN_CHUNKING_V1",
    "Chunk",
    "ChunkingPolicy",
    "chunk_document",
]

#: Sentence terminators, including the Indic ones.
#:
#: `।` (Devanagari danda, U+0964) and `॥` (double danda, U+0965) terminate sentences
#: in Hindi and appear in Telugu text too. A splitter that only knows `.` `!` `?`
#: treats an entire Hindi paragraph as one sentence, which then falls through to
#: word splitting and produces boundaries in the middle of clauses.
#:
#: Abbreviations (`Rs.`, `Dr.`) are deliberately **not** special-cased: an early
#: boundary is harmless because the packer immediately re-joins short sentences up
#: to the target, so the only cost of over-splitting is a little arithmetic.
_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?।॥])\s+")


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """A versioned chunking configuration. All budgets in grapheme clusters."""

    #: What the packer aims for. Chunks land at or just under this.
    target_graphemes: int
    #: Hard ceiling. An atom longer than this is split further.
    max_graphemes: int
    #: How much of the previous chunk's tail seeds the next one.
    overlap_graphemes: int
    #: Below this a trailing chunk is merged backwards rather than emitted alone.
    min_graphemes: int
    #: Recorded in every retrieval measurement. Change the policy, change this.
    version: str

    def __post_init__(self) -> None:
        if self.target_graphemes < 1:
            raise InvariantViolation("Chunk target must be positive.")
        if self.max_graphemes < self.target_graphemes:
            raise InvariantViolation("Chunk maximum cannot be below the target.")
        if not 0 <= self.overlap_graphemes < self.target_graphemes:
            # An overlap at or above the target would re-seed each chunk with at
            # least a full chunk of text and the packer would never advance.
            raise InvariantViolation(
                "Chunk overlap must be smaller than the target, or chunking cannot progress.",
                detail={
                    "overlap_graphemes": self.overlap_graphemes,
                    "target_graphemes": self.target_graphemes,
                },
            )
        if not 0 <= self.min_graphemes <= self.target_graphemes:
            raise InvariantViolation("Chunk minimum must lie between zero and the target.")
        if not self.version.strip():
            raise InvariantViolation("A chunking policy must carry a version.")


#: The policy frozen for the D-8 bake-off.
#:
#: Every number is a **starting default, not a measurement.** ~700 graphemes is a
#: few short paragraphs: large enough to carry the context an answer needs, small
#: enough that the result is quotable on a phone call rather than read out as an
#: essay. The 1000 ceiling keeps every chunk an order of magnitude inside the
#: embedding adapter's per-input limit, so no chunk can be refused at embed time.
FROZEN_CHUNKING_V1: Final[ChunkingPolicy] = ChunkingPolicy(
    target_graphemes=700,
    max_graphemes=1000,
    overlap_graphemes=100,
    min_graphemes=80,
    version="chunking-v1",
)


#: Below this many chunks there is no predecessor to merge a short tail into.
_MERGEABLE_MINIMUM: Final[int] = 2

#: Graphemes in the widest separator the packer inserts (`"\n\n"`). Budgeted for in
#: `_atom_ceiling` so a seeded chunk plus a separator plus an atom still fits.
_MAX_SEPARATOR: Final[int] = 2


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit of a document."""

    index: int
    text: str
    grapheme_count: int
    #: The policy version that produced it. Carried per chunk, not just per run,
    #: because a re-index can leave a document's chunks written by two different
    #: policies mid-migration and "which policy produced this row" must be
    #: answerable from the row.
    policy_version: str


@dataclass(frozen=True, slots=True)
class _Atom:
    """The smallest unit the packer will not split."""

    text: str
    length: int
    starts_paragraph: bool


def chunk_document(text: str, *, policy: ChunkingPolicy = FROZEN_CHUNKING_V1) -> tuple[Chunk, ...]:
    """Split a document into chunks. Pure and deterministic.

    Returns an empty tuple for text that normalises to nothing — a legitimate
    outcome for a document that held only whitespace or only stripped control
    characters, and one the ingestion pipeline must handle rather than treat as an
    error.

    Guarantees, each asserted by a test:

    * no chunk exceeds `policy.max_graphemes`;
    * no chunk is empty;
    * no grapheme cluster is split across chunks;
    * concatenating the chunks in order, with overlap removed, reproduces the
      normalised source — nothing is silently dropped.
    """
    normalised = normalise_text(text)
    if not normalised:
        return ()

    atoms = list(_atomise(normalised, policy))
    packed = _pack(atoms, policy)
    merged = _merge_short_tail(packed, policy)
    return tuple(
        Chunk(
            index=index,
            text=body,
            grapheme_count=grapheme_length(body),
            policy_version=policy.version,
        )
        for index, body in enumerate(merged)
    )


def _atomise(normalised: str, policy: ChunkingPolicy) -> Iterator[_Atom]:
    """Break the document into atoms no longer than `max_graphemes`."""
    for paragraph in split_paragraphs(normalised):
        first_in_paragraph = True
        for sentence in _SENTENCE_END.split(paragraph):
            trimmed = sentence.strip()
            if not trimmed:
                continue
            for piece in _fit(trimmed, _atom_ceiling(policy)):
                yield _Atom(
                    text=piece,
                    length=grapheme_length(piece),
                    starts_paragraph=first_in_paragraph,
                )
                first_in_paragraph = False


def _atom_ceiling(policy: ChunkingPolicy) -> int:
    """The longest a single atom may be, so a seeded chunk still fits under the max.

    **Not `policy.max_graphemes`, and the difference is a bug this comment exists to
    stop coming back.** A chunk is seeded with up to `overlap_graphemes` of the
    previous chunk's tail, then the packer appends one atom *unconditionally* — it has
    to, or an atom longer than the target could never be placed at all and the packer
    would spin. So the widest chunk the packer can build is

        overlap + separator + atom

    and bounding atoms by `max_graphemes` therefore allowed chunks of
    `max + overlap + 2`. A test caught it at 176 against a ceiling of 160.

    Subtracting the overlap and the widest separator makes the documented guarantee —
    no chunk exceeds `max_graphemes` — arithmetically true rather than nearly true.
    """
    return max(1, policy.max_graphemes - policy.overlap_graphemes - _MAX_SEPARATOR)


def _fit(sentence: str, ceiling: int) -> Iterator[str]:
    """Yield pieces of `sentence`, each within `ceiling` graphemes.

    Splits on words first; a single word over the ceiling is split on grapheme
    clusters, which is the floor described in the module docstring.
    """
    if grapheme_length(sentence) <= ceiling:
        yield sentence
        return

    current: list[str] = []
    current_length = 0
    for word in sentence.split(" "):
        word_length = grapheme_length(word)
        if word_length > ceiling:
            if current:
                yield " ".join(current)
                current, current_length = [], 0
            yield from _split_graphemes(word, ceiling)
            continue
        # +1 for the space that will join it to what is already buffered.
        projected = current_length + word_length + (1 if current else 0)
        if current and projected > ceiling:
            yield " ".join(current)
            current, current_length = [word], word_length
            continue
        current.append(word)
        current_length = projected
    if current:
        yield " ".join(current)


def _split_graphemes(word: str, ceiling: int) -> Iterator[str]:
    buffer: list[str] = []
    for cluster in grapheme_clusters(word):
        if len(buffer) >= ceiling:
            yield "".join(buffer)
            buffer = []
        buffer.append(cluster)
    if buffer:
        yield "".join(buffer)


def _pack(atoms: list[_Atom], policy: ChunkingPolicy) -> list[str]:
    """Greedily pack atoms up to the target, seeding each chunk with overlap."""
    chunks: list[str] = []
    buffer: list[tuple[str, str]] = []  # (separator, text)
    length = 0

    def flush() -> None:
        nonlocal buffer, length
        if not buffer:
            return
        body = _join(buffer)
        chunks.append(body)
        seed = _overlap_seed(body, policy.overlap_graphemes)
        buffer = [("", seed)] if seed else []
        length = grapheme_length(seed) if seed else 0

    for atom in atoms:
        separator = _separator(atom, seeded=bool(buffer))
        projected = length + len(separator) + atom.length
        if buffer and projected > policy.target_graphemes:
            flush()
            separator = _separator(atom, seeded=bool(buffer))
            projected = length + len(separator) + atom.length
        buffer.append((separator, atom.text))
        length = projected

    if buffer:
        chunks.append(_join(buffer))
    return [chunk for chunk in chunks if chunk.strip()]


def _separator(atom: _Atom, *, seeded: bool) -> str:
    """What joins this atom to what is already buffered.

    A paragraph break is preserved as one, because it is a boundary the author
    chose and it survives into the text a caller hears quoted back.
    """
    if not seeded:
        return ""
    return "\n\n" if atom.starts_paragraph else " "


def _join(parts: list[tuple[str, str]]) -> str:
    return "".join(f"{separator}{text}" for separator, text in parts).strip()


def _overlap_seed(body: str, overlap: int) -> str:
    """The tail of `body` that seeds the next chunk, snapped to a word boundary.

    Returns `""` when the overlap would cover the whole chunk, which would stop the
    packer making progress.
    """
    if overlap <= 0:
        return ""
    clusters = list(grapheme_clusters(body))
    if len(clusters) <= overlap:
        return ""
    tail = "".join(clusters[-overlap:])
    # Snap forward to the first space so the seed starts at a word rather than
    # mid-word; a mid-word fragment embeds as noise and is quoted back as gibberish.
    space = tail.find(" ")
    snapped = tail[space + 1 :] if space != -1 else tail
    return snapped.strip()


def _merge_short_tail(chunks: list[str], policy: ChunkingPolicy) -> list[str]:
    """Fold a too-short final chunk into its predecessor where it fits.

    A trailing 12-grapheme chunk retrieves badly — it has almost no context to embed
    — and it is a common artifact of a document whose length is just over a
    boundary. Merging is only done when the result still respects the ceiling;
    otherwise the short chunk stands, because a chunk over the maximum is worse.
    """
    if len(chunks) < _MERGEABLE_MINIMUM:
        return chunks
    last = chunks[-1]
    if grapheme_length(last) >= policy.min_graphemes:
        return chunks
    combined = f"{chunks[-2]} {last}"
    if grapheme_length(combined) > policy.max_graphemes:
        return chunks
    return [*chunks[:-2], combined]
