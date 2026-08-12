"""Script-aware normalisation and grapheme segmentation.

The tests that matter here are the Indic ones. `len()` on Devanagari or Telugu is not
a count of anything a human would recognise, and every assertion below that uses
`grapheme_length` exists because a character-budgeted chunker would give Hindi and
Telugu systematically smaller chunks than English — which would make the D-8
per-language comparison measure chunk size rather than model quality.
"""

from __future__ import annotations

import unicodedata

import pytest

from rn_domain.text import (
    collapse_whitespace,
    grapheme_clusters,
    grapheme_length,
    normalise_for_matching,
    normalise_text,
    split_paragraphs,
    truncate_to_graphemes,
)

pytestmark = [pytest.mark.unit]

ZWSP = chr(0x200B)
ZWNJ = chr(0x200C)
ZWJ = chr(0x200D)
NBSP = chr(0x00A0)
SOFT_HYPHEN = chr(0x00AD)
BOM = chr(0xFEFF)


def test_nfc_unifies_the_two_devanagari_nukta_forms() -> None:
    """The bug that bit the Phase-2 opt-out matcher, asserted here at the source.

    `फ़` has a precomposed form (U+095E) and a decomposed one (PHA + U+093C). They
    are Unicode composition exclusions, so NFC settles on the decomposed spelling - and
    without normalising, two spellings of the same word compare unequal and a matcher
    written against one silently never fires on the other.

    Built from codepoints rather than typed as two literals: an editor, a paste or a
    formatter will happily unify two identical-looking literals, and the test would
    then pass while asserting nothing at all.
    """
    suffix = chr(0x094B) + chr(0x0928)  # DEVANAGARI VOWEL SIGN O + LETTER NA
    precomposed = chr(0x095E) + suffix  # DEVANAGARI LETTER FA
    decomposed = chr(0x092B) + chr(0x093C) + suffix  # LETTER PHA + SIGN NUKTA

    assert precomposed != decomposed
    assert normalise_text(precomposed) == normalise_text(decomposed)
    # NFC *decomposes* this one, because the precomposed letter is a composition
    # exclusion. Asserting the direction stops a future "just use NFKC" from passing.
    assert normalise_text(precomposed) == decomposed


def test_meaningless_invisibles_are_stripped() -> None:
    assert normalise_text(f"he{ZWSP}llo") == "hello"
    assert normalise_text(f"{BOM}hello") == "hello"
    assert normalise_text(f"soft{SOFT_HYPHEN}hyphen") == "softhyphen"


def test_indic_joiners_survive_because_they_are_orthographic() -> None:
    """ZWNJ and ZWJ control conjunct formation, so stripping them changes the word.

    This is the case where the standard advice — "strip zero-width characters" — is
    actively wrong, and it is why `_INVISIBLE_TO_STRIP` enumerates codepoints instead
    of matching a category.
    """
    with_zwnj = f"वेब{ZWNJ}साइट"
    assert ZWNJ in normalise_text(with_zwnj)
    with_zwj = f"क{ZWJ}ष"
    assert ZWJ in normalise_text(with_zwj)


def test_space_like_characters_fold_to_a_plain_space() -> None:
    assert normalise_text(f"a{NBSP}b") == "a b"


def test_control_characters_are_dropped_but_newlines_and_tabs_survive() -> None:
    assert normalise_text("a\x00b") == "ab"
    assert normalise_text("a\x07b") == "ab"
    assert normalise_text("line\n\nnext") == "line\n\nnext"


def test_runs_of_whitespace_collapse_but_paragraphs_are_preserved() -> None:
    assert normalise_text("a     b") == "a b"
    assert normalise_text("one\n\n\n\n\ntwo") == "one\n\ntwo"
    assert normalise_text("  padded  ") == "padded"


def test_normalise_is_idempotent() -> None:
    """A second pass must change nothing, or chunking and matching could disagree
    about the same text depending on how many times it had been normalised."""
    messy = f"  Hello{NBSP}{ZWSP}world  \r\n\r\n\r\n  again\t\tnow  "
    once = normalise_text(messy)
    assert normalise_text(once) == once


def test_normalise_for_matching_casefolds_and_normalise_text_does_not() -> None:
    assert normalise_for_matching("HeLLo") == "hello"
    assert normalise_text("HeLLo") == "HeLLo"


def test_normalised_output_is_nfc() -> None:
    assert unicodedata.is_normalized("NFC", normalise_text("फ़ोन"))


def test_collapse_whitespace_flattens_newlines_too() -> None:
    assert collapse_whitespace("a\n\nb\tc") == "a b c"


def test_split_paragraphs_splits_on_blank_lines_only() -> None:
    assert split_paragraphs("one\ntwo\n\nthree") == ["one\ntwo", "three"]
    assert split_paragraphs("   ") == []


@pytest.mark.parametrize(
    ("text", "codepoints", "graphemes"),
    [
        ("hello", 5, 5),
        # हूँ — one consonant carrying two combining marks.
        ("हूँ", 3, 1),
        # असिस्टेंट — the word whose virama broke a Phase-2 pattern.
        ("असिस्टेंट", 9, 4),
        # Telugu వెబ్ — consonant, matra, consonant, virama.
        ("वेब्", 4, 2),
        ("", 0, 0),
    ],
)
def test_grapheme_length_is_not_codepoint_length(
    text: str, codepoints: int, graphemes: int
) -> None:
    assert len(text) == codepoints
    assert grapheme_length(text) == graphemes


def test_grapheme_clusters_reassemble_losslessly() -> None:
    """Segmentation must not lose or invent a character."""
    for text in ("hello", "हूँ असि", f"a{ZWJ}b", ""):
        assert "".join(grapheme_clusters(text)) == text


def test_truncate_to_graphemes_never_splits_a_cluster() -> None:
    devanagari = "हूँहूँ"  # हूँहूँ, two clusters
    assert grapheme_length(devanagari) == 2
    truncated = truncate_to_graphemes(devanagari, 1)
    assert grapheme_length(truncated) == 1
    # The surviving cluster keeps both of its combining marks; a codepoint-based
    # truncation would have produced a bare consonant.
    assert truncated == "हूँ"


def test_truncate_to_zero_or_negative_returns_empty() -> None:
    assert truncate_to_graphemes("hello", 0) == ""
    assert truncate_to_graphemes("hello", -3) == ""
