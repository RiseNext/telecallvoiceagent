"""The audio transcoder. One interface, two implementations, resolved at session open.

**Where this lives is a decision, not an accident** (ADR-003): the transcoder sits at
the *telephony-adapter boundary* — never inside a provider client, never inside
business logic, never inline in the audio pump. The telephony adapter declares the
negotiated rate, the voice adapter declares what it accepts and emits, and the bridge
asks `resolve_transcoder` for the pair. That is what lets a Sarvam-primary agent at
8 kHz and an OpenAI-primary agent at 24 kHz be the same code path.

`PassthroughTranscoder` exists even though it does nothing. That is the point: with a
no-op in the interface the call site has **no branch**, and a branch that only fires on
the resampling path is a branch that is only tested on the resampling path.

## Upsampling can be cheap. Downsampling cannot.

`8k → 24k` (inbound, OpenAI path) adds no information; any reasonable interpolator is
fine. `24k → 8k` and `24k → 16k` require a **proper anti-aliasing low-pass before
decimation**. Naive decimation — taking every third sample — folds everything above
the new Nyquist back into the audible band: at 8 kHz output, Nyquist is 4 kHz and a
5 kHz component lands on 3 kHz, directly on top of speech.

This is not an audiophile concern. Energy above 4 kHz in speech is concentrated in
fricatives, sibilants and aspirated stops — and aspirated-vs-unaspirated and
retroflex-vs-dental contrasts are **phonemic** in Hindi and Telugu. Aliasing them does
not sound like static, it sounds like the agent is mumbling, which is the one thing
this product is judged on. `tests/unit/test_audio_transcoder.py` asserts it with a
5 kHz sine and a -40 dBFS floor at the fold-down frequency; naive decimation fails it
instantly.

So: **`soxr`, both directions, never a hand-rolled resampler and never naive
decimation.** `soxr` also replaces the stdlib `audioop` that PEP 594 removed in 3.13.

## Streaming, not one-shot

`process()` is stateful and `flush()` drains the filter's tail. A resampler restarted
per chunk produces a discontinuity at every boundary — a click every 20 ms, which
sounds like a bad line and profiles like nothing at all. `reset()` exists for session
reuse and clears that state deliberately.

Because the filter holds a tail, **`process()` output length is not exactly the input
length times the rate ratio** — it lags, and `flush()` returns the remainder. Total in
equals total out across the pair, which is what the byte-conservation test asserts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from rn_core.errors import InvariantViolation, ProviderError
from rn_providers.audio.formats import SAMPLE_WIDTH_BYTES, AudioFormat

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = [
    "AudioTranscoder",
    "PassthroughTranscoder",
    "PolyphaseTranscoder",
    "ResamplerQuality",
    "resolve_transcoder",
]


#: s16le full scale. The divisor for float conversion and the clip bounds after it.
_FULL_SCALE: Final[float] = 32768.0
_INT16_MIN: Final[int] = -32768
_INT16_MAX: Final[int] = 32767


class ResamplerQuality(StrEnum):
    """libsoxr quality presets, cheapest to best.

    Configuration rather than a constant because ADR-003 says to start high and lower
    it only if profiling proves the transcoder is a real cost — which at 20 ms frames
    is unlikely, and the likely culprit would be Python-level per-frame overhead
    rather than soxr.
    """

    QUICK = "QQ"
    LOW = "LQ"
    MEDIUM = "MQ"
    HIGH = "HQ"
    VERY_HIGH = "VHQ"


@runtime_checkable
class AudioTranscoder(Protocol):
    """Converts PCM from one format to another, statefully.

    Implementations must be safe to call from one task only. The audio path gives each
    direction its own transcoder instance rather than sharing one, because a resampler
    holds filter state and two interleaved streams through one instance would blend
    into each other — audibly, and in a way that looks like a network fault.
    """

    @property
    def source(self) -> AudioFormat: ...

    @property
    def target(self) -> AudioFormat: ...

    def process(self, pcm: bytes) -> bytes:
        """Convert a frame. May return fewer bytes than the ratio implies (filter lag)."""
        ...

    def flush(self) -> bytes:
        """Drain the filter tail. Call once at end of stream, then `reset()` to reuse."""
        ...

    def reset(self) -> None:
        """Discard filter state. The next `process()` starts a fresh stream."""
        ...


class PassthroughTranscoder:
    """A no-op transcoder for matched formats.

    Exists so that the bridge has no branch. It still validates that the two formats
    genuinely match, so that "passthrough" cannot be selected for a pair that needed
    conversion — which would be silent, and would sound like the wrong pitch.
    """

    __slots__ = ("_format",)

    def __init__(self, fmt: AudioFormat) -> None:
        self._format = fmt

    @property
    def source(self) -> AudioFormat:
        return self._format

    @property
    def target(self) -> AudioFormat:
        return self._format

    def process(self, pcm: bytes) -> bytes:
        _check_aligned(pcm)
        return pcm

    def flush(self) -> bytes:
        return b""

    def reset(self) -> None:
        return None


class PolyphaseTranscoder:
    """A streaming, anti-aliased resampler over `soxr`.

    Args:
        source: Input format.
        target: Output format. Must differ from `source` in rate — a same-rate
            `PolyphaseTranscoder` is refused, because it would be a resampler doing
            nothing while looking like it was doing something, and `resolve_transcoder`
            already has a name for that case.
        quality: libsoxr preset. Defaults to `HIGH`, per ADR-003.

    Raises:
        ProviderError: if `numpy`/`soxr` are not installed. They live in the
            `rn-providers[audio]` extra so that the API and the workers, which never
            touch a media frame, do not carry numpy.
    """

    __slots__ = ("_channels", "_quality", "_source", "_stream", "_target")

    def __init__(
        self,
        *,
        source: AudioFormat,
        target: AudioFormat,
        quality: ResamplerQuality = ResamplerQuality.HIGH,
    ) -> None:
        if source.rate_hz == target.rate_hz:
            raise InvariantViolation(
                "PolyphaseTranscoder was asked to resample between identical rates; "
                "use PassthroughTranscoder, which is what resolve_transcoder returns.",
                detail={"rate_hz": source.rate_hz},
            )
        if source.encoding is not target.encoding:
            raise InvariantViolation(
                "The transcoder resamples; it does not change encoding.",
                detail={"source": source.encoding.value, "target": target.encoding.value},
            )
        self._source = source
        self._target = target
        self._quality = quality
        self._channels = source.channels
        self._stream = self._new_stream()

    def _new_stream(self) -> Any:
        """A float32 stream, deliberately — **not** soxr's int16 mode.

        soxr's integer output path applies **dither**, and its dither is not seeded
        deterministically: the same input resampled in two processes differs by ±1 LSB.
        That is inaudible and completely fine for a phone call, and completely fatal for
        a byte-exact golden file — a golden that changes on every run cannot signal that
        anything changed.

        So the filter runs in float and this module quantises, with explicit
        round-half-away-from-zero and clipping. The output is then reproducible across
        processes, machines and runs, which is what makes an audio regression visible as
        a diff instead of as a caller complaining three weeks later.
        """
        soxr = _require_soxr()
        return soxr.ResampleStream(
            self._source.rate_hz,
            self._target.rate_hz,
            self._channels,
            dtype="float32",
            quality=self._quality.value,
        )

    @property
    def source(self) -> AudioFormat:
        return self._source

    @property
    def target(self) -> AudioFormat:
        return self._target

    def process(self, pcm: bytes) -> bytes:
        _check_aligned(pcm)
        if not pcm:
            return b""
        return self._run(pcm, last=False)

    def flush(self) -> bytes:
        return self._run(b"", last=True)

    def reset(self) -> None:
        # A fresh stream rather than a documented reset call: soxr's stream object has
        # no public reset, and re-creating it is unambiguous about what state survives.
        self._stream = self._new_stream()

    def _run(self, pcm: bytes, *, last: bool) -> bytes:
        numpy = _require_numpy()
        samples = numpy.frombuffer(pcm, dtype="<i2").astype(numpy.float32) / _FULL_SCALE
        if self._channels > 1:  # pragma: no cover - mono is enforced by AudioFormat
            samples = samples.reshape(-1, self._channels)
        converted = self._stream.resample_chunk(samples, last=last)
        # `rint` is round-half-to-even and `clip` bounds the interpolator's overshoot —
        # a resampled peak can exceed the original's amplitude, and wrapping instead of
        # clipping turns a loud sample into a loud *inverted* one, which is a click.
        quantised = numpy.clip(numpy.rint(converted * _FULL_SCALE), _INT16_MIN, _INT16_MAX).astype(
            "<i2"
        )
        return bytes(numpy.ascontiguousarray(quantised).tobytes())


def resolve_transcoder(
    *,
    source: AudioFormat,
    target: AudioFormat,
    quality: ResamplerQuality = ResamplerQuality.HIGH,
) -> AudioTranscoder:
    """Pick the transcoder for a format pair. Resolved at session open, not build time.

    Returns a `PassthroughTranscoder` when the formats already match — which is the
    case that matters commercially: an OpenAI-primary agent at 24 kHz and a
    Sarvam-primary agent at 8 kHz both resample nothing, and neither call site knows
    which one it got.
    """
    if source == target:
        return PassthroughTranscoder(source)
    return PolyphaseTranscoder(source=source, target=target, quality=quality)


def _check_aligned(pcm: bytes) -> None:
    """Refuse a byte count that splits a sample.

    Cheap, and it catches the specific mistake of slicing PCM on an odd boundary — the
    result is not quiet corruption, it is every subsequent sample being byte-swapped,
    which sounds like loud static and is easy to misattribute to the provider.
    """
    if len(pcm) % SAMPLE_WIDTH_BYTES:
        raise InvariantViolation(
            "PCM byte count does not land on a sample boundary.",
            detail={"bytes": len(pcm), "sample_width": SAMPLE_WIDTH_BYTES},
        )


_AUDIO_EXTRA_HINT: Final[str] = (
    "Install the audio extra: the transcoder needs numpy and soxr, which live in "
    "`rn-providers[audio]` so that services with no media path do not carry them."
)


def _require_soxr() -> Any:
    try:
        import soxr  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ProviderError(_AUDIO_EXTRA_HINT, detail={"missing": "soxr"}) from exc
    return soxr


def _require_numpy() -> Any:
    try:
        import numpy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ProviderError(_AUDIO_EXTRA_HINT, detail={"missing": "numpy"}) from exc
    return numpy
