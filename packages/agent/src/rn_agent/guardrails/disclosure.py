"""AI-disclosure detection, en / hi / te.

**Be precise about what this is.** It is a *detector*, not a control. We cannot
constrain generated speech token by token, so this runs after the model has spoken
and reports whether the disclosure was present. AGENT_ARCHITECTURE §6 states that
gap openly and so does this module: absence raises a compliance finding on the call
and is a **hard failure in evaluation**, which is the strongest thing available. It
is not prevention.

Two pattern families, because the honest answer takes both:

* **AFFIRM_AI** — "I am an AI assistant". Counts, *unless negated*.
* **DENY_HUMAN** — "I am not a human". Counts, *only when negated*.

That split is the whole subtlety. Negation flips the two in opposite directions:
"I am not an AI" must not count as disclosure, while "I am not a human" must. A
matcher that treated negation as one rule gets one of them backwards, and the one
it gets backwards is a compliance failure that reads as a pass.

Coverage breadth for Hindi and Telugu is a **quality question settled by
evaluation, not by this table** (PRD **D-2**). The patterns here cover the
formulations we expect an agent instructed by the platform layer to produce, in
native script and in the romanised forms Indian callers and ASR both produce.
Nothing here claims to recognise every possible phrasing.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["DisclosureFinding", "DisclosureKind", "detect_disclosure", "has_ai_disclosure"]


def _nfc(text: str) -> str:
    """Normalise to NFC before matching.

    Devanagari nukta letters (`क़ ख़ ग़ ज़ ड़ ढ़ फ़ य़`) have a precomposed and a decomposed
    representation, and a regex does not treat them as equal. They are Unicode
    *composition exclusions*, so NFC does not compose them — it decomposes the
    precomposed form, which is what unifies the two. Applied to the patterns as well as
    the input, so the two agree whatever form this source file is saved in.

    Matters less here than in `optout` (a missed disclosure is a detected compliance
    finding, not a missed opt-out) but the two matchers should not differ in how they
    read text.
    """
    return unicodedata.normalize("NFC", text)


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a pattern, NFC-normalised to match `_nfc`-normalised input."""
    return re.compile(_nfc(pattern), re.IGNORECASE)


class DisclosureKind(StrEnum):
    """How the disclosure was made, when it was."""

    NONE = "none"
    AFFIRMED_AI = "affirmed_ai"
    """Said it is an AI / assistant / bot."""
    DENIED_HUMAN = "denied_human"
    """Said it is not a human. Equally valid, and common when answering directly."""


#: Words for the machine itself. `ai` needs word boundaries or it matches inside
#: "said", "again", "available"; `a\.i\.` is what a model writes when it is
#: spelling it out for speech.
_MACHINE = (
    r"(?:ai|a\.\s?i\.|artificial\s+intelligence|virtual\s+assistant|"
    r"ai\s+assistant|automated\s+assistant|automated\s+system|voice\s+assistant|"
    r"digital\s+assistant|computer\s+program|bot|chatbot)"
)

#: Words for a person. Kept separate from `_MACHINE` because the negation rule for
#: the two is opposite.
_HUMAN = r"(?:human(?:\s+being)?|real\s+person|person|man|woman|employee|human\s+agent)"

_EN_NEGATOR = r"(?:not|n[o']t|never)"

# A right-hand boundary that works where `\b` does not.
#
# `\b` is the wrong tool twice over in this file. It fails after an abbreviation
# ("A. I. assistant": a `.` followed by a space is two non-word characters, so there
# is no boundary between them), and it fails against Indic suffix particles, because
# Hindi and Telugu attach them inside the word — there is no boundary before `kandi`
# in `cheyyakandi`. `(?!\w)` asks the question that was actually meant: "the match
# does not continue into another word".
_END = r"(?!\w)"

# Affirmations. The negative lookahead is what keeps "I am not an AI" out.
_AFFIRM_AI: tuple[re.Pattern[str], ...] = (
    # English: "I'm an AI assistant", "this is an automated system", "I am a bot",
    # "I am an A. I. assistant".
    _compile(
        rf"\b(?:i\s*am|i'?m|this\s+is|you'?re\s+speaking\s+(?:to|with))\s+"
        rf"(?!{_EN_NEGATOR}\b)"
        rf"(?:an?\s+)?{_MACHINE}{_END}"
    ),
    # Hindi, Devanagari: "मैं एक AI असिस्टेंट हूँ", "मैं एक बोट हूं".
    #
    # A bounded gap rather than a character class between the noun and the copula.
    # `\w` does not cover Devanagari combining marks — the virama in "असिस्टेंट" and
    # the candrabindu in "हूँ" are both outside it — so a `[\w...]+` class silently
    # fails on exactly the words this pattern exists to match. The copula is still
    # required: it is what distinguishes "मैं AI हूं" from "AI के बारे में".
    _compile(
        r"मैं\s+(?:एक\s+)?(?:ए\s?आई|एआई|AI|आर्टिफिशियल\s+इंटेलिजेंस|"
        r"वर्चुअल\s+असिस्टेंट|असिस्टेंट|बोट|रोबोट|कंप्यूटर)"
        r"[^\n]{0,24}?(?:हूँ|हूं|हु|है)"
    ),
    # Hindi, romanised: "main ek AI assistant hoon", "mai bot hu".
    _compile(
        r"\bma(?:i|in|en)\b(?:\s+ek)?\s+(?:ai|a\.i\.|artificial\s+intelligence|"
        r"virtual\s+assistant|assistant|bot|robot)\b[^.\n]{0,24}?\bh(?:oon|un|u|oo|ai)\b"
    ),
    # Telugu, native script: "నేను ఒక AI అసిస్టెంట్‌ని", "నేను AI ని".
    _compile(
        r"నేను\s+(?:ఒక\s+)?(?:ఏఐ|AI|ఆర్టిఫిషియల్\s+ఇంటెలిజెన్స్|"
        r"వర్చువల్\s+అసిస్టెంట్|అసిస్టెంట్|బాట్|రోబో)"
    ),
    # Telugu, romanised: "nenu AI assistant ni", "nenu oka bot ni".
    _compile(
        r"\bnenu\b(?:\s+oka)?\s+(?:ai|a\.i\.|artificial\s+intelligence|"
        r"virtual\s+assistant|assistant|bot|robo)\b"
    ),
)

# Denials of humanity. Each pattern *requires* the negator, so "I am a human"
# cannot match one of these.
_DENY_HUMAN: tuple[re.Pattern[str], ...] = (
    _compile(rf"\b(?:i\s*am|i'?m)\s+{_EN_NEGATOR}\s+(?:an?\s+)?{_HUMAN}\b"),
    # "no, I'm not" / "I'm not, I'm an AI" is covered by _AFFIRM_AI; this catches
    # the bare denial: "I am not a real person."
    _compile(
        rf"\b(?:this\s+is|you'?re\s+(?:not\s+)?speaking\s+(?:to|with))\s+"
        rf"{_EN_NEGATOR}\s+(?:an?\s+)?{_HUMAN}\b"
    ),
    # Hindi: "मैं इंसान नहीं हूँ", "मैं मनुष्य नहीं हूं".
    _compile(r"मैं\s+(?:कोई\s+)?(?:इंसान|इन्सान|मनुष्य|आदमी|व्यक्ति)\s+नहीं"),
    # Hindi romanised: "main insaan nahin hoon", "mai insan nahi hu".
    _compile(
        r"\bma(?:i|in|en)\b\s+(?:koi\s+)?(?:insaan|insan|manushya|aadmi|admi)\s+"
        r"nah(?:i|in|ii|ीn)?\b"
    ),
    # Telugu: "నేను మనిషిని కాదు", "నేను మనుషుడు కాదు".
    _compile(r"నేను\s+(?:మనిషి(?:ని)?|మనుషు(?:డు|రాలు)?|వ్యక్తి)\s*(?:ని\s*)?కాదు"),
    # Telugu romanised: "nenu manishi ni kaadu", "nenu manushi kadu".
    _compile(r"\bnenu\b\s+(?:manishi|manushi|manushyudu|vyakti)\b[^.\n]{0,12}?\bka(?:a)?du\b"),
)


@dataclass(frozen=True, slots=True)
class DisclosureFinding:
    """Whether a turn disclosed that the speaker is an AI."""

    kind: DisclosureKind

    @property
    def disclosed(self) -> bool:
        return self.kind is not DisclosureKind.NONE


def detect_disclosure(text: str) -> DisclosureFinding:
    """Classify one assistant turn.

    Pure, allocation-light, and compiled once at import. Called on the first
    assistant turn of a call; not on every turn, because the obligation is to
    disclose up front, and re-announcing on every turn is not a better call.
    """
    if not text or not text.strip():
        return DisclosureFinding(kind=DisclosureKind.NONE)
    normalised = _nfc(text)
    if any(pattern.search(normalised) for pattern in _AFFIRM_AI):
        return DisclosureFinding(kind=DisclosureKind.AFFIRMED_AI)
    if any(pattern.search(normalised) for pattern in _DENY_HUMAN):
        return DisclosureFinding(kind=DisclosureKind.DENIED_HUMAN)
    return DisclosureFinding(kind=DisclosureKind.NONE)


def has_ai_disclosure(text: str) -> bool:
    """Convenience predicate over `detect_disclosure`."""
    return detect_disclosure(text).disclosed


def first_disclosing_turn(turns: Iterable[str]) -> int | None:
    """The index of the first turn that disclosed, or `None`.

    Useful in evaluation: "disclosed, but only on turn 4" is a different finding
    from "disclosed in the greeting", and a boolean cannot express it.
    """
    for index, turn in enumerate(turns):
        if has_ai_disclosure(turn):
            return index
    return None
