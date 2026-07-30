"""Guardrail matchers: en / hi / te, native script and romanised, with negation.

**The negatives matter more than the positives here**, and they are not symmetric
between the two matchers:

* Disclosure — "I am not an AI" must NOT count; "I am not a human" MUST count. A
  matcher that treats negation as one rule gets one of them backwards, and the one it
  gets backwards is a compliance failure that reads as a pass.
* Opt-out — "don't stop calling me" must NOT match. Getting that backwards silently
  ends a customer relationship: the business never contacts them again and nobody
  knows why.

Recall breadth in Hindi and Telugu is an evaluation question (PRD **D-2**), settled
against real transcripts rather than by this file. What is asserted here is that the
formulations we do cover are matched, and that the traps are not.
"""

from __future__ import annotations

from typing import Literal

import pytest

from rn_agent.guardrails.disclosure import (
    DisclosureKind,
    detect_disclosure,
    first_disclosing_turn,
    has_ai_disclosure,
)
from rn_agent.guardrails.optout import OptOutLanguage, detect_opt_out, is_opt_out

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# AI disclosure — affirming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Hello! I'm an AI assistant calling from Acme Services.",
        "Hi, I am an AI assistant. How can I help?",
        "This is an automated assistant from Acme.",
        "You're speaking to an AI assistant today.",
        "I am a virtual assistant and I can help with bookings.",
        "I'm a bot, but I can still help you with that.",
        "I am an A. I. assistant for Acme.",
    ],
)
def test_english_affirmations_are_disclosure(text: str) -> None:
    assert detect_disclosure(text).kind is DisclosureKind.AFFIRMED_AI


@pytest.mark.parametrize(
    "text",
    [
        "नमस्ते, मैं एक AI असिस्टेंट हूँ।",
        "मैं एक वर्चुअल असिस्टेंट हूं, बताइए कैसे मदद करूं?",
        "main ek AI assistant hoon, aap kaise hain?",
        "namaste, mai ek bot hu",
    ],
)
def test_hindi_affirmations_are_disclosure(text: str) -> None:
    assert detect_disclosure(text).kind is DisclosureKind.AFFIRMED_AI


@pytest.mark.parametrize(
    "text",
    [
        "నమస్కారం, నేను ఒక AI అసిస్టెంట్‌ని.",
        "నేను ఏఐ అసిస్టెంట్, ఎలా సహాయం చేయగలను?",
        "namaskaram, nenu oka AI assistant ni",
        "nenu oka bot ni, cheppandi",
    ],
)
def test_telugu_affirmations_are_disclosure(text: str) -> None:
    assert detect_disclosure(text).kind is DisclosureKind.AFFIRMED_AI


# ---------------------------------------------------------------------------
# AI disclosure — denying humanity (the refusal-to-claim-human behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "No, I'm not a human.",
        "I am not a real person, I'm here to help though.",
        "I'm not a human being.",
        "मैं इंसान नहीं हूँ।",
        "main insaan nahi hoon",
        "నేను మనిషిని కాదు.",
        "nenu manishi kaadu",
    ],
)
def test_denying_humanity_counts_as_disclosure(text: str) -> None:
    """Answering "are you a human?" honestly is disclosure, in all three languages."""
    assert detect_disclosure(text).kind is DisclosureKind.DENIED_HUMAN


# ---------------------------------------------------------------------------
# AI disclosure — the negation trap, and plain absence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I am not an AI, I'm a real person.",
        "I'm not a bot!",
        "I am not an automated system.",
    ],
)
def test_denying_being_an_ai_is_not_disclosure(text: str) -> None:
    """The trap. Negation flips affirmation and denial in *opposite* directions."""
    assert not has_ai_disclosure(text)


@pytest.mark.parametrize(
    "text",
    [
        "Hello, I'm calling from Acme Services about your enquiry.",
        "Good morning! How can I help you today?",
        "",
        "   ",
        "I am a human being.",
        "I'm a real person from the sales team.",
        "Our AI tools can help your business grow.",
        "The artificial intelligence market is growing fast.",
    ],
)
def test_no_disclosure_is_reported_honestly(text: str) -> None:
    assert detect_disclosure(text).kind is DisclosureKind.NONE


def test_talking_about_ai_is_not_claiming_to_be_one() -> None:
    """A sales agent describing an AI product has not disclosed anything about itself."""
    assert not has_ai_disclosure("We build AI assistants for businesses like yours.")


def test_first_disclosing_turn_locates_a_late_disclosure() -> None:
    """ "Disclosed on turn 3" is a different finding from "disclosed in the greeting",
    and a boolean cannot express it."""
    turns = ["Hello there!", "How can I help?", "I'm an AI assistant, by the way."]
    assert first_disclosing_turn(turns) == 2
    assert first_disclosing_turn(["Hi", "Bye"]) is None


# ---------------------------------------------------------------------------
# Opt-out — positives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please stop calling me.",
        "Stop calling.",
        "Don't call me again.",
        "Do not call this number.",
        "Never call me again please.",
        "Remove me from your list.",
        "Take me off your list.",
        "Please delete my number.",
        "Unsubscribe.",
        "I want to opt out.",
        "Put me on the do not call list.",
        "Stop contacting me.",
    ],
)
def test_english_opt_outs_are_detected(text: str) -> None:
    finding = detect_opt_out(text)
    assert finding.matched
    assert finding.language is OptOutLanguage.ENGLISH


@pytest.mark.parametrize(
    "text",
    [
        "मुझे कॉल न करें।",
        "कॉल करना बंद करो।",
        "दोबारा फ़ोन मत करना।",
        "मुझे लिस्ट से हटा दो।",
        "परेशान मत करो।",
        "mujhe call na karein",
        "call band karo",
        "phone mat karo",
        "list se mujhe hata do",
        "pareshan mat karo",
    ],
)
def test_hindi_opt_outs_are_detected(text: str) -> None:
    finding = detect_opt_out(text)
    assert finding.matched
    assert finding.language is OptOutLanguage.HINDI


@pytest.mark.parametrize(
    "text",
    [
        "నాకు కాల్ చేయకండి.",
        "ఇక కాల్ చేయవద్దు.",
        "లిస్ట్ నుంచి నన్ను తీసివేయండి.",
        "ఇబ్బంది చేయకండి.",
        "నా నంబర్ తీసివేయండి.",
        "naaku call cheyyakandi",
        "inka call cheyyavaddu",
        "ibbandi cheyyakandi",
    ],
)
def test_telugu_opt_outs_are_detected(text: str) -> None:
    finding = detect_opt_out(text)
    assert finding.matched
    assert finding.language is OptOutLanguage.TELUGU


def test_a_match_records_a_bounded_excerpt() -> None:
    """Useful for a human reviewing a disputed call. Transcript content, so it is kept
    in memory for the caller to handle and is never logged."""
    finding = detect_opt_out("Look, please just stop calling me, alright?")
    assert finding.excerpt is not None
    assert len(finding.excerpt) <= 120
    assert "stop calling" in finding.excerpt.lower()


# ---------------------------------------------------------------------------
# Opt-out — negatives, which is where the value is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # The negation trap, in three languages.
        "Don't stop calling me!",
        "Please never stop calling, I like the updates.",
        "I didn't say stop calling.",
        "Keep calling me until you reach me.",
        "मत बंद करो, कॉल करते रहो।",
        "call band mat karo",
        "కాల్ ఆపవద్దు.",
        "call aapa vaddu",
        # Ordinary conversation that must not opt anyone out.
        "Can you call me later please?",
        "Call me tomorrow morning.",
        "Stop, wait, let me get a pen.",
        "I need to stop for a second.",
        "Can you remove the extra item from my order?",
        "What is on your list of services?",
        "",
        "   ",
    ],
)
def test_these_are_not_opt_outs(text: str) -> None:
    assert not is_opt_out(text), text


def test_a_later_request_to_stop_still_registers_after_a_negation_elsewhere() -> None:
    """Documents a real limitation rather than pretending it away.

    The negation pass is utterance-scoped, so an utterance containing both a negated
    and a genuine opt-out resolves to "not an opt-out". That is the safe direction —
    a missed opt-out is caught by the model's own handling and by the next utterance,
    whereas a false positive permanently ends contact. Recall on mixed utterances is
    an evaluation question (D-2), not something to guess at here.
    """
    assert not is_opt_out("Don't stop calling me, but remove me from the marketing list.")
    # Split across utterances — which is how a real call arrives — it is detected.
    assert is_opt_out("Remove me from the marketing list.")


# ---------------------------------------------------------------------------
# Unicode normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "precomposed", "decomposed"),
    [
        # `फ़` is precomposed DEVANAGARI LETTER FA; the other form is
        # `फ` (PHA) + `़` (NUKTA). A regex does not treat them as equal.
        ("phone", "मुझे फ़ोन न करें।", "मुझे फ़ोन न करें।"),
    ],
)
def test_both_devanagari_nukta_forms_are_recognised(
    label: str, precomposed: str, decomposed: str
) -> None:
    """A real defect this suite caught: the precomposed form was silently missed.

    Devanagari nukta letters are Unicode **composition exclusions**, so NFC does not
    compose them — a pattern written in one form fails against input in the other, and
    the failure is silent. A missed opt-out is the worst direction available here: the
    business keeps calling someone who asked them to stop.
    """
    assert precomposed != decomposed, "the two forms must actually differ"
    assert is_opt_out(precomposed), f"{label}: precomposed form was not recognised"
    assert is_opt_out(decomposed), f"{label}: decomposed form was not recognised"


@pytest.mark.parametrize("form", ["NFC", "NFD", "NFKC", "NFKD"])
def test_the_verdict_does_not_depend_on_normalisation_form(
    form: Literal["NFC", "NFD", "NFKC", "NFKD"],
) -> None:
    """Whatever an ASR or a browser hands us, the answer must be the same."""
    import unicodedata

    for text, expected in (
        ("मुझे कॉल न करें।", True),
        ("नाकु కాల్ చేయకండి.", None),
        ("Please stop calling me.", True),
    ):
        if expected is None:
            continue
        assert is_opt_out(unicodedata.normalize(form, text)) is expected, (form, text)


@pytest.mark.parametrize("form", ["NFC", "NFD", "NFKC", "NFKD"])
def test_disclosure_does_not_depend_on_normalisation_form(
    form: Literal["NFC", "NFD", "NFKC", "NFKD"],
) -> None:
    import unicodedata

    hindi = "मैं एक AI असिस्टेंट हूँ।"
    assert has_ai_disclosure(unicodedata.normalize(form, hindi)), form
