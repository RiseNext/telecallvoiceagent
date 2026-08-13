"""Sample alignment: hold back a trailing half-sample instead of corrupting it.

PCM on the wire is a **byte** stream, and neither side promises that a chunk boundary
lands on a sample boundary. A realtime provider chunks base64 by whatever size suits it,
and a telephony frame is whatever the provider put in it. So an s16le delta can end with
one byte of a two-byte sample.

Handing that to a resampler is not a subtle error. Drop the odd byte and every
subsequent sample in the stream is **byte-swapped** — which is not quiet distortion, it
is full-scale noise, and it is easy to misattribute to the provider or the network.
Padding it with a zero is only marginally better: it injects a sample nobody sent and
shifts the whole stream by one byte anyway.

The right answer is the boring one: keep the orphan byte and put it at the front of the
next chunk. One byte of state, and the stream stays intact across an arbitrary chunking.

This is why the transcoder is strict about odd counts rather than tolerant: the
tolerance lives in exactly one place, here, and a caller that forgets it gets an error
instead of noise.
"""

from __future__ import annotations

__all__ = ["SampleAligner"]

#: s16le. Duplicated from `rn_providers.audio.formats.SAMPLE_WIDTH_BYTES` rather than
#: imported so this module has no dependencies at all; a test asserts the two agree.
_SAMPLE_WIDTH = 2


class SampleAligner:
    """Emits whole samples, carrying any trailing partial sample to the next chunk.

    One instance per direction. Sharing one between the inbound and outbound streams
    would splice a byte of the caller's audio into the agent's, which is exactly the
    class of bug this exists to prevent.
    """

    __slots__ = ("_carry",)

    def __init__(self) -> None:
        self._carry = b""

    @property
    def pending_bytes(self) -> int:
        """How much of a sample is being held back. Never more than one byte."""
        return len(self._carry)

    def feed(self, pcm: bytes) -> bytes:
        """Return every whole sample available, keeping any orphan byte for next time."""
        data = self._carry + pcm if self._carry else pcm
        remainder = len(data) % _SAMPLE_WIDTH
        if remainder:
            self._carry = data[-remainder:]
            return data[:-remainder]
        self._carry = b""
        return data

    def reset(self) -> None:
        """Discard the carried byte.

        Called when the stream restarts — a new response, or a barge-in that flushed
        everything. Carrying a byte across a discontinuity would put the tail of an
        abandoned utterance at the head of the next one.
        """
        self._carry = b""
