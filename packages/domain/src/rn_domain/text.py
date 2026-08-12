"""Script-aware text normalisation. Pure, stdlib only.

Lives in the domain because it is pure computation with no I/O — the same
justification `values.PhoneNumber` already relies on — and because everything above
needs it: ingestion normalises before chunking, the chunker normalises before
measuring, and the sanitisation matchers normalise before matching. Three copies of
"nearly the same normalisation" is how two of them drift.

**The trap this module exists for.** Phase 2's opt-out matcher was bitten twice by
Unicode, and both bugs are in scope here:

* **Composed vs decomposed forms.** The precomposed Devanagari nukta form of
  ``फ़ोन`` silently failed to match its decomposed equivalent. Any comparison,
  hash or match over Indic text must normalise to NFC first, and this is the only
  place that decision is made.
* **Combining marks are outside ``\\w``.** The virama in ``असिस्टेंट`` and the
  candrabindu in ``हूँ`` both are. Code that assumes a "letter" is one codepoint
  fails on exactly the words it was written for.

**ZWNJ and ZWJ are preserved, deliberately.** Stripping zero-width characters is the
standard advice and it is *wrong for Indic scripts*: U+200C (ZWNJ) and U+200D (ZWJ)
control conjunct formation in Devanagari and Telugu, so removing them changes which
glyph — and sometimes which word — the text represents. Only the zero-width
characters that carry no orthographic meaning are removed. This differs from phone
number normalisation, where stripping all of them is correct, because a phone number
has no orthography.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from typing import Final

__all__ = [
    "collapse_whitespace",
    "grapheme_clusters",
    "grapheme_length",
    "normalise_for_matching",
    "normalise_text",
    "split_paragraphs",
    "truncate_to_graphemes",
]

# ---------------------------------------------------------------------------
# Every codepoint below is named by its number, never written as a literal.
#
# Not a style preference. A literal U+200B in a source file is *invisible*: a
# reviewer cannot see it, a diff cannot show it, and an editor that "tidies
# whitespace" can delete it with nobody noticing — after which this module stops
# stripping the character it exists to strip, silently. Ruff refuses literal
# invisibles for that reason (PLE2515) and flags the visually-ambiguous spaces
# (RUF001).
#
# `chr(0x200B)` rather than a "​" escape because it survives every transport
# this file will ever take: a copy-paste, a patch, a terminal, a code-review UI.
# The comment carries the name so the reader never has to look one up.
# ---------------------------------------------------------------------------

#: Zero-width and directional characters that carry no orthographic meaning and are
#: safe to drop. **ZWNJ (U+200C) and ZWJ (U+200D) are deliberately absent** — see
#: the module docstring.
_INVISIBLE_TO_STRIP: Final[frozenset[str]] = frozenset(
    {
        chr(0x00AD),  # SOFT HYPHEN
        chr(0x200B),  # ZERO WIDTH SPACE
        chr(0x200E),  # LEFT-TO-RIGHT MARK
        chr(0x200F),  # RIGHT-TO-LEFT MARK
        chr(0x2060),  # WORD JOINER
        chr(0xFEFF),  # ZERO WIDTH NO-BREAK SPACE (BOM)
    }
)

#: Space-like characters folded to an ordinary space. Enumerated rather than derived
#: from `str.isspace()`, because that is also true of the newlines and tabs this
#: module keeps as structure.
_SPACE_LIKE: Final[frozenset[str]] = frozenset(
    {
        chr(0x00A0),  # NO-BREAK SPACE
        chr(0x2007),  # FIGURE SPACE
        chr(0x2009),  # THIN SPACE
        chr(0x202F),  # NARROW NO-BREAK SPACE
        chr(0x3000),  # IDEOGRAPHIC SPACE
    }
)

#: Viramas. A virama followed by a consonant forms a conjunct, so a grapheme
#: boundary must not fall between them. Devanagari (Hindi) and Telugu are the two
#: scripts this platform commits to; adding a script means adding its virama here.
_VIRAMA: Final[frozenset[str]] = frozenset(
    {
        chr(0x094D),  # DEVANAGARI SIGN VIRAMA
        chr(0x0C4D),  # TELUGU SIGN VIRAMA
    }
)

#: ZWNJ and ZWJ. Orthographically significant in Indic scripts — they control
#: conjunct formation — so they survive normalisation *and* they suppress a grapheme
#: boundary on both sides.
_JOINERS: Final[frozenset[str]] = frozenset(
    {
        chr(0x200C),  # ZERO WIDTH NON-JOINER
        chr(0x200D),  # ZERO WIDTH JOINER
    }
)

#: Unicode general categories for combining marks: non-spacing, spacing-combining,
#: enclosing. Devanagari and Telugu matras fall in Mn and Mc.
_COMBINING_CATEGORIES: Final[frozenset[str]] = frozenset({"Mn", "Mc", "Me"})

#: Categories dropped outright: control, format, private-use, surrogate. Newline,
#: tab and the joiners are admitted before this check runs.
_DISCARDED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Co", "Cs"})

_STRUCTURAL_WHITESPACE: Final[frozenset[str]] = frozenset({"\n", "\t"})


def normalise_text(value: str) -> str:
    """NFC-normalise and clean text while preserving paragraph structure.

    In order: NFC; normalise line endings; drop meaningless invisibles; fold
    space-like characters to a space; strip control and format characters other than
    newline, tab and the Indic joiners; collapse runs of spaces and tabs within a
    line; collapse three-or-more newlines to a single paragraph break; trim.

    What it deliberately does **not** do: casefold (a matching concern, not a storage
    concern), transliterate, or strip punctuation. Stored chunk text must remain the
    tenant's text — it is quoted back to a caller, and it must be the same bytes that
    were embedded.
    """
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    kept: list[str] = []
    for char in text:
        if char in _INVISIBLE_TO_STRIP:
            continue
        if char in _SPACE_LIKE:
            kept.append(" ")
            continue
        # Newline and tab are `Cc` and carry structure; the joiners are `Cf` and
        # carry orthography. Both are admitted before the category filter, which
        # would otherwise discard them.
        if char in _STRUCTURAL_WHITESPACE or char in _JOINERS:
            kept.append(char)
            continue
        if unicodedata.category(char) in _DISCARDED_CATEGORIES:
            continue
        kept.append(char)

    return _collapse_runs("".join(kept)).strip()


def normalise_for_matching(value: str) -> str:
    """Normalisation for *comparison*: `normalise_text` plus casefold.

    Separate from `normalise_text` because the two have different jobs, and folding
    them would mean either storing casefolded tenant text or matching against
    un-casefolded text. `str.casefold()` rather than `.lower()`: it handles the cases
    `.lower()` misses, and it is what the Phase-2 guardrail matchers use.
    """
    return normalise_text(value).casefold()


def collapse_whitespace(value: str) -> str:
    """Every run of whitespace, including newlines, folded to one space."""
    return " ".join(value.split())


def split_paragraphs(value: str) -> list[str]:
    """Split on blank lines, dropping empties.

    Paragraphs are the chunker's preferred boundary because they are the boundary the
    author already chose. Splitting on single newlines instead would shred wrapped
    prose into fragments that embed poorly.
    """
    return [block.strip() for block in value.split("\n\n") if block.strip()]


def grapheme_clusters(value: str) -> Iterator[str]:
    """Yield approximate grapheme clusters.

    **An approximation of UAX#29 tailored to the scripts this platform commits to**,
    not a full implementation — a full one needs a segmentation library, and the
    property actually required here is narrow: never split a matra, a nukta or a
    virama-conjunct away from the consonant it belongs to. A boundary is suppressed
    when the current character is a combining mark or a joiner, or when the previous
    character was a joiner or a virama.

    Stated as an approximation on purpose. Someone will eventually need real
    segmentation for emoji sequences or Thai; when they do, this docstring is the
    honest starting point rather than a surprise.
    """
    cluster: list[str] = []
    previous = ""
    for char in value:
        joins = (
            unicodedata.category(char) in _COMBINING_CATEGORIES
            or char in _JOINERS
            or previous in _JOINERS
            or previous in _VIRAMA
        )
        if cluster and not joins:
            yield "".join(cluster)
            cluster = []
        cluster.append(char)
        previous = char
    if cluster:
        yield "".join(cluster)


def grapheme_length(value: str) -> int:
    """How many grapheme clusters a string holds.

    Not `len()`. ``len("हूँ")`` is 3 and its grapheme length is 1, so a
    character-budgeted chunker gives Hindi and Telugu systematically smaller chunks
    than English for the same budget — which would make a per-language retrieval
    comparison measure chunk size rather than model quality. That is precisely the
    confound the D-8 bake-off must not have.
    """
    return sum(1 for _ in grapheme_clusters(value))


def truncate_to_graphemes(value: str, limit: int) -> str:
    """Truncate to at most `limit` grapheme clusters, never mid-cluster."""
    if limit <= 0:
        return ""
    out: list[str] = []
    for count, cluster in enumerate(grapheme_clusters(value)):
        if count >= limit:
            break
        out.append(cluster)
    return "".join(out)


def _collapse_runs(text: str) -> str:
    """Collapse spaces/tabs within a line, and 3+ newlines down to one paragraph break."""
    out: list[str] = []
    blank_run = 0
    for raw in text.split("\n"):
        line = " ".join(raw.split())
        if line:
            blank_run = 0
            out.append(line)
            continue
        blank_run += 1
        # One blank line is a paragraph break and is kept; further blanks add
        # nothing, and a long run of them would otherwise survive into a prompt.
        if blank_run == 1:
            out.append("")
    return "\n".join(out)
