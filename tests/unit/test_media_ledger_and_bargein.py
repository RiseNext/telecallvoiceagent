"""Playback accounting and barge-in — the arithmetic that fails silently.

Everything here is asserted against a `ManualClock`, so the assertions are exact rather
than tolerant. A tolerant timing assertion is one that passes on a developer's machine
and fails in CI, and a *loose* one is worse: it would accept the over-reporting these
tests exist to forbid.

**The invariant that outranks every other assertion in this file:** `audio_end_ms` is
never greater than what the caller actually heard. Under-reporting makes the agent
repeat a sentence. Over-reporting makes it reference a sentence the caller never heard,
in a conversation that then cannot be recovered — with no error, no log line and
perfectly clean audio.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from rn_providers.audio.formats import PCM_8K, PCM_24K, ms_of_bytes
from rn_providers.fakes.realtime import (
    OPENAI_LIKE_CAPABILITIES,
    SARVAM_LIKE_CAPABILITIES,
    FakeRealtimeProvider,
)
from rn_providers.telephony.exotel import exotel_chunk_policy
from rn_voice.media.bargein import handle_barge_in
from rn_voice.media.clock import ManualClock
from rn_voice.media.ledger import PlaybackLedger
from rn_voice.media.ring import OutboundRingBuffer

pytestmark = pytest.mark.unit

GOLDEN = pathlib.Path(__file__).parent.parent / "fixtures" / "media" / "playback_ledger.json"

#: One 24 kHz chunk: 3840 bytes, exactly 80 ms.
CHUNK = exotel_chunk_policy(PCM_24K).effective_min
CHUNK_MS = ms_of_bytes(CHUNK, PCM_24K)


def _ledger(clock: ManualClock | None = None) -> tuple[PlaybackLedger, ManualClock]:
    the_clock = clock or ManualClock()
    return PlaybackLedger(fmt=PCM_24K, clock=the_clock), the_clock


def _enqueue(ledger: PlaybackLedger, *, item_id: str = "item-1", chunks: int = 1) -> None:
    for index in range(chunks):
        ledger.note_enqueued(item_id=item_id, content_index=0, byte_count=CHUNK)
        ledger.note_mark_written(f"{item_id}:{index}")


# ---------------------------------------------------------------------------
# Milliseconds, not bytes
# ---------------------------------------------------------------------------


def test_accounting_is_rate_invariant() -> None:
    """Milliseconds are rate-invariant so a resampler cannot corrupt them. The same
    duration at two rates is two byte counts and one number here."""
    clock = ManualClock()
    at_24k = PlaybackLedger(fmt=PCM_24K, clock=clock)
    at_8k = PlaybackLedger(fmt=PCM_8K, clock=clock)
    at_24k.note_enqueued(item_id="i", content_index=0, byte_count=PCM_24K.bytes_per_second)
    at_8k.note_enqueued(item_id="i", content_index=0, byte_count=PCM_8K.bytes_per_second)
    assert at_24k.enqueued_ms == at_8k.enqueued_ms == 1000.0


# ---------------------------------------------------------------------------
# The per-item reset
# ---------------------------------------------------------------------------


def test_a_new_item_resets_the_ledger() -> None:
    """**The one implementation detail to take from REALTIME_VOICE §3.** `truncate` takes
    an `audio_end_ms` relative to the start of *that item's* audio, so a global counter
    is a guaranteed, silent, unbounded corruption."""
    ledger, _ = _ledger()
    _enqueue(ledger, item_id="item-1", chunks=3)
    assert ledger.enqueued_ms == CHUNK_MS * 3

    _enqueue(ledger, item_id="item-2", chunks=1)

    assert ledger.item_id == "item-2"
    assert ledger.enqueued_ms == CHUNK_MS, "the second item must start from zero"
    assert ledger.confirmed_ms == 0.0
    assert ledger.resets == 2


def test_two_items_in_one_response_produce_two_ledgers() -> None:
    """The §3 bug, caught mechanically rather than by review."""
    ledger, _ = _ledger()
    _enqueue(ledger, item_id="item-1", chunks=2)
    _enqueue(ledger, item_id="item-2", chunks=2)
    assert ledger.resets == 2
    assert ledger.enqueued_ms == CHUNK_MS * 2


def test_a_content_index_change_also_resets() -> None:
    ledger, _ = _ledger()
    ledger.note_enqueued(item_id="i", content_index=0, byte_count=CHUNK)
    ledger.note_enqueued(item_id="i", content_index=1, byte_count=CHUNK)
    assert ledger.enqueued_ms == CHUNK_MS
    assert ledger.content_index == 1


# ---------------------------------------------------------------------------
# Marks are the only ground truth
# ---------------------------------------------------------------------------


def test_an_echoed_mark_confirms_everything_up_to_it() -> None:
    ledger, _ = _ledger()
    _enqueue(ledger, chunks=3)
    ledger.note_mark_echoed("item-1:1")
    assert ledger.confirmed_ms == CHUNK_MS * 2
    assert ledger.enqueued_ms == CHUNK_MS * 3


def test_an_unknown_mark_echo_is_ignored() -> None:
    """Exactly what arrives after a `clear` — the audio a mark was waiting on has been
    discarded — and also what a duplicate echo looks like. Neither ends a call."""
    ledger, _ = _ledger()
    _enqueue(ledger, chunks=1)
    ledger.note_mark_echoed("nonexistent")
    assert ledger.confirmed_ms == 0.0


def test_the_estimate_never_exceeds_what_was_written() -> None:
    """The `min()` clamp. The wall-clock extrapolation assumes the provider plays out in
    realtime, which is **[A] and unverified** — so the clamp makes the worst case "we
    under-report" rather than "we over-report"."""
    ledger, clock = _ledger()
    _enqueue(ledger, chunks=2)
    ledger.note_mark_echoed("item-1:0")

    clock.advance(10_000)  # far more time than the audio could possibly take

    assert ledger.estimate_played_ms() == ledger.enqueued_ms


def test_with_no_marks_at_all_the_estimate_still_biases_low() -> None:
    """The pathological case: the provider never echoes. `confirmed_ms` stays at zero and
    the estimate may only grow with the clock, never jump to `enqueued_ms`."""
    ledger, clock = _ledger()
    _enqueue(ledger, chunks=4)
    clock.advance(CHUNK_MS)
    estimate = ledger.estimate_played_ms()
    assert estimate == pytest.approx(CHUNK_MS)
    assert estimate < ledger.enqueued_ms


def test_the_lag_is_reported_as_a_health_metric() -> None:
    """`enqueued - confirmed` is the backpressure signal and the width of the
    uncertainty window."""
    ledger, _ = _ledger()
    _enqueue(ledger, chunks=3)
    ledger.note_mark_echoed("item-1:0")
    assert ledger.snapshot().lag_ms == CHUNK_MS * 2


def test_a_late_mark_is_recorded_as_divergence_not_as_a_correction() -> None:
    """A mark for audio we already truncated past must never retroactively raise the
    estimate — that is the over-reporting direction, arriving by the back door."""
    ledger, _clock = _ledger()
    _enqueue(ledger, chunks=3)
    frozen = ledger.freeze()
    ledger.note_mark_echoed("item-1:2")

    assert ledger.truncate_divergence_ms > 0
    assert ledger.estimate_played_ms() == frozen


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


def test_freezing_stops_the_estimate_advancing() -> None:
    """The pacer runs on another task and will happily advance `enqueued_ms` while the
    `clear` is being awaited. That is why the freeze comes first."""
    ledger, clock = _ledger()
    _enqueue(ledger, chunks=2)
    ledger.note_mark_echoed("item-1:0")
    frozen = ledger.freeze()

    clock.advance(500)
    _enqueue(ledger, item_id="item-1", chunks=2)

    assert ledger.estimate_played_ms() == frozen


def test_freezing_twice_returns_the_same_value() -> None:
    """A duplicated trigger must not inflate the number."""
    ledger, clock = _ledger()
    _enqueue(ledger, chunks=2)
    first = ledger.freeze()
    clock.advance(200)
    assert ledger.freeze() == first


def test_resume_releases_the_freeze() -> None:
    ledger, _clock = _ledger()
    _enqueue(ledger, chunks=2)
    ledger.freeze()
    ledger.resume()
    assert not ledger.is_frozen


# ---------------------------------------------------------------------------
# The golden ledger file
# ---------------------------------------------------------------------------


def test_a_recorded_delta_and_mark_schedule_produces_the_expected_ledger() -> None:
    """A deterministic tape diffed against a committed file.

    An accounting change that was not intentional shows up here as a diff, rather than
    as a confused caller three weeks later.
    """
    ledger, clock = _ledger()
    trace: list[dict[str, object]] = []

    schedule = [
        ("enqueue", "item-1", 0),
        ("enqueue", "item-1", 0),
        ("echo", "item-1:0", 0),
        ("advance", "", 40),
        ("enqueue", "item-1", 0),
        ("echo", "item-1:1", 0),
        ("advance", "", 120),
        ("enqueue", "item-2", 0),
        ("advance", "", 30),
        ("echo", "item-2:0", 0),
    ]
    marks_per_item: dict[str, int] = {}
    for action, argument, amount in schedule:
        if action == "enqueue":
            index = marks_per_item.get(argument, 0)
            marks_per_item[argument] = index + 1
            ledger.note_enqueued(item_id=argument, content_index=0, byte_count=CHUNK)
            ledger.note_mark_written(f"{argument}:{index}")
        elif action == "echo":
            ledger.note_mark_echoed(argument)
        else:
            clock.advance(amount)
        snapshot = ledger.snapshot()
        trace.append(
            {
                "action": action,
                "argument": argument,
                "item_id": snapshot.item_id,
                "enqueued_ms": round(snapshot.enqueued_ms, 3),
                "confirmed_ms": round(snapshot.confirmed_ms, 3),
                "estimate_ms": round(snapshot.estimate_ms, 3),
            }
        )

    produced = json.dumps(trace, indent=2, sort_keys=True)
    assert GOLDEN.is_file(), f"missing golden {GOLDEN}; write it from this test's output"
    assert produced == GOLDEN.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Barge-in
# ---------------------------------------------------------------------------


class _Clears:
    def __init__(self) -> None:
        self.count = 0
        self.order: list[str] = []

    async def __call__(self) -> None:
        self.count += 1
        self.order.append("clear")


async def test_barge_in_fires_all_three_operations_in_order() -> None:
    """**The Phase-4 barge-in criterion.** Exactly one clear, exactly one flush, exactly
    one truncate, in that order — and the freeze before any of them."""
    ledger, _clock = _ledger()
    buffer = OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K))
    buffer.append(b"\x11" * 5000, item_id="item-1")
    _enqueue(ledger, chunks=3)
    ledger.note_mark_echoed("item-1:1")
    session = FakeRealtimeProvider(capabilities=OPENAI_LIKE_CAPABILITIES)
    clears = _Clears()

    outcome = await handle_barge_in(
        ledger=ledger, buffer=buffer, session=session, clear_playback=clears
    )

    assert clears.count == 1
    assert buffer.buffered_bytes == 0
    assert outcome.flushed_bytes == 5000
    assert len(session.truncations) == 1
    assert session.truncations[0].item_id == "item-1"
    assert session.truncations[0].audio_end_ms == outcome.audio_end_ms
    assert ledger.is_frozen, "the ledger must be frozen before anything else ran"


async def test_the_reported_end_is_never_greater_than_what_was_played() -> None:
    """**The asymmetry, as a testable invariant.** Two chunks written, one confirmed, and
    almost no time elapsed: the truthful answer is one chunk, not two."""
    ledger, clock = _ledger()
    buffer = OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K))
    _enqueue(ledger, chunks=2)
    ledger.note_mark_echoed("item-1:0")
    clock.advance(5)
    session = FakeRealtimeProvider(capabilities=OPENAI_LIKE_CAPABILITIES)

    outcome = await handle_barge_in(
        ledger=ledger, buffer=buffer, session=session, clear_playback=_Clears()
    )

    truly_played = CHUNK_MS + 5
    assert outcome.audio_end_ms <= truly_played
    assert outcome.audio_end_ms <= ledger.enqueued_ms


async def test_the_reported_end_is_rounded_down() -> None:
    """Half a millisecond in the wrong direction is still the wrong direction."""
    ledger, clock = _ledger()
    _enqueue(ledger, chunks=1)
    ledger.note_mark_echoed("item-1:0")
    clock.advance(0.7)
    outcome = await handle_barge_in(
        ledger=ledger,
        buffer=OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K)),
        session=FakeRealtimeProvider(capabilities=OPENAI_LIKE_CAPABILITIES),
        clear_playback=_Clears(),
    )
    assert outcome.audio_end_ms == int(CHUNK_MS)


async def test_a_session_with_no_remote_state_is_cancelled_not_truncated() -> None:
    """The cascade has no remote conversation to correct — the context is ours. Unified
    at the *effect* level rather than pretending the mechanisms are the same."""
    ledger, _ = _ledger()
    _enqueue(ledger, chunks=2)
    session = FakeRealtimeProvider(capabilities=SARVAM_LIKE_CAPABILITIES)

    outcome = await handle_barge_in(
        ledger=ledger,
        buffer=OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K)),
        session=session,
        clear_playback=_Clears(),
    )

    assert session.truncations == []
    assert session.cancellations == 1
    assert outcome.cancelled


async def test_barge_in_before_any_audio_still_clears_and_cancels() -> None:
    """A caller who interrupts the greeting before a byte has been written. There is no
    item to truncate, so the model is stopped rather than corrected."""
    ledger, _ = _ledger()
    session = FakeRealtimeProvider(capabilities=OPENAI_LIKE_CAPABILITIES)
    clears = _Clears()

    outcome = await handle_barge_in(
        ledger=ledger,
        buffer=OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K)),
        session=session,
        clear_playback=clears,
    )

    assert clears.count == 1
    assert outcome.audio_end_ms == 0
    assert session.truncations == []
    assert session.cancellations == 1
