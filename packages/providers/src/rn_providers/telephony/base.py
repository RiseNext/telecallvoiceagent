"""The telephony seam: chunk rules, media events, and the transport protocol.

Two things live here and they are deliberately separate:

**`ChunkPolicy` is a provider fact.** How large an outbound audio write may be, and
what it must be a multiple of, is a rule the telephony vendor sets. Exotel's rule is
not Twilio's. So the *adapter* declares it and the bridge obeys it — the split ADR-003
states as *format conversion is a provider fact; pacing is our policy*.

**`TelephonyTransport` is the socket.** Events in, audio and control frames out. It
knows nothing about ring buffers, playback accounting or barge-in policy; those are
`rn_voice.media`, which is where they can be tested without a socket at all.

## The derivation that produces 3200 / 3200 / 3840

HC-2 is absolute: outbound payloads are a multiple of **320 bytes**, at least **3200**,
at most **100000**. That rule is in *bytes*, and bytes mean different durations at
different rates — which is where the trap is. At 24 kHz, 320 bytes is 6.667 ms, and
accumulating playback in units of 6.667 ms makes `audio_end_ms` drift. `audio_end_ms`
is the value HC-7 requires us to report truthfully on every barge-in, and a wrong one
fails **silently** (REALTIME_VOICE §4).

So the policy carries a second number, the **frame quantum** — 20 ms of audio, which is
320 / 640 / 960 bytes at 8k / 16k / 24k — and the effective alignment is the lowest
common multiple of the two. The minimum emission is then the smallest multiple of that
alignment which still satisfies the provider's byte floor:

    rate   provider  quantum   lcm   effective minimum
    8000      320       320    320   3200 B = 200 ms
    16000     320       640    640   3200 B = 100 ms
    24000     320       960    960   3840 B =  80 ms

Those are exactly the three numbers ADR-003 and REALTIME_VOICE §1.4 state, and deriving
them rather than hardcoding them means a fourth rate, or a provider with a different
alignment, cannot produce a number nobody checked.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rn_core.errors import InvariantViolation
from rn_providers.audio.formats import AudioFormat, ms_of_bytes

__all__ = [
    "ChunkPolicy",
    "ConnectedEvent",
    "DtmfEvent",
    "FrameDecodeFailed",
    "MarkEvent",
    "MediaEvent",
    "StartEvent",
    "StopEvent",
    "TelephonyEvent",
    "TelephonyTransport",
    "chunk_policy_for",
]


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    """The outbound write rules a telephony provider imposes, plus our frame quantum.

    Attributes:
        alignment_bytes: The provider's own alignment rule (Exotel: 320).
        min_bytes: The provider's floor (Exotel: 3200).
        max_bytes: The provider's ceiling (Exotel: 100000).
        frame_quantum_bytes: **Ours**, not the provider's — the byte length of one
            `FRAME_MS` frame at the negotiated rate. This is what keeps playback
            accounting on whole milliseconds.
    """

    alignment_bytes: int
    min_bytes: int
    max_bytes: int
    frame_quantum_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("alignment_bytes", self.alignment_bytes),
            ("min_bytes", self.min_bytes),
            ("max_bytes", self.max_bytes),
            ("frame_quantum_bytes", self.frame_quantum_bytes),
        ):
            if value <= 0:
                raise InvariantViolation(
                    "Chunk policy values must be positive.", detail={name: value}
                )
        if self.max_bytes < self.min_bytes:
            raise InvariantViolation(
                "Chunk policy ceiling is below its floor.",
                detail={"min_bytes": self.min_bytes, "max_bytes": self.max_bytes},
            )
        if self.effective_max < self.effective_min:
            # Reachable with a large quantum and a tight provider window. It would mean
            # no legal chunk exists at all, which must fail here rather than as a pacer
            # that silently never emits.
            raise InvariantViolation(
                "No chunk size satisfies this policy once the frame quantum is applied.",
                detail={
                    "effective_min": self.effective_min,
                    "effective_max": self.effective_max,
                    "frame_quantum_bytes": self.frame_quantum_bytes,
                },
            )

    @property
    def effective_alignment(self) -> int:
        """The provider's alignment and our frame quantum, reconciled.

        Their lowest common multiple, so an emission satisfies both at once. At 8 kHz
        both are 320 and this is a no-op; at 24 kHz it is what turns 320 into 960.
        """
        return math.lcm(self.alignment_bytes, self.frame_quantum_bytes)

    @property
    def effective_min(self) -> int:
        """The smallest legal emission: the provider's floor, rounded up to alignment."""
        alignment = self.effective_alignment
        return ((self.min_bytes + alignment - 1) // alignment) * alignment

    @property
    def effective_max(self) -> int:
        """The largest legal emission: the provider's ceiling, rounded **down**."""
        return (self.max_bytes // self.effective_alignment) * self.effective_alignment

    def is_legal(self, byte_count: int) -> bool:
        """Whether a payload of this size may be written.

        The single predicate every emission is checked against, by the ring buffer that
        produces chunks *and* by the fake media server that receives them — so the two
        cannot disagree about what legal means.
        """
        return (
            byte_count >= self.effective_min
            and byte_count <= self.effective_max
            and byte_count % self.effective_alignment == 0
        )

    def emission_size(self, available: int) -> int:
        """How many bytes to write next, or 0 if not enough is buffered.

        **Deliberately the *smallest* legal emission, not the largest.** Emitting as much
        as is buffered is the obvious implementation and it is wrong: the provider's
        ceiling is 100000 bytes, which is 2.08 seconds at 24 kHz, and a chunk that size
        puts two seconds of audio into a buffer we cannot see and can only destroy
        wholesale. The barge-in uncertainty window is exactly one chunk (§3), so chunk
        size *is* the uncertainty window — 80 ms at 24 kHz if we emit the minimum, two
        seconds if we emit the maximum.

        Throughput is not lost by this: the pacer emits repeatedly and controls the
        *rate*. What is gained is a predictable cadence and a bounded window.

        Zero rather than an exception: "not enough buffered yet" is the normal state of
        a ring buffer between deltas, not an error.
        """
        return self.effective_min if available >= self.effective_min else 0

    def largest_legal_chunk(self, available: int) -> int:
        """The biggest legal emission that fits in `available` bytes, or 0 if none does.

        Not what the pacer uses — see `emission_size`. Kept because the ceiling
        arithmetic is worth having in one place and a caller draining a backlog under a
        deadline may legitimately want it.
        """
        if available < self.effective_min:
            return 0
        alignment = self.effective_alignment
        return min(available - (available % alignment), self.effective_max)

    def milliseconds_of(self, byte_count: int, fmt: AudioFormat) -> float:
        return ms_of_bytes(byte_count, fmt)


def chunk_policy_for(
    fmt: AudioFormat,
    *,
    alignment_bytes: int,
    min_bytes: int,
    max_bytes: int,
) -> ChunkPolicy:
    """Build a policy for a negotiated format, taking the quantum from the format.

    The frame quantum is never passed in: it is a function of the rate, and letting a
    caller supply it would allow a policy whose accounting quantum does not match the
    audio it is accounting for.
    """
    return ChunkPolicy(
        alignment_bytes=alignment_bytes,
        min_bytes=min_bytes,
        max_bytes=max_bytes,
        frame_quantum_bytes=fmt.frame_bytes,
    )


# --------------------------------------------------------------------------
# Inbound events
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectedEvent:
    """The socket is up. Carries nothing; the call is described by `StartEvent`."""


@dataclass(frozen=True, slots=True)
class StartEvent:
    """The media stream is beginning.

    `media_format` is read from the provider rather than assumed. ADR-003 requires this:
    the query parameter that *requests* a rate is unverified (§6a-2, anti-fact #9), so
    the negotiated rate is whatever the provider says it is on this event — and if the
    two disagree, the provider wins and the bridge is configured from it.
    """

    stream_sid: str
    call_sid: str
    media_format: AudioFormat
    #: Voicebot custom parameters. Capped at 3 pairs / 256 characters of query string
    #: (HC-12), so in practice exactly one opaque session id travels here and everything
    #: else is looked up server-side.
    custom_parameters: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MediaEvent:
    """One inbound audio frame, already base64-decoded to raw PCM."""

    payload: bytes
    sequence_number: int | None = None
    chunk: int | None = None
    #: The provider's own timestamp, in milliseconds, as it sent it. Advisory: it is
    #: not our clock and nothing in the playback accounting derives from it.
    timestamp_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DtmfEvent:
    digit: str


@dataclass(frozen=True, slots=True)
class MarkEvent:
    """The provider has finished playing the audio that preceded this mark (HC-9).

    **The only ground truth about what the caller actually heard.** Everything else the
    bridge knows is what it wrote to a socket.
    """

    name: str


@dataclass(frozen=True, slots=True)
class StopEvent:
    """The stream is over. Nothing further may be written."""


@dataclass(frozen=True, slots=True)
class FrameDecodeFailed:
    """One inbound frame could not be decoded.

    **An event, not an exception, and that is the whole point.** A malformed frame is
    one bad frame out of twenty per second; raising out of the event iterator would end
    the iterator and therefore the call, turning a recoverable provider hiccup into a
    dropped call. The adapter reports it and keeps reading.

    Carries a reason and never the frame. A frame that failed to decode may still hold
    caller audio, and the point of logging this is diagnosis, not capture.
    """

    reason: str


type TelephonyEvent = (
    ConnectedEvent | StartEvent | MediaEvent | DtmfEvent | MarkEvent | StopEvent | FrameDecodeFailed
)


@runtime_checkable
class TelephonyTransport(Protocol):
    """One live media socket.

    Deliberately four methods. Anything richer — pacing, buffering, retry — is bridge
    policy and belongs above, where it can be tested without a socket.
    """

    @property
    def chunk_policy(self) -> ChunkPolicy:
        """The outbound write rules. Read once at session open, obeyed thereafter."""
        ...

    @property
    def media_format(self) -> AudioFormat:
        """The negotiated format, as reported by the provider on `StartEvent`."""
        ...

    def events(self) -> AsyncIterator[TelephonyEvent]:
        """Inbound events, in arrival order, until the stream stops."""
        ...

    async def send_media(self, payload: bytes) -> None:
        """Write one audio payload. Must already satisfy `chunk_policy`."""
        ...

    async def send_mark(self, name: str) -> None:
        """Write a mark. The provider echoes it once the preceding audio has played."""
        ...

    async def clear_playback(self) -> None:
        """Discard audio the provider has buffered but not yet played (HC-8).

        Discards *their* buffer only — it does not stop our generator, which is why
        barge-in is three operations and not one.
        """
        ...
