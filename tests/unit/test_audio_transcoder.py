"""Audio formats and the transcoder: golden files, conservation, anti-aliasing.

**What a golden file here does and does not prove.** The `.bin` fixtures were generated
by this implementation, so they catch *change* — a soxr upgrade, a quality-preset edit,
a refactor that reorders the filter — and announce it as a diff instead of as a caller
complaining about audio quality three weeks later. They do not prove the resampler is
correct on their own. That is what the other tests in this file are for: the ratio
tests bound the arithmetic, byte conservation bounds the buffering, and the
anti-aliasing test bounds the thing that actually matters for Indic intelligibility.

Regenerate deliberately, never casually:

    uv run python -m tests.fixtures.regenerate_audio_goldens
"""

from __future__ import annotations

import math
import pathlib

import pytest

from rn_core.errors import InvariantViolation, ProviderError
from rn_providers.audio.formats import (
    FRAME_MS,
    PCM_8K,
    PCM_16K,
    PCM_24K,
    SUPPORTED_RATES,
    AudioFormat,
    bytes_of_ms,
    ms_of_bytes,
)
from rn_providers.audio.transcoder import (
    PassthroughTranscoder,
    PolyphaseTranscoder,
    ResamplerQuality,
    resolve_transcoder,
)

pytestmark = pytest.mark.unit

GOLDEN_DIR = pathlib.Path(__file__).parent.parent / "fixtures" / "audio"

#: The fixture signal: 200 ms of a 440 Hz tone. Deterministic, non-silent (silence
#: would make a dropped tail invisible) and low enough in frequency to survive every
#: rate conversion, so a golden diff means the *resampler* changed rather than the
#: signal being near a Nyquist edge.
GOLDEN_MS = 200
GOLDEN_TONE_HZ = 440


def tone(fmt: AudioFormat, *, hz: int, milliseconds: int, amplitude: int = 12000) -> bytes:
    """A deterministic mono s16le sine. Identical arithmetic to the golden generator."""
    samples = int(fmt.rate_hz * milliseconds / 1000)
    out = bytearray()
    for index in range(samples):
        value = int(amplitude * math.sin(2 * math.pi * hz * index / fmt.rate_hz))
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


def _to_samples(pcm: bytes) -> list[int]:
    return [
        int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        for offset in range(0, len(pcm), 2)
    ]


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------


def test_the_three_legal_rates_and_no_others() -> None:
    """ADR-003 permits exactly three configurations. A fourth is a silent mis-rate."""
    assert {8000, 16000, 24000} == SUPPORTED_RATES
    with pytest.raises(InvariantViolation):
        AudioFormat(rate_hz=44100)


def test_a_frame_is_twenty_milliseconds_at_every_rate() -> None:
    """320 / 640 / 960 — and each is a whole number of milliseconds, which 320 bytes at
    24 kHz is not. That is the entire reason the quantum exists."""
    assert (PCM_8K.frame_bytes, PCM_16K.frame_bytes, PCM_24K.frame_bytes) == (320, 640, 960)
    for fmt in (PCM_8K, PCM_16K, PCM_24K):
        assert ms_of_bytes(fmt.frame_bytes, fmt) == FRAME_MS


def test_stereo_is_refused() -> None:
    """A stereo frame would silently halve every duration derived from a byte count."""
    with pytest.raises(InvariantViolation):
        AudioFormat(rate_hz=8000, channels=2)


def test_byte_and_millisecond_conversions_round_trip() -> None:
    for fmt in (PCM_8K, PCM_16K, PCM_24K):
        assert ms_of_bytes(bytes_of_ms(100, fmt), fmt) == 100.0


def test_a_byte_count_is_snapped_down_to_a_whole_sample() -> None:
    """An odd byte count would split an s16le sample; every subsequent sample would then
    be byte-swapped, which is loud static rather than quiet corruption."""
    for fmt in (PCM_8K, PCM_16K, PCM_24K):
        assert bytes_of_ms(0.05, fmt) % 2 == 0


def test_silence_is_the_requested_duration() -> None:
    assert len(PCM_24K.silence(80)) == bytes_of_ms(80, PCM_24K)
    assert set(PCM_24K.silence(80)) == {0}


# ---------------------------------------------------------------------------
# Transcoder selection
# ---------------------------------------------------------------------------


def test_matched_formats_resolve_to_passthrough() -> None:
    """The case that matters commercially: an OpenAI-primary agent at 24 kHz and a
    Sarvam-primary agent at 8 kHz both resample nothing, with no branch at the call site."""
    assert isinstance(resolve_transcoder(source=PCM_24K, target=PCM_24K), PassthroughTranscoder)
    assert isinstance(resolve_transcoder(source=PCM_8K, target=PCM_24K), PolyphaseTranscoder)


def test_a_resampler_between_identical_rates_is_refused() -> None:
    """It would be a resampler doing nothing while looking like it was doing something."""
    with pytest.raises(InvariantViolation):
        PolyphaseTranscoder(source=PCM_24K, target=PCM_24K)


def test_odd_byte_counts_are_refused_by_both_implementations() -> None:
    for transcoder in (
        PassthroughTranscoder(PCM_24K),
        PolyphaseTranscoder(source=PCM_24K, target=PCM_8K),
    ):
        with pytest.raises(InvariantViolation):
            transcoder.process(b"\x00\x00\x00")


def test_passthrough_returns_its_input_unchanged() -> None:
    pcm = tone(PCM_8K, hz=440, milliseconds=20)
    assert PassthroughTranscoder(PCM_8K).process(pcm) == pcm
    assert PassthroughTranscoder(PCM_8K).flush() == b""


# ---------------------------------------------------------------------------
# Golden files — both directions, all three rates
# ---------------------------------------------------------------------------

GOLDEN_PAIRS = [
    (PCM_8K, PCM_24K),
    (PCM_24K, PCM_8K),
    (PCM_16K, PCM_24K),
    (PCM_24K, PCM_16K),
    (PCM_8K, PCM_16K),
    (PCM_16K, PCM_8K),
]


def golden_name(source: AudioFormat, target: AudioFormat) -> str:
    return f"tone{GOLDEN_TONE_HZ}_{GOLDEN_MS}ms_{source.rate_hz}_to_{target.rate_hz}.bin"


def transcode_whole(source: AudioFormat, target: AudioFormat, pcm: bytes) -> bytes:
    """Feed the signal in 20 ms frames and drain the tail — exactly as the bridge does."""
    transcoder = PolyphaseTranscoder(source=source, target=target, quality=ResamplerQuality.HIGH)
    out = bytearray()
    frame = source.frame_bytes
    for offset in range(0, len(pcm), frame):
        out += transcoder.process(pcm[offset : offset + frame])
    out += transcoder.flush()
    return bytes(out)


@pytest.mark.parametrize(("source", "target"), GOLDEN_PAIRS, ids=lambda f: str(f.rate_hz))
def test_a_known_input_produces_byte_exact_expected_output(
    source: AudioFormat, target: AudioFormat
) -> None:
    """Byte-exact, both directions, all three rates — the Phase-4 criterion.

    A diff here is not automatically a bug; it is a *change*, and the point is that it
    cannot happen silently. Investigate, then regenerate deliberately.
    """
    expected_path = GOLDEN_DIR / golden_name(source, target)
    assert expected_path.is_file(), (
        f"missing golden {expected_path.name}; regenerate with "
        "`uv run python -m tests.fixtures.regenerate_audio_goldens`"
    )
    produced = transcode_whole(
        source, target, tone(source, hz=GOLDEN_TONE_HZ, milliseconds=GOLDEN_MS)
    )
    assert produced == expected_path.read_bytes()


@pytest.mark.parametrize(("source", "target"), GOLDEN_PAIRS, ids=lambda f: str(f.rate_hz))
def test_total_audio_in_equals_total_audio_out(source: AudioFormat, target: AudioFormat) -> None:
    """Byte conservation, modulo the rate ratio and an explicit flush.

    A ring buffer or a resampler that silently drops a tail is invisible in a listening
    test and obvious here. One frame of tolerance for the filter's own boundary.
    """
    pcm = tone(source, hz=GOLDEN_TONE_HZ, milliseconds=GOLDEN_MS)
    produced = transcode_whole(source, target, pcm)
    expected_ms = ms_of_bytes(len(pcm), source)
    produced_ms = ms_of_bytes(len(produced), target)
    assert abs(produced_ms - expected_ms) <= FRAME_MS


def test_the_flush_is_what_returns_the_tail() -> None:
    """Without the flush the last frames of every utterance are lost inside the filter."""
    transcoder = PolyphaseTranscoder(source=PCM_24K, target=PCM_8K)
    streamed = len(transcoder.process(tone(PCM_24K, hz=440, milliseconds=GOLDEN_MS)))
    tail = len(transcoder.flush())
    assert tail > 0
    assert abs(ms_of_bytes(streamed + tail, PCM_8K) - GOLDEN_MS) <= FRAME_MS


def test_reset_starts_a_fresh_stream() -> None:
    transcoder = PolyphaseTranscoder(source=PCM_8K, target=PCM_24K)
    first = transcoder.process(tone(PCM_8K, hz=440, milliseconds=100))
    transcoder.reset()
    second = transcoder.process(tone(PCM_8K, hz=440, milliseconds=100))
    assert first == second, "a reset transcoder must behave like a new one"


# ---------------------------------------------------------------------------
# The one that matters for Indic intelligibility
# ---------------------------------------------------------------------------


def goertzel_power(pcm: bytes, *, hz: float, rate: int) -> float:
    """Energy at one frequency. A Goertzel filter, so this needs no FFT dependency."""
    samples = _to_samples(pcm)
    if not samples:
        return 0.0
    omega = 2.0 * math.pi * hz / rate
    coefficient = 2.0 * math.cos(omega)
    s_prev = s_prev2 = 0.0
    for sample in samples:
        s = sample + coefficient * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s
    return s_prev2 * s_prev2 + s_prev * s_prev - coefficient * s_prev * s_prev2


def test_downsampling_does_not_alias_a_five_kilohertz_tone() -> None:
    """**The test this whole module exists for.**

    At 8 kHz output Nyquist is 4 kHz, so a 5 kHz component folds to 3 kHz — directly on
    top of speech. The energy above 4 kHz in speech is concentrated in fricatives,
    sibilants and aspirated stops, and aspirated-vs-unaspirated and retroflex-vs-dental
    contrasts are **phonemic** in Hindi and Telugu. Aliasing them does not sound like
    static; it sounds like the agent is mumbling.

    Naive decimation fails this instantly. soxr passes it by a wide margin.
    """
    source = tone(PCM_24K, hz=5000, milliseconds=GOLDEN_MS, amplitude=20000)
    downsampled = transcode_whole(PCM_24K, PCM_8K, source)

    reference = goertzel_power(source, hz=5000, rate=PCM_24K.rate_hz)
    folded = goertzel_power(downsampled, hz=3000, rate=PCM_8K.rate_hz)
    # Guard against a silent output making the ratio trivially pass.
    assert reference > 0
    ratio_db = 10 * math.log10(max(folded, 1e-12) / reference)
    assert ratio_db < -40, f"5 kHz aliased into 3 kHz at {ratio_db:.1f} dB (limit -40 dB)"


def test_naive_decimation_would_fail_that_test() -> None:
    """A negative control. Without it, the assertion above could be passing for the
    wrong reason — a threshold nothing could ever exceed proves nothing."""
    source = tone(PCM_24K, hz=5000, milliseconds=GOLDEN_MS, amplitude=20000)
    samples = _to_samples(source)
    naive = b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples[::3])
    reference = goertzel_power(source, hz=5000, rate=PCM_24K.rate_hz)
    folded = goertzel_power(naive, hz=3000, rate=PCM_8K.rate_hz)
    ratio_db = 10 * math.log10(max(folded, 1e-12) / reference)
    assert ratio_db > -40, "naive decimation is supposed to alias; the control is broken"


def test_upsampling_preserves_the_tone() -> None:
    """The cheap direction still has to be right — it just does not need a filter."""
    source = tone(PCM_8K, hz=440, milliseconds=GOLDEN_MS, amplitude=20000)
    upsampled = transcode_whole(PCM_8K, PCM_24K, source)
    at_440 = goertzel_power(upsampled, hz=440, rate=PCM_24K.rate_hz)
    at_1200 = goertzel_power(upsampled, hz=1200, rate=PCM_24K.rate_hz)
    assert at_440 > at_1200 * 100


def test_the_audio_extra_is_reported_clearly_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """numpy and soxr live in `rn-providers[audio]`. A missing extra must say so."""
    import builtins

    real_import = builtins.__import__

    def _fail(name: str, *args: object, **kwargs: object) -> object:
        if name == "soxr":
            raise ImportError("no soxr")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _fail)
    with pytest.raises(ProviderError, match="audio extra"):
        PolyphaseTranscoder(source=PCM_8K, target=PCM_24K)
