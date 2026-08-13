"""Audio formats and the arithmetic that every other module derives from.

**Milliseconds, never bytes, are the unit of account.** Milliseconds are
rate-invariant, so a resampler cannot corrupt them; bytes are not, and a byte count
that crosses a rate boundary silently means something different on the other side.
Every conversion in the audio path goes through the two functions at the bottom of
this module so that the arithmetic exists once.

The one formula everything rests on, s16le mono:

    bytes_per_second = rate * 2

**Only three rates are legal**, and they are legal for reasons that are external
facts rather than preferences (ADR-003, REALTIME_VOICE §1.4):

* **8000** — Exotel's native PSTN rate, and the rate at which both Sarvam cascade
  legs are pure passthrough (HC-23).
* **16000** — Sarvam's documented optimal STT rate. Nobody's default.
* **24000** — the only rate OpenAI Realtime accepts as `audio/pcm` (HC-4), so an
  OpenAI-primary agent running at 24 kHz does no resampling at all.

There is **no audio-quality argument** between them: the source is an 8 kHz phone
call and the higher rates are Exotel upsampling that adds no information. The reason
to care is resampling cost and chunk-accumulation latency, nothing else.

**`FRAME_MS = 20` is not a style choice.** A streaming resampler with internal state
needs consistently-sized frames to avoid boundary artefacts, and 20 ms is the
accounting quantum for playback: it is a whole number of bytes at all three rates
(320 / 640 / 960) *and* a whole number of milliseconds, which 320 bytes at 24 kHz is
not. See `ChunkPolicy` for what that saves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from rn_core.errors import InvariantViolation

__all__ = [
    "FRAME_MS",
    "PCM_8K",
    "PCM_16K",
    "PCM_24K",
    "SAMPLE_WIDTH_BYTES",
    "SUPPORTED_RATES",
    "AudioEncoding",
    "AudioFormat",
    "bytes_of_ms",
    "ms_of_bytes",
]

#: s16le. The only sample width in the media path — Exotel emits it (HC-1) and
#: OpenAI Realtime accepts it (HC-4), so nothing here needs to be general.
SAMPLE_WIDTH_BYTES: Final[int] = 2

#: The internal frame quantum, in milliseconds. See the module docstring.
FRAME_MS: Final[int] = 20

#: Rates the platform will negotiate. A rate outside this set is refused at
#: construction rather than producing an audio path that is subtly wrong.
SUPPORTED_RATES: Final[frozenset[int]] = frozenset({8000, 16000, 24000})


class AudioEncoding(StrEnum):
    """How samples are laid out.

    One member today, and that is honest rather than lazy: Exotel carries raw slin
    (s16le) and never G.711 (HC-1), which is the single fact that makes a resampler
    unavoidable on this stack. G.711 members will be added by whichever adapter first
    needs them, together with the codec that reads them — an enum member with no
    decoder behind it is a promise the code does not keep.
    """

    PCM_S16LE = "pcm_s16le"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """A negotiated audio format. Immutable, and validated on construction.

    Carried rather than passed as a bare integer rate because a rate on its own has
    been the source of the entire class of bug this module exists to prevent: a
    number that is 8000 in one place and 24000 in another, with nothing in the type
    system to notice.
    """

    rate_hz: int
    channels: int = 1
    encoding: AudioEncoding = AudioEncoding.PCM_S16LE

    def __post_init__(self) -> None:
        if self.rate_hz not in SUPPORTED_RATES:
            raise InvariantViolation(
                "Unsupported audio sample rate.",
                detail={"rate_hz": self.rate_hz, "supported": sorted(SUPPORTED_RATES)},
            )
        if self.channels != 1:
            # Telephony is mono end to end. A stereo frame here would silently halve
            # every duration computed from a byte count.
            raise InvariantViolation(
                "The media path is mono only.", detail={"channels": self.channels}
            )

    @property
    def bytes_per_second(self) -> int:
        return self.rate_hz * SAMPLE_WIDTH_BYTES * self.channels

    @property
    def frame_bytes(self) -> int:
        """Bytes in one `FRAME_MS` frame. 320 @ 8k, 640 @ 16k, 960 @ 24k."""
        return self.bytes_per_second * FRAME_MS // 1000

    def silence(self, milliseconds: int) -> bytes:
        """Digital silence. Used for the 10-second connect deadline and for padding.

        Zero bytes rather than a dither: this is s16le, and zero is silence.
        """
        return b"\x00" * bytes_of_ms(milliseconds, self)


PCM_8K: Final[AudioFormat] = AudioFormat(rate_hz=8000)
PCM_16K: Final[AudioFormat] = AudioFormat(rate_hz=16000)
PCM_24K: Final[AudioFormat] = AudioFormat(rate_hz=24000)


def ms_of_bytes(byte_count: int, fmt: AudioFormat) -> float:
    """Duration of a PCM byte count, in milliseconds.

    Returns a float and is **not** rounded here. Rounding belongs at the point where a
    whole millisecond is actually required (the truncate call), because rounding early
    and then accumulating is exactly how `audio_end_ms` drifts.
    """
    if byte_count < 0:
        raise InvariantViolation("Negative byte count.", detail={"bytes": byte_count})
    return byte_count * 1000.0 / fmt.bytes_per_second


def bytes_of_ms(milliseconds: float, fmt: AudioFormat) -> int:
    """PCM bytes for a duration, snapped **down** to a whole sample.

    Down rather than nearest: every caller of this is either allocating silence or
    bounding something, and a half sample is not representable. Snapping to the sample
    grid also guarantees the result is even, so no caller can produce a byte count that
    splits an s16le sample in half.
    """
    if milliseconds < 0:
        raise InvariantViolation("Negative duration.", detail={"ms": milliseconds})
    raw = int(milliseconds * fmt.bytes_per_second / 1000)
    return raw - (raw % (SAMPLE_WIDTH_BYTES * fmt.channels))
