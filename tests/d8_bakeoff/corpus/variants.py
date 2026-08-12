"""Deterministic query variants. No model, no randomness, no network.

Everything here is a template substitution or a character edit, so a variant is
reproducible byte for byte and auditable by reading the function that made it. That
matters more than it sounds: a benchmark whose inputs cannot be regenerated identically
cannot be re-run to check a disputed result.

**What is generated here and what is not.**

| Axis | Generated mechanically | Supplied by a human |
|---|---|---|
| English paraphrase / terse / conversational | no — they are *phrasings*, and a mechanical paraphrase reads like a mechanical paraphrase | yes, as phrasebook templates |
| typo / noisy variants | **yes** — a transcription error is a character-level accident, not a phrasing choice | no |
| Hindi / Telugu, any script | no | yes, as phrasebook templates |
| code-mix | no | yes |
| cross-script pairing | **yes** — pairing a query with gold in another script is bookkeeping | the query text itself |

The line is: **mechanical where the transform is genuinely mechanical, human where it is a
judgement about how people speak.** Generating Hindi paraphrases from English by rule would
produce textbook Hindi nobody says, and the benchmark would then measure how well a model
handles textbook Hindi.

`typo_variants` is the one real generator, and it models the errors that actually reach a
retrieval query on this platform: **speech-to-text transcription noise**, not keyboard
slips. A caller does not typo on a phone call; the STT does. So the edits are
transposition, doubling, dropping and phonetic-neighbour substitution — and they are
deliberately conservative, because a variant mangled past recognition tests nothing except
whether the model can read gibberish.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from rn_domain.text import grapheme_clusters, normalise_text

__all__ = [
    "MAX_TYPO_EDITS",
    "TypoKind",
    "Variant",
    "cross_script_targets",
    "detect_script",
    "is_latin_script",
    "typo_variants",
]

#: Never more than this many edits per variant. Two is enough to be realistically noisy;
#: beyond that the query stops being the query and the test stops meaning anything.
MAX_TYPO_EDITS: Final[int] = 2

#: Minimum length before a typo variant is worth generating at all. Perturbing a
#: three-character query produces a different query, not a noisy one.
MIN_TYPO_LENGTH: Final[int] = 8


class TypoKind(StrEnum):
    """Which transcription accident this variant models."""

    TRANSPOSE = "transpose"
    """Adjacent characters swapped — the commonest STT and typing error."""

    DOUBLE = "double"
    """A character repeated. Frequent on elongated vowels in Indian speech."""

    DROP = "drop"
    """A character lost. What a clipped or noisy line produces."""

    PHONETIC = "phonetic"
    """A phonetically adjacent substitution — the error an STT actually makes, as opposed
    to a keyboard-adjacency error, which it never does."""


#: Phonetic confusions an Indian-English STT plausibly produces. Latin script only:
#: applying character substitutions to Devanagari or Telugu risks producing a different
#: word or an invalid cluster, and judging that needs a speaker, not a table.
_PHONETIC_NEIGHBOURS: Final[dict[str, str]] = {
    "v": "w",
    "w": "v",
    "s": "sh",
    "z": "j",
    "d": "dh",
    "t": "th",
    "p": "f",
    "b": "bh",
    "k": "c",
    "g": "gh",
}


@dataclass(frozen=True, slots=True)
class Variant:
    """One generated variant, with enough provenance to audit it."""

    text: str
    kind: TypoKind
    #: Stable suffix for the generated id, derived from the transform rather than a
    #: counter, so regenerating the corpus does not renumber everything.
    slug: str


#: Unicode block name fragment -> the `Script` it maps to.
_SCRIPT_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("DEVANAGARI", "devanagari"),
    ("TELUGU", "telugu"),
    ("LATIN", "latin"),
)

#: Share of letters that must belong to one script before the text is called that script.
_DOMINANCE: Final[float] = 0.6


def detect_script(text: str) -> str:
    """The dominant script of `text`, as a `dataset.Script` value.

    **Derived from the text, never from which subset the text sits in.** That distinction is
    the whole point for the cross-script subset: its queries are deliberately written in a
    script that differs from their gold passages', so reading the script off a per-subset
    table produced queries whose "cross-script" gold filter matched everything and therefore
    filtered out all of it. A test caught it as zero cross-script queries generated.

    Returns `"mixed"` when no script reaches `_DOMINANCE`, which is the honest answer for
    genuinely code-mixed text rather than a coin flip between two scripts.
    """
    letters = [char for char in normalise_text(text) if char.isalpha()]
    if not letters:
        return "latin"
    counts: dict[str, int] = {}
    for char in letters:
        name = unicodedata.name(char, "")
        for marker, script in _SCRIPT_MARKERS:
            if marker in name:
                counts[script] = counts.get(script, 0) + 1
                break
    if not counts:
        return "latin"
    winner, count = max(counts.items(), key=lambda item: item[1])
    return winner if count / len(letters) >= _DOMINANCE else "mixed"


def is_latin_script(text: str) -> bool:
    """Whether text is predominantly Latin.

    Used to gate the character-level generators: they are safe on romanised and English
    text and unsafe on Devanagari or Telugu, where dropping or swapping a codepoint can
    detach a matra from its consonant and produce something no speaker would utter. The
    check is on letters only, so digits and punctuation do not sway it.
    """
    letters = [char for char in normalise_text(text) if char.isalpha()]
    if not letters:
        return False
    latin = sum(1 for char in letters if "LATIN" in unicodedata.name(char, ""))
    return latin / len(letters) > 0.8


def typo_variants(text: str, *, limit: int = 2) -> tuple[Variant, ...]:
    """Deterministic transcription-noise variants of `text`.

    Returns an empty tuple for text that is too short, or for non-Latin script — see
    `is_latin_script` for why the character-level edits are not applied to Devanagari or
    Telugu. Positions are chosen by hashing the input, so the same query always yields the
    same variants across runs, processes and machines (`hashlib`, not `hash()`, which is
    salted per process).
    """
    normalised = normalise_text(text)
    if len(normalised) < MIN_TYPO_LENGTH or not is_latin_script(normalised):
        return ()

    produced: list[Variant] = []
    for kind in (TypoKind.TRANSPOSE, TypoKind.PHONETIC, TypoKind.DOUBLE, TypoKind.DROP):
        if len(produced) >= min(limit, MAX_TYPO_EDITS):
            break
        mutated = _apply(normalised, kind)
        if mutated and mutated != normalised:
            produced.append(Variant(text=mutated, kind=kind, slug=kind.value))
    return tuple(produced)


def _position(text: str, salt: str, span: int) -> int:
    """A stable position inside `text`, derived from its content.

    `blake2b` rather than `hash()`: `hash()` is salted per process for `str`, so a
    `hash()`-derived position would give different variants in two interpreters and the
    corpus would not be reproducible.
    """
    digest = hashlib.blake2b(f"{salt}:{text}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % max(1, span)


def _phonetic(text: str, interior: Sequence[int]) -> str | None:
    candidates = [index for index in interior if text[index].lower() in _PHONETIC_NEIGHBOURS]
    if not candidates:
        return None
    at = candidates[_position(text, TypoKind.PHONETIC.value, len(candidates))]
    return text[:at] + _PHONETIC_NEIGHBOURS[text[at].lower()] + text[at + 1 :]


def _transpose(text: str, at: int) -> str | None:
    if at + 1 >= len(text) or text[at] == text[at + 1]:
        return None
    return text[:at] + text[at + 1] + text[at] + text[at + 2 :]


def _double(text: str, at: int) -> str | None:
    return text[:at] + text[at] + text[at:]


def _drop(text: str, at: int) -> str | None:
    # Never drop a combining mark: on Latin text there are none, and the guard means this
    # stays safe if the script gate is ever relaxed.
    if unicodedata.category(text[at]) in {"Mn", "Mc", "Me"}:
        return None
    return text[:at] + text[at + 1 :]


#: One edit function per kind, so adding a kind cannot silently fall through to a default.
_EDITS: Final[dict[TypoKind, Callable[[str, int], str | None]]] = {
    TypoKind.TRANSPOSE: _transpose,
    TypoKind.DOUBLE: _double,
    TypoKind.DROP: _drop,
}


def _apply(text: str, kind: TypoKind) -> str | None:
    # Word-interior positions only. An edit to the first or last character of a query is
    # both less realistic and more likely to change the query's meaning outright.
    interior = [index for index, _ in enumerate(text) if 0 < index < len(text) - 1]
    if not interior:
        return None
    if kind is TypoKind.PHONETIC:
        return _phonetic(text, interior)
    at = interior[_position(text, kind.value, len(interior))]
    return _EDITS[kind](text, at)


def cross_script_targets(
    query_script: str, candidates: Sequence[tuple[str, str]]
) -> tuple[str, ...]:
    """Passage ids whose script differs from the query's — the cross-script gold set.

    Args:
        query_script: the query's script name.
        candidates: `(passage_id, passage_script)` pairs.

    Pure bookkeeping, which is exactly why it is mechanical: pairing a query with gold in
    another script is a selection, not a judgement about language. Returning only
    differing-script passages is what stops the hardest subset being quietly padded with
    same-script cases that would inflate its score.
    """
    return tuple(passage_id for passage_id, script in candidates if script != query_script)


def iter_graphemes(text: str) -> Iterator[str]:
    """Re-exported for callers that need cluster-safe iteration over a variant."""
    return grapheme_clusters(text)
