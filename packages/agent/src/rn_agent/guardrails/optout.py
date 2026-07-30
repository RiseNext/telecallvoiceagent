"""Opt-out recognition, en / hi / te. A code path independent of the model.

This runs over every final caller transcript **whatever the model does**. If the
model misses "don't call me again", or hears it and carries on selling, this still
fires. That independence is the point: an opt-out that only works when the model
cooperates is not a compliance control.

**What Phase 2 delivers is the matcher.** Recognition, in three languages, native
script and romanised, with negation handled. What it does *not* deliver is the
durable consequence: writing a `suppressions` row and blocking future dialling is
Phase 9, where `record_opt_out` and the pre-dial gate arrive. A hit here is a
finding for the caller of this function to act on; nothing is persisted.

**Negation is the trap, and it is not symmetric with disclosure.** "don't stop
calling me" contains "stop calling". "I didn't say stop calling" contains it too.
A substring or naive-regex matcher opts out a customer who asked for the opposite,
and the customer never hears from the business again. So negated forms are matched
*first* and win outright.

Coverage breadth is an evaluation question (PRD **D-2**), not a claim made here.
The table covers the formulations we expect from Indian callers on the phone; it
will need extending against real transcripts, and the extension belongs with
measured recall rather than with guesses.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["OptOutFinding", "OptOutLanguage", "detect_opt_out", "is_opt_out"]


def _nfc(text: str) -> str:
    """Normalise to NFC before matching.

    **Devanagari nukta letters have two representations that are not interchangeable
    to a regex.** `फ़` is either U+095E or `फ` + U+093C (nukta), and the same applies to
    क़ ख़ ग़ ज़ ड़ ढ़ य़ — the letters in `फ़ोन` ("phone") and `दफ़्तर`, which is exactly the
    vocabulary an opt-out uses. They are Unicode *composition exclusions*, so NFC does
    not compose them; it decomposes the precomposed form, which is what unifies the two.

    Without this, a transcript using the precomposed form silently fails to match and
    **an opt-out is missed** — the worst failure direction available here, because the
    business keeps calling someone who asked them to stop. Applied to the patterns as
    well as the input, so the two agree whatever form this source file is saved in.
    """
    return unicodedata.normalize("NFC", text)


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a pattern, NFC-normalised to match `_nfc`-normalised input."""
    return re.compile(_nfc(pattern), re.IGNORECASE)


class OptOutLanguage(StrEnum):
    """Which pattern family matched. Recorded for evaluation, never for routing."""

    ENGLISH = "en"
    HINDI = "hi"
    TELUGU = "te"


# ---------------------------------------------------------------------------
# Negated forms. Checked FIRST; a match here means "not an opt-out", full stop.
#
# Python's `re` requires fixed-width lookbehind, so a negative lookbehind for a
# variable-length negator is not available. A separate pass is not a workaround —
# it is clearer: "these phrasings are the opposite of an opt-out" reads as a rule.
# ---------------------------------------------------------------------------
#: A right-hand boundary that works where `\b` does not.
#:
#: Hindi and Telugu attach particles *inside* the word: there is no word boundary
#: before `kandi` in `cheyyakandi` or before `vaddu` in `cheyyavaddu`, so a `\b`
#: there matches nothing and the pattern silently never fires. `(?!\w)` asks what was
#: actually meant — "the match does not continue into another word".
_END = r"(?!\w)"

#: A left-hand boundary for Devanagari, where `\b` is also unreliable.
#:
#: `ना` is a suffix of `करना`, so `\bना` never matches — but worse, *omitting* the
#: boundary makes `ना` match inside `करना`, which turned "कॉल करना बंद करो" (a genuine
#: opt-out) into a negated one and dropped it. Requiring whitespace or start-of-string
#: is explicit and correct for a standalone negator.
_START = r"(?:^|\s)"

_NEGATED: tuple[re.Pattern[str], ...] = (
    # English: "don't stop calling", "never stop calling me", "didn't say stop"
    _compile(
        r"\b(?:do\s*n[o']?t|don't|dont|do\s+not|never|did\s*n[o']?t|didn't|didnt|"
        r"was\s*n[o']?t|wasn't|not)\b[^.?!\n]{0,30}?"
        r"\b(?:stop|remove|unsubscribe|opt\s*out|delete)\b"
    ),
    # "keep calling me", "please do call me" — an explicit opposite.
    _compile(r"\b(?:keep|continue|carry\s+on)\b[^.?!\n]{0,12}?\bcall"),
    # Hindi, negator first: "मत बंद करो", "mat band karo".
    _compile(rf"{_START}(?:मत|नहीं|ना)\s*(?:बंद|रोक|हटा)"),
    _compile(r"\b(?:mat|nahi|nahin|na)\b\s*(?:band|rok|hata)"),
    # Hindi, negator second: "band mat karo", "बंद मत करो". Both orders occur in
    # speech, and matching only one leaves the other to be *accidentally* unmatched
    # by the opt-out patterns rather than deliberately refused.
    _compile(r"(?:बंद|रोक|हटा)\w*\s*(?:मत|ना|नहीं)"),
    _compile(r"\b(?:band|rok|hata)\w*\s+(?:mat|na|nahi|nahin)\b"),
    # Telugu: "ఆపవద్దు" (do not stop), "ఆపకండి".
    _compile(r"(?:ఆప|తీసివేయ|తొలగించ)\s*(?:వద్దు|కండి|కు)"),
    _compile(rf"\b(?:aap|theesivey|tholagin)\w*?(?:vaddu|vadhu|kandi){_END}"),
)


# ---------------------------------------------------------------------------
# Opt-out forms, per language.
# ---------------------------------------------------------------------------
_ENGLISH: tuple[re.Pattern[str], ...] = (
    # "stop calling me", "stop calling", "stop ringing me"
    _compile(r"\bstop\b[^.?!\n]{0,12}?\b(?:calling|call|ringing|contacting|phoning)\b"),
    # "don't call me again", "do not call me", "never call me again"
    _compile(
        r"\b(?:do\s*n[o']?t|don't|dont|do\s+not|never)\b[^.?!\n]{0,16}?"
        r"\b(?:call|ring|phone|contact)\b"
    ),
    # "remove me from your list", "take me off the list", "delete my number"
    _compile(
        r"\b(?:remove|take)\s+(?:me|my\s+(?:number|name|details))\b[^.?!\n]{0,24}?"
        r"\b(?:list|database|records?|off)\b"
    ),
    _compile(r"\b(?:delete|erase)\s+my\s+(?:number|details|data|record)"),
    _compile(r"\bunsubscribe\b"),
    _compile(r"\bopt(?:\s|-)?out\b"),
    _compile(r"\b(?:add|put)\s+me\s+(?:on|to)\b[^.?!\n]{0,20}?\bdo\s*not\s*call\b"),
    _compile(r"\bdo\s*not\s*(?:call|disturb)\s*(?:list|registry)\b"),
)

_HINDI: tuple[re.Pattern[str], ...] = (
    # Devanagari: "मुझे कॉल न करें", "दोबारा फ़ोन मत करो", "कॉल करना बंद करो"
    _compile(r"(?:कॉल|काल|फ़ोन|फोन|संपर्क)\s*(?:मत|ना|न)\s*(?:करें|करो|कीजिए|करना)"),
    _compile(r"(?:कॉल|काल|फ़ोन|फोन)\s*(?:करना\s*)?(?:बंद|बन्द)\s*(?:करें|करो|कीजिए)"),
    _compile(r"(?:दोबारा|फिर\s*से|आगे)\s*(?:कॉल|काल|फ़ोन|फोन)\s*(?:मत|ना|न|नहीं)"),
    _compile(r"(?:सूची|लिस्ट)\s*से\s*(?:मुझे\s*)?(?:हटा|निकाल)"),
    _compile(r"परेशान\s*(?:मत|ना|न)\s*(?:करें|करो)"),
    # Romanised: "mujhe call na karein", "phone mat karo", "call band karo"
    _compile(
        r"\b(?:call|kall|phone|fon|sampark)\b\s*(?:mat|na|naa|nahi|nahin)\s*"
        r"\b(?:karo|karein|kare|kariye|karna)\b"
    ),
    _compile(r"\b(?:call|phone)\b\s*(?:karna\s*)?\bband\b\s*\b(?:karo|karein|kare|kariye)\b"),
    _compile(r"\b(?:dobara|dubara|phir\s*se|aage)\b[^.?!\n]{0,12}?\b(?:mat|na|nahi|nahin)\b"),
    _compile(r"\b(?:list|suchi|soochi)\b\s*se\s*(?:mujhe\s*)?\b(?:hata|nikal)"),
    _compile(r"\bpareshan\b\s*(?:mat|na|nahi)\s*\b(?:karo|karein|kare)\b"),
)

_TELUGU: tuple[re.Pattern[str], ...] = (
    # Native script: "నాకు కాల్ చేయకండి", "ఇక కాల్ చేయవద్దు", "లిస్ట్ నుంచి తీసేయండి"
    _compile(r"(?:కాల్|ఫోన్|కాల)\s*(?:చేయ|చెయ్య)\s*(?:కండి|వద్దు|కు|కూడదు)"),
    _compile(r"(?:ఇక|మళ్ళీ|మళ్లీ|తిరిగి)\s*(?:కాల్|ఫోన్)\s*(?:చేయ|చెయ్య)\s*(?:కండి|వద్దు)"),
    _compile(r"(?:జాబితా|లిస్ట్|లిస్టు)\s*(?:నుంచి|నుండి)\s*(?:నన్ను\s*)?(?:తీసి|తొలగించ|తీసేయ)"),
    _compile(r"(?:ఇబ్బంది|డిస్టర్బ్)\s*(?:చేయ|చెయ్య)\s*(?:కండి|వద్దు)"),
    _compile(r"నా\s*(?:నంబర్|నెంబర్)\s*(?:ను\s*)?(?:తీసి|తొలగించ|డిలీట్)"),
    # Romanised: "naaku call cheyyakandi", "inka call cheyyavaddu".
    #
    # The refusal particle is attached to the verb, so the boundary is `_END` on the
    # right and nothing on the left. A `\b` before `kandi` matches nothing at all —
    # a pattern that reads correctly and never fires.
    _compile(
        r"\b(?:call|phone|kaal)\b\s*(?:che|chey|cheyy|chesi)\w*?"
        rf"(?:kandi|kandhi|vaddu|vadhu|koodadu|kudadu){_END}"
    ),
    _compile(
        r"\b(?:inka|inkaa|malli|marali|tirigi)\b[^.?!\n]{0,16}?"
        rf"(?:vaddu|vadhu|kandi|kandhi){_END}"
    ),
    _compile(r"\b(?:list|jaabitha|jabitha)\b[^.?!\n]{0,12}?\b(?:teesi|tolagin|teeseyy?)"),
    _compile(rf"\bibbandi\b[^.?!\n]{{0,12}}?\w*?(?:cheyyakandi|vaddu|kandi){_END}"),
)

_FAMILIES: tuple[tuple[OptOutLanguage, tuple[re.Pattern[str], ...]], ...] = (
    (OptOutLanguage.ENGLISH, _ENGLISH),
    (OptOutLanguage.HINDI, _HINDI),
    (OptOutLanguage.TELUGU, _TELUGU),
)


@dataclass(frozen=True, slots=True)
class OptOutFinding:
    """Whether a caller asked not to be contacted again."""

    matched: bool
    language: OptOutLanguage | None = None
    #: The matched span, for evaluation and for a human reviewing a disputed call.
    #: Bounded and taken from the caller's own words — this is transcript content
    #: and must be handled as such: never logged, never put on a span.
    excerpt: str | None = None


_NO_MATCH = OptOutFinding(matched=False)

#: Longest excerpt retained. Enough to see what matched, short enough that it is
#: not a way to smuggle a transcript into a structure that gets serialised.
_MAX_EXCERPT = 120


def detect_opt_out(text: str) -> OptOutFinding:
    """Test one final caller utterance for an opt-out request.

    Pure and compiled at import. Runs per utterance rather than per word because on
    the cascaded provider path there are no interim transcripts at all (HC-20), so
    an utterance is the smallest unit that exists.
    """
    if not text or not text.strip():
        return _NO_MATCH
    # Normalised once, here, so every pattern below sees the same representation.
    # See `_nfc`: without it a precomposed Devanagari nukta letter silently fails to
    # match and an opt-out is missed.
    normalised = _nfc(text)
    # A negated phrasing wins outright. "Don't stop calling me" is not an opt-out,
    # and getting this backwards silently ends a customer relationship.
    if any(pattern.search(normalised) for pattern in _NEGATED):
        return _NO_MATCH
    for language, patterns in _FAMILIES:
        for pattern in patterns:
            match = pattern.search(normalised)
            if match:
                return OptOutFinding(
                    matched=True, language=language, excerpt=match.group(0)[:_MAX_EXCERPT]
                )
    return _NO_MATCH


def is_opt_out(text: str) -> bool:
    """Convenience predicate over `detect_opt_out`."""
    return detect_opt_out(text).matched
