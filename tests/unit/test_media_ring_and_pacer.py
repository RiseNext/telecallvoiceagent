"""The ring buffer, the aligner and the pacer.

The sweep below is the test that pays for itself. Model deltas arrive at **arbitrary**
sizes, so the interesting input space is "any sequence of byte counts" and a handful of
hand-picked examples will always miss the boundary that matters.

**It is a seeded sweep, not Hypothesis.** `hypothesis` is referenced by TESTING.md as
the eventual tool for this and is **not currently installed**, and adding a dependency
mid-phase to write one test is not a trade worth making. What replaces it is stronger
than random luck in the way that counts: every boundary the chunk policy actually has —
floor - 1, floor, floor + 1, one alignment unit, the ceiling, the ceiling + 1 — is
enumerated **explicitly**, and the pseudo-random portion runs from fixed seeds so a
failure reproduces exactly rather than "sometimes". What is genuinely lost is
Hypothesis's shrinking; a failing case here is reported at whatever size produced it.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable

import pytest

from rn_providers.audio.formats import PCM_8K, PCM_16K, PCM_24K, AudioFormat, ms_of_bytes
from rn_providers.telephony.exotel import exotel_chunk_policy
from rn_voice.media.align import SampleAligner
from rn_voice.media.clock import ManualClock, SystemClock
from rn_voice.media.ledger import PlaybackLedger
from rn_voice.media.pacer import Pacer, PacerSinks
from rn_voice.media.ring import OutboundRingBuffer

pytestmark = pytest.mark.unit

RATES = [PCM_8K, PCM_16K, PCM_24K]


class _Recorder:
    """Collects what the pacer wrote, and how long it asked to sleep."""

    def __init__(self) -> None:
        self.media: list[bytes] = []
        self.marks: list[str] = []
        self.slept_ms: list[float] = []
        self.order: list[str] = []

    def sinks(self) -> PacerSinks:
        return PacerSinks(send_media=self._media, send_mark=self._mark, sleep=self._sleep)

    async def _media(self, payload: bytes) -> None:
        self.media.append(payload)
        self.order.append("media")

    async def _mark(self, name: str) -> None:
        self.marks.append(name)
        self.order.append("mark")

    async def _sleep(self, milliseconds: float) -> None:
        self.slept_ms.append(milliseconds)


def _pacer(
    fmt: AudioFormat,
    *,
    clock: ManualClock | None = None,
    lead_chunks: int = 2,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[OutboundRingBuffer, PlaybackLedger, Pacer, _Recorder]:
    policy = exotel_chunk_policy(fmt)
    buffer = OutboundRingBuffer(policy=policy)
    the_clock = clock or ManualClock()
    ledger = PlaybackLedger(fmt=fmt, clock=the_clock)
    recorder = _Recorder()
    sinks = recorder.sinks()
    if sleep is not None:
        sinks = PacerSinks(send_media=sinks.send_media, send_mark=sinks.send_mark, sleep=sleep)
    pacer = Pacer(
        buffer=buffer,
        ledger=ledger,
        fmt=fmt,
        clock=the_clock,
        sinks=sinks,
        lead_chunks=lead_chunks,
    )
    return buffer, ledger, pacer, recorder


# ---------------------------------------------------------------------------
# The alignment sweep
# ---------------------------------------------------------------------------


def _boundary_sizes(fmt: AudioFormat) -> list[int]:
    """Every delta size where the policy's behaviour changes, at this rate.

    Enumerated rather than sampled: these are exactly the values a random generator
    reaches by luck, and they are the ones that break an off-by-one in the aligner.
    """
    policy = exotel_chunk_policy(fmt)
    alignment = policy.effective_alignment
    return [
        0,
        2,
        alignment - 2,
        alignment,
        alignment + 2,
        policy.effective_min - 2,
        policy.effective_min,
        policy.effective_min + 2,
        policy.effective_min + alignment,
        policy.effective_max - 2,
        policy.effective_max,
        policy.effective_max + 2,
        policy.effective_max * 2 + alignment,
    ]


def _drain_and_assert(fmt: AudioFormat, deltas: list[int]) -> tuple[int, int]:
    """Push `deltas` through a buffer, asserting legality on every emission.

    Returns `(real bytes in, real bytes out)` so the caller can also assert conservation.
    """
    policy = exotel_chunk_policy(fmt)
    buffer = OutboundRingBuffer(policy=policy)
    written = 0
    real_out = 0
    for size in deltas:
        # Even byte counts only: an odd count would split an s16le sample, which the
        # transcoder refuses upstream.
        even = max(0, size - size % 2)
        written += even
        buffer.append(b"\x00" * even, item_id="item-1")
        while (chunk := buffer.take_chunk()) is not None:
            assert policy.is_legal(len(chunk.payload))
            assert chunk.real_bytes == len(chunk.payload)
            real_out += chunk.real_bytes
    while (chunk := buffer.take_final()) is not None:
        assert policy.is_legal(len(chunk.payload)), "the padded final chunk must be legal too"
        assert chunk.real_bytes <= len(chunk.payload)
        real_out += chunk.real_bytes
    assert buffer.buffered_bytes == 0
    return written, real_out


@pytest.mark.parametrize("fmt", RATES, ids=lambda f: f"{f.rate_hz}")
def test_every_policy_boundary_emits_a_legal_chunk(fmt: AudioFormat) -> None:
    """**The Phase-4 alignment criterion**, at every boundary the policy has."""
    for size in _boundary_sizes(fmt):
        written, real_out = _drain_and_assert(fmt, [size])
        assert real_out == written


@pytest.mark.parametrize("fmt", RATES, ids=lambda f: f"{f.rate_hz}")
def test_arbitrary_delta_sequences_emit_only_legal_chunks(fmt: AudioFormat) -> None:
    """The same invariant over sequences, from fixed seeds so a failure reproduces.

    Deltas up to 9000 bytes span "far below the floor" to "several chunks at once" at
    all three rates, which is the range a real response actually produces.
    """
    for seed in range(40):
        rng = random.Random(seed)
        deltas = [rng.randint(0, 9000) for _ in range(rng.randint(1, 60))]
        written, real_out = _drain_and_assert(fmt, deltas)
        assert real_out == written, f"audio was dropped (seed={seed}, rate={fmt.rate_hz})"


@pytest.mark.parametrize("fmt", RATES, ids=lambda f: f"{f.rate_hz}")
def test_a_sequence_of_boundary_sizes_emits_only_legal_chunks(fmt: AudioFormat) -> None:
    """Boundaries in sequence, where a residue left by one interacts with the next."""
    sizes = _boundary_sizes(fmt)
    for seed in range(20):
        rng = random.Random(seed)
        deltas = [rng.choice(sizes) for _ in range(rng.randint(2, 25))]
        written, real_out = _drain_and_assert(fmt, deltas)
        assert real_out == written, f"audio was dropped (seed={seed}, rate={fmt.rate_hz})"


def test_no_real_audio_is_ever_dropped() -> None:
    """Byte conservation through the buffer, stated on its own because it is the
    invariant a listening test cannot see: a silently dropped tail sounds like nothing."""
    written, real_out = _drain_and_assert(PCM_24K, [4001, 12, 999, 40000, 3])
    assert real_out == written


# ---------------------------------------------------------------------------
# Padding
# ---------------------------------------------------------------------------


def test_a_short_tail_is_padded_to_the_floor_and_the_padding_is_not_counted() -> None:
    """**The subtle one.** Padding is played, but it is not the model's audio. Counting
    it in the ledger would inflate `audio_end_ms` — the over-reporting direction."""
    policy = exotel_chunk_policy(PCM_24K)
    buffer = OutboundRingBuffer(policy=policy)
    buffer.append(b"\x11" * 900, item_id="item-1")

    assert buffer.take_chunk() is None, "900 bytes is below the 3840-byte floor"
    chunk = buffer.take_final()
    assert chunk is not None
    assert len(chunk.payload) == policy.effective_min
    assert chunk.real_bytes == 900
    assert chunk.padding_bytes == policy.effective_min - 900
    assert chunk.payload[:900] == b"\x11" * 900
    assert set(chunk.payload[900:]) == {0}, "padding must be digital silence"


def test_a_full_final_chunk_is_not_padded() -> None:
    policy = exotel_chunk_policy(PCM_24K)
    buffer = OutboundRingBuffer(policy=policy)
    buffer.append(b"\x11" * policy.effective_min, item_id="item-1")
    chunk = buffer.take_final()
    assert chunk is not None
    assert chunk.padding_bytes == 0


def test_a_new_item_discards_the_previous_item_s_partial_audio() -> None:
    """Two items' audio must never share a chunk: the ledger accounts per item and a
    mixed chunk could not be attributed to either."""
    buffer = OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K))
    buffer.append(b"\x11" * 900, item_id="item-1")
    buffer.append(b"\x22" * 4000, item_id="item-2")
    chunk = buffer.take_chunk()
    assert chunk is not None
    assert chunk.item_id == "item-2"
    assert b"\x11" not in chunk.payload


def test_flush_discards_everything_and_reports_how_much() -> None:
    buffer = OutboundRingBuffer(policy=exotel_chunk_policy(PCM_24K))
    buffer.append(b"\x00" * 5000, item_id="item-1")
    assert buffer.flush() == 5000
    assert buffer.buffered_bytes == 0
    assert buffer.take_chunk() is None


# ---------------------------------------------------------------------------
# The pacer
# ---------------------------------------------------------------------------


async def test_a_mark_follows_every_chunk_in_order() -> None:
    """One mark per chunk, not per utterance: it bounds the barge-in uncertainty window
    to a single chunk instead of a whole response."""
    fmt = PCM_24K
    buffer, _, pacer, recorder = _pacer(fmt)
    buffer.append(b"\x00" * (exotel_chunk_policy(fmt).effective_min * 3), item_id="item-1")

    written = await pacer.drain()

    assert written == 3
    assert len(recorder.media) == len(recorder.marks) == 3
    assert recorder.order == ["media", "mark"] * 3


async def test_the_ledger_is_told_only_the_real_bytes() -> None:
    fmt = PCM_24K
    buffer, ledger, pacer, _ = _pacer(fmt)
    buffer.append(b"\x11" * 900, item_id="item-1")

    await pacer.drain(final=True)

    assert ledger.enqueued_ms == pytest.approx(ms_of_bytes(900, fmt))
    assert ledger.enqueued_ms < ms_of_bytes(exotel_chunk_policy(fmt).effective_min, fmt)


async def test_the_pacer_sleeps_rather_than_dumping_the_whole_response() -> None:
    """The pacing decision. Dumping everything would make the barge-in uncertainty
    window the whole utterance instead of one or two chunks."""
    fmt = PCM_24K
    clock = ManualClock()
    policy = exotel_chunk_policy(fmt)
    buffer, _, pacer, recorder = _pacer(fmt, clock=clock, lead_chunks=2)

    async def advancing_sleep(milliseconds: float) -> None:
        # A sleep that moves the injected clock, so the pacer makes progress without any
        # wall-clock time passing at all.
        clock.advance(milliseconds)
        recorder.slept_ms.append(milliseconds)

    buffer, _, pacer, recorder = _pacer(fmt, clock=clock, lead_chunks=2, sleep=advancing_sleep)
    buffer.append(b"\x00" * (policy.effective_min * 10), item_id="item-1")

    written = await pacer.drain()

    assert written == 10
    assert recorder.slept_ms, "a ten-chunk response must not be dumped in one go"
    # The lead is two chunks, so the pacer only sleeps once it is more than two chunks
    # ahead — the first two go out immediately.
    assert len(recorder.slept_ms) <= 8


async def test_a_shallow_lead_bounds_the_uncertainty_window() -> None:
    """80-160 ms at 24 kHz is what makes the ~200 ms barge-in requirement achievable."""
    fmt = PCM_24K
    policy = exotel_chunk_policy(fmt)
    lead_ms = ms_of_bytes(policy.effective_min * 2, fmt)
    assert lead_ms == 160.0


async def test_a_lead_below_one_chunk_is_refused() -> None:
    fmt = PCM_24K
    with pytest.raises(Exception, match="lead"):
        _pacer(fmt, lead_chunks=0)


async def test_the_system_clock_is_monotonic_and_in_milliseconds() -> None:
    clock = SystemClock()
    first = clock.monotonic_ms()
    second = clock.monotonic_ms()
    assert second >= first
    assert first > 0


def test_a_manual_clock_cannot_go_backwards() -> None:
    clock = ManualClock()
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)


# ---------------------------------------------------------------------------
# Sample alignment
# ---------------------------------------------------------------------------


def test_an_odd_chunk_holds_back_the_orphan_byte() -> None:
    """Dropping it byte-swaps every subsequent sample — full-scale noise, not subtle
    distortion, and easy to misattribute to the network."""
    aligner = SampleAligner()
    assert aligner.feed(bytes([1, 2, 3])) == bytes([1, 2])
    assert aligner.pending_bytes == 1
    assert aligner.feed(bytes([4])) == bytes([3, 4])
    assert aligner.pending_bytes == 0


def test_an_arbitrary_chunking_reassembles_exactly() -> None:
    """The general case: a byte stream split at sizes nobody chose."""
    source = bytes(range(200))
    for seed in range(25):
        rng = random.Random(seed)
        aligner = SampleAligner()
        out = bytearray()
        offset = 0
        while offset < len(source):
            size = rng.randint(1, 17)
            out += aligner.feed(source[offset : offset + size])
            offset += size
        assert bytes(out) == source, f"stream was corrupted (seed={seed})"
        assert aligner.pending_bytes == 0


def test_reset_discards_the_carried_byte() -> None:
    """Carrying a byte across a discontinuity would put the tail of an abandoned
    utterance at the head of the next one."""
    aligner = SampleAligner()
    aligner.feed(bytes([1]))
    aligner.reset()
    assert aligner.pending_bytes == 0
    assert aligner.feed(bytes([2, 3])) == bytes([2, 3])


def test_the_aligner_agrees_with_the_declared_sample_width() -> None:
    """`align` deliberately has no imports; this is what keeps the constant honest."""
    from rn_providers.audio.formats import SAMPLE_WIDTH_BYTES
    from rn_voice.media import align

    assert align._SAMPLE_WIDTH == SAMPLE_WIDTH_BYTES


def test_nothing_in_the_media_layer_reads_the_clock_directly() -> None:
    """REALTIME_VOICE §11: *"Nothing in the audio path may call `time.monotonic()`
    directly."*

    Barge-in correctness is a timing relationship, and a relationship is only testable
    if the test controls time. One module is allowed to touch `time` — `clock.py`, which
    exists to be the single place that does.
    """
    import pathlib

    media = pathlib.Path(__file__).resolve().parents[2] / ("apps/voice-gateway/src/rn_voice/media")
    offenders = sorted(
        path.name
        for path in media.glob("*.py")
        if path.name != "clock.py"
        and (
            "time.monotonic" in path.read_text(encoding="utf-8")
            or "time.time" in path.read_text(encoding="utf-8")
        )
    )
    assert offenders == [], (
        f"these media modules read the clock directly instead of taking one: {offenders}"
    )


def test_the_session_layer_does_not_read_the_clock_directly_either() -> None:
    """The bridge owns the pumps, so a stray `time.monotonic()` there would defeat the
    injected clock just as thoroughly as one in `media`."""
    import pathlib

    session = pathlib.Path(__file__).resolve().parents[2] / (
        "apps/voice-gateway/src/rn_voice/session"
    )
    offenders = sorted(
        path.name
        for path in session.glob("*.py")
        if "time.monotonic" in path.read_text(encoding="utf-8")
        or "time.time" in path.read_text(encoding="utf-8")
    )
    assert offenders == []
