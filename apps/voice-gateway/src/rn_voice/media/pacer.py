"""The outbound pacer. Drains at realtime, keeping a deliberately shallow lead.

The tempting alternative is to dump audio as fast as the model produces it and rely on
the provider's own buffer plus `clear` for barge-in. **Reject it**, for one decisive
reason: the barge-in accounting is only as accurate as the mark lag. With a deep
sink-side buffer, the gap between "what we handed to the provider" and "what the caller
actually heard" can be fifteen seconds — and at barge-in you would be guessing across
that entire window, in the direction that corrupts the conversation.

With a lead of one or two chunks the uncertainty window is one or two chunks: **80-160
ms at 24 kHz**, which is what makes the PRD's ~200 ms barge-in requirement achievable
at all. That is the concrete payoff of pacing shallowly, and it is why the lead is a
small integer rather than a byte budget.

Two supporting reasons for the same choice: the provider's 100000-byte maximum chunk
(2.08 s at 24 kHz) is a strong hint that its sink is not deep, and its keepalive and
idle behaviour on the media socket are undocumented (§6a-8) — so nothing here should
depend on undocumented buffering behaviour for correctness.

**Every write is followed by a mark.** One mark per chunk, not per utterance: it costs
one tiny frame per chunk (12.5/s/call at 80 ms chunks) and it bounds the barge-in
uncertainty to a single chunk. Marking per utterance is cheaper and makes the window
the whole utterance.

The pacer takes its clock and its sleep function by injection, so a test drives it to
completion in zero wall-clock time. Nothing here reads `time` directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rn_core.errors import InvariantViolation
from rn_providers.audio.formats import AudioFormat, ms_of_bytes
from rn_voice.media.clock import Clock
from rn_voice.media.ledger import PlaybackLedger
from rn_voice.media.ring import OutboundChunk, OutboundRingBuffer

__all__ = ["DEFAULT_LEAD_CHUNKS", "Pacer", "PacerSinks"]

#: How many chunks may be outstanding in the provider's buffer. Two, because one is
#: enough to underrun on any scheduling hiccup and three doubles the barge-in
#: uncertainty window for no audible gain.
DEFAULT_LEAD_CHUNKS = 2


@dataclass(frozen=True, slots=True)
class PacerSinks:
    """Where a paced chunk goes. Injected so the pacer never imports a transport.

    Attributes:
        send_media: Writes one legal payload.
        send_mark: Writes one mark, immediately after the payload it follows.
        sleep: Awaits a duration in milliseconds. Injected rather than
            `asyncio.sleep` so a test runs a 20-second response instantly.
    """

    send_media: Callable[[bytes], Awaitable[None]]
    send_mark: Callable[[str], Awaitable[None]]
    sleep: Callable[[float], Awaitable[None]]


class Pacer:
    """Drains the ring buffer to the wire at approximately realtime.

    Args:
        buffer: The source of legal chunks.
        ledger: Fed `real_bytes` per chunk and the mark that follows it.
        fmt: The telephony format, for turning bytes into a drain schedule.
        clock: Injected. Used to decide when the lead has been consumed.
        sinks: The write side.
        lead_chunks: How many chunks may be in flight ahead of realtime.
    """

    __slots__ = (
        "_buffer",
        "_clock",
        "_format",
        "_lead_chunks",
        "_ledger",
        "_marks_written",
        "_playhead_ms",
        "_sinks",
    )

    def __init__(
        self,
        *,
        buffer: OutboundRingBuffer,
        ledger: PlaybackLedger,
        fmt: AudioFormat,
        clock: Clock,
        sinks: PacerSinks,
        lead_chunks: int = DEFAULT_LEAD_CHUNKS,
    ) -> None:
        if lead_chunks < 1:
            raise InvariantViolation(
                "A pacer lead below one chunk cannot keep audio flowing.",
                detail={"lead_chunks": lead_chunks},
            )
        self._buffer = buffer
        self._ledger = ledger
        self._format = fmt
        self._clock = clock
        self._sinks = sinks
        self._lead_chunks = lead_chunks
        self._marks_written = 0
        #: Wall-clock instant, in monotonic ms, at which everything written so far will
        #: have finished playing. The drain schedule is derived from this rather than
        #: from a sleep-per-chunk, so scheduling jitter does not accumulate into drift.
        self._playhead_ms = 0.0

    @property
    def marks_written(self) -> int:
        return self._marks_written

    async def drain(self, *, final: bool = False) -> int:
        """Write every chunk currently available, pacing to keep the lead shallow.

        Args:
            final: Also drain the sub-minimum remainder, padded with silence. Call it
                when a response has ended — otherwise the tail of every utterance sits
                in the buffer until the next one pushes it out, which sounds like the
                agent swallowing its last word.

        Returns:
            How many chunks were written.
        """
        written = 0
        while True:
            chunk = self._buffer.take_chunk()
            if chunk is None:
                if not final:
                    break
                chunk = self._buffer.take_final()
                if chunk is None:
                    break
            await self._wait_for_room()
            await self._write(chunk)
            written += 1
        return written

    async def _wait_for_room(self) -> None:
        """Sleep until the provider's buffer has drained below the lead."""
        now = self._clock.monotonic_ms()
        if self._playhead_ms <= now:
            # Nothing outstanding — the sink is idle and we are behind, not ahead.
            self._playhead_ms = now
            return
        lead_ms = self._lead_chunk_ms()
        ahead = self._playhead_ms - now
        if ahead > lead_ms:
            await self._sinks.sleep(ahead - lead_ms)

    def _lead_chunk_ms(self) -> float:
        """The permitted lead, in milliseconds of audio."""
        return ms_of_bytes(self._buffer.policy.effective_min * self._lead_chunks, self._format)

    async def _write(self, chunk: OutboundChunk) -> None:
        """Write one chunk, then its mark, then account for it.

        Order matters. The mark follows the audio it refers to (HC-9), and the ledger is
        told *after* the write succeeds — an exception between the two would otherwise
        leave the ledger claiming audio that never reached the socket, which is the
        over-reporting direction.
        """
        await self._sinks.send_media(chunk.payload)
        self._ledger.note_enqueued(
            item_id=chunk.item_id,
            content_index=chunk.content_index,
            # Real bytes only. Padding is played but it is not the model's audio, and
            # counting it would inflate `audio_end_ms`.
            byte_count=chunk.real_bytes,
        )
        name = f"{chunk.item_id}:{self._marks_written}"
        self._marks_written += 1
        self._ledger.note_mark_written(name)
        await self._sinks.send_mark(name)
        self._playhead_ms = max(self._playhead_ms, self._clock.monotonic_ms()) + ms_of_bytes(
            len(chunk.payload), self._format
        )

    def reset_playhead(self) -> None:
        """Forget the outstanding lead. Called after a barge-in clears the sink.

        Without this the pacer would keep waiting out audio the provider has just
        discarded, and the agent's next words would be late by the length of what was
        interrupted.
        """
        self._playhead_ms = self._clock.monotonic_ms()
