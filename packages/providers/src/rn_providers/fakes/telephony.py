"""`FakeTelephonyProvider` — replay in, assertion out. The Exotel Voicebot applet, faked.

This is the component that makes PRD §7's *"the full call flow must be exercisable
without placing a paid phone call"* true rather than aspirational, and it is specified
in [TESTING.md §3.2](../../../../../docs/TESTING.md) rather than invented here.

**Inbound, it plays the caller.** A tape of frames replayed either `INSTANT` (as fast as
possible, for logic tests) or `REALTIME` (at wall-clock rate, for timing tests), with
faults injectable on demand: dropped frames, a duplicated frame, a sequence jump, a
malformed frame, a close without `stop`, a delayed `start` event (to probe the ~10 s
connect deadline, HC-5), and mark echoes with configurable lag and loss.

Every one of those is a real provider behaviour whose handling is otherwise never
exercised. The missing-mark case in particular is precisely what the `min()` clamp in
the playback ledger exists for.

**Outbound, it is a strict assertion sink.** Every chunk written is checked before it is
accepted: alignment and size against the chunk policy, an even byte count (s16le
framing), and the stream id. A fake that merely accepted bytes would let the single most
common failure in this class of integration — unaligned writes — reach a real call,
where it presents as choppy audio that everyone initially blames on the network.

It also asserts the **pacing** rule, which nothing else can: outbound audio must not
outrun real time by more than a configured jitter allowance. A pacer that dumps a whole
response passes every alignment check and still destroys barge-in accuracy.

> **Nothing here is evidence about Exotel.** The tapes are hand-authored from documented
> shapes and the outbound assertions are made against `ExotelDialect`, which is **[A]**.
> No real trace exists in this repository — see `docs/PHASE_4G_WIRE_CAPTURE.md`. This
> proves the bridge obeys the rules we believe apply; it cannot prove the rules.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rn_core.errors import InvariantViolation
from rn_providers.audio.formats import PCM_24K, AudioFormat, bytes_of_ms, ms_of_bytes
from rn_providers.telephony.base import (
    ChunkPolicy,
    ConnectedEvent,
    DtmfEvent,
    FrameDecodeFailed,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
    TelephonyEvent,
)
from rn_providers.telephony.exotel import (
    ASSUMED_DIALECT,
    ExotelDialect,
    decode_inbound_frame,
    exotel_chunk_policy,
)

__all__ = [
    "DEFAULT_JITTER_ALLOWANCE_MS",
    "FakeTelephonyProvider",
    "InboundAudio",
    "InboundDtmf",
    "InboundScriptStep",
    "MalformedFrame",
    "Pace",
    "SentFrame",
    "Stop",
    "TelephonyFaults",
    "WaitForOutbound",
]

#: How far ahead of real time outbound audio may run before the sink complains.
#:
#: The pacer holds a lead of one or two chunks by design (80-160 ms at 24 kHz), so the
#: allowance has to exceed that or the assertion fires on correct behaviour. Generous
#: rather than tight: this exists to catch "the bridge dumped the whole response", which
#: is off by seconds, not by tens of milliseconds.
DEFAULT_JITTER_ALLOWANCE_MS = 1_000.0


class Pace(StrEnum):
    """How fast the tape is replayed."""

    INSTANT = "instant"
    """As fast as the loop will run. For logic tests. No wall-clock time passes."""

    REALTIME = "realtime"
    """At the tape's own timestamps. For timing and load tests."""


# --------------------------------------------------------------------------
# Inbound tape
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InboundAudio:
    """Caller audio, as `milliseconds` split into `message_ms`-sized frames.

    Defaults to 20 ms per message. HC-1's budget is 10-20 messages/second/direction,
    implying 50-100 ms per message, but the exact cadence is one of the things the wire
    capture settles — so the default is the conservative small one and a test that cares
    states what it means.
    """

    milliseconds: int
    message_ms: int = 20


@dataclass(frozen=True, slots=True)
class InboundDtmf:
    digit: str


@dataclass(frozen=True, slots=True)
class MalformedFrame:
    """A frame the provider should never send, but might.

    `raw` goes through the **real** decoder; the adapter reports the failure as a
    `FrameDecodeFailed` event rather than raising, because raising would end the event
    iterator and therefore the call. One bad frame out of twenty per second must not
    drop a call.
    """

    raw: str


@dataclass(frozen=True, slots=True)
class WaitForOutbound:
    """Block the inbound tape until we have written `chunks` media frames.

    How a test synchronises without sleeping: *let the agent speak two chunks, then
    interrupt*. Sleeping instead makes the test slow and flaky in the same change.
    """

    chunks: int


@dataclass(frozen=True, slots=True)
class Stop:
    """End the stream with a `stop` event, as a well-behaved provider does."""


type InboundScriptStep = InboundAudio | InboundDtmf | MalformedFrame | WaitForOutbound | Stop


@dataclass(frozen=True, slots=True)
class TelephonyFaults:
    """Provider misbehaviour, injectable per run. Every field is a real failure mode.

    Attributes:
        drop_frames: Media-frame indices (0-based, counted across the whole tape) that
            are silently not delivered. Packet loss, as the application sees it.
        duplicate_frame: A media-frame index delivered twice. Providers retry.
        sequence_jump_at: A media-frame index after which `sequence_number` jumps by
            1000. The application must not depend on contiguity.
        close_without_stop: End the stream **without** a `stop` event — a dropped socket
            rather than a hang-up. The bridge must finalise anyway.
        delay_start_event_ms: Hold the `start` event back this long. At 9500 ms this
            probes the ~10 s connect deadline (HC-5), which is why the number exists.
        mark_echo_delay: How many further media writes must occur before a mark is
            echoed. `0` echoes as soon as the tape next yields.
        mark_loss_rate: Fraction of marks never echoed, applied **deterministically**
            (every *n*-th mark) rather than randomly — a fake that loses marks at random
            produces a test that fails one run in twenty.
    """

    drop_frames: frozenset[int] = frozenset()
    duplicate_frame: int | None = None
    sequence_jump_at: int | None = None
    close_without_stop: bool = False
    delay_start_event_ms: int = 0
    mark_echo_delay: int = 0
    mark_loss_rate: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.mark_loss_rate <= 1.0:
            raise InvariantViolation(
                "mark_loss_rate is a fraction.", detail={"value": self.mark_loss_rate}
            )


@dataclass(frozen=True, slots=True)
class SentFrame:
    """One frame we wrote, decoded. The audit record."""

    event: str
    payload: bytes = b""
    mark_name: str = ""
    #: Monotonic milliseconds, from the injected clock, at which we wrote it. What the
    #: pacing assertion is computed from.
    at_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class FakeTelephonyProvider:
    """An in-process stand-in for the Exotel media WebSocket. Satisfies `TelephonyTransport`.

    Args:
        script: What the caller side does, in order.
        media_format: The rate this call negotiated. Reported on `StartEvent`, which is
            where ADR-003 says the bridge should read it from.
        dialect: Which outbound shape to audit against. **[A]** — see `ExotelDialect`.
        faults: Provider misbehaviour to inject.
        pace: `INSTANT` or `REALTIME`.
        jitter_allowance_ms: How far ahead of real time outbound audio may run before
            the sink refuses it. Only meaningful with a clock that advances.
        clock_ms: Returns monotonic milliseconds. Injected so the pacing assertion works
            against a test's own clock rather than wall time.
    """

    __slots__ = (
        "_clock_ms",
        "_closed",
        "_dialect",
        "_echo_queue",
        "_faults",
        "_first_media_at_ms",
        "_jitter_allowance_ms",
        "_marks_seen",
        "_media_format",
        "_media_writes",
        "_pace",
        "_policy",
        "_script",
        "_stream_sid",
        "sent",
    )

    def __init__(
        self,
        script: Iterable[InboundScriptStep] = (),
        *,
        media_format: AudioFormat = PCM_24K,
        dialect: ExotelDialect = ASSUMED_DIALECT,
        faults: TelephonyFaults | None = None,
        pace: Pace = Pace.INSTANT,
        jitter_allowance_ms: float = DEFAULT_JITTER_ALLOWANCE_MS,
        clock_ms: Any = None,
        stream_sid: str = "fake-stream-sid",
    ) -> None:
        self._script: tuple[InboundScriptStep, ...] = tuple(script)
        self._media_format = media_format
        self._dialect = dialect
        self._faults = faults or TelephonyFaults()
        self._pace = pace
        self._jitter_allowance_ms = jitter_allowance_ms
        self._clock_ms = clock_ms
        self._policy = exotel_chunk_policy(media_format, dialect=dialect)
        self._stream_sid = stream_sid
        self._closed = False
        self._media_writes = 0
        self._marks_seen = 0
        self._first_media_at_ms: float | None = None
        self._echo_queue: list[tuple[int, str]] = []
        self.sent: list[SentFrame] = []

    # -- TelephonyTransport ------------------------------------------------

    @property
    def chunk_policy(self) -> ChunkPolicy:
        return self._policy

    @property
    def media_format(self) -> AudioFormat:
        return self._media_format

    @property
    def stream_sid(self) -> str:
        return self._stream_sid

    async def events(self) -> AsyncIterator[TelephonyEvent]:
        """Replay the tape, interleaved with mark echoes as they come due."""
        yield ConnectedEvent()
        if self._faults.delay_start_event_ms:
            # HC-5: the bot must respond within ~10 s of connect. Holding `start` back is
            # how a test proves the bridge does not wait for it before saying anything.
            await self._wait(self._faults.delay_start_event_ms)
        yield StartEvent(
            stream_sid=self._stream_sid,
            call_sid="fake-call-sid",
            media_format=self._media_format,
            custom_parameters={"session_id": "fake-session"},
        )

        media_index = 0
        for step in self._script:
            if isinstance(step, Stop):
                break
            async for event in self._replay(step, media_index):
                yield event
            if isinstance(step, InboundAudio):
                media_index += len(self._audio_frames(step))
            for echo in self._due_echoes():
                yield echo

        for echo in self._due_echoes(flush=True):
            yield echo
        self._closed = True
        if self._faults.close_without_stop:
            # A dropped socket rather than a hang-up: the iterator simply ends. The
            # bridge has to finalise on iterator exhaustion, not on a `stop` event.
            return
        yield StopEvent()

    async def _replay(
        self, step: InboundScriptStep, media_index: int
    ) -> AsyncIterator[TelephonyEvent]:
        """Turn one tape step into the events a provider would actually deliver."""
        match step:
            case WaitForOutbound():
                await self._wait_for_media_writes(step.chunks)
            case InboundAudio():
                for offset, frame in enumerate(self._audio_frames(step)):
                    for delivered in self._apply_frame_faults(frame, media_index + offset):
                        yield delivered
                        for echo in self._due_echoes():
                            yield echo
                        await self._wait(step.message_ms)
            case InboundDtmf():
                yield DtmfEvent(digit=step.digit)
            case MalformedFrame():
                # Put through the real decoder, so the failure a test sees is the one
                # production would see, then reported as an event rather than raised.
                try:
                    yield decode_inbound_frame(step.raw, fallback_format=self._media_format)
                except Exception as exc:  # any decode failure is one event, not a dead call
                    yield FrameDecodeFailed(reason=str(exc))

    async def send_media(self, payload: bytes) -> None:
        self._require_open()
        self._assert_payload(payload)
        self._assert_pacing(payload)
        self._media_writes += 1
        self.sent.append(SentFrame(event="media", payload=payload, at_ms=self._now_ms()))

    async def send_mark(self, name: str) -> None:
        self._require_open()
        self.sent.append(SentFrame(event="mark", mark_name=name, at_ms=self._now_ms()))
        self._marks_seen += 1
        if self._is_lost_mark(self._marks_seen):
            return
        self._echo_queue.append((self._media_writes + self._faults.mark_echo_delay, name))

    async def clear_playback(self) -> None:
        self._require_open()
        self.sent.append(SentFrame(event="clear", at_ms=self._now_ms()))
        # A real `clear` discards audio the provider buffered but has not played, so any
        # mark still waiting on that audio will never be echoed. Dropping them here is
        # what makes the post-barge-in ledger state realistic rather than optimistic.
        self._echo_queue.clear()

    # -- outbound assertions ----------------------------------------------

    def _assert_payload(self, payload: bytes) -> None:
        if len(payload) % 2:
            # s16le framing. An odd count means a sample was split, and every subsequent
            # sample would be byte-swapped — loud static, easy to blame on the network.
            raise InvariantViolation(
                "Outbound payload has an odd byte count and cannot be s16le.",
                detail={"bytes": len(payload)},
            )
        if not self._policy.is_legal(len(payload)):
            raise InvariantViolation(
                "Fake provider rejected an illegal outbound media payload.",
                detail={
                    "bytes": len(payload),
                    "min": self._policy.effective_min,
                    "max": self._policy.effective_max,
                    "alignment": self._policy.effective_alignment,
                },
            )

    def _assert_pacing(self, payload: bytes) -> None:
        """Refuse audio that outruns real time by more than the jitter allowance.

        Nothing else can catch this. A bridge that dumps an entire response passes every
        alignment and size check, and then barge-in accounting is guessing across the
        whole utterance instead of across one chunk.

        Skipped when the clock does not advance (`Pace.INSTANT` with no clock), because
        there the assertion would be about the test harness rather than the bridge.
        """
        if self._clock_ms is None:
            return
        now = self._now_ms()
        if self._first_media_at_ms is None:
            self._first_media_at_ms = now
            return
        elapsed = now - self._first_media_at_ms
        already_written = sum(
            ms_of_bytes(len(frame.payload), self._media_format) for frame in self.media_frames
        )
        ahead = already_written + ms_of_bytes(len(payload), self._media_format) - elapsed
        if ahead > self._jitter_allowance_ms:
            raise InvariantViolation(
                "Outbound audio is outrunning real time by more than the jitter allowance.",
                detail={
                    "ahead_ms": round(ahead, 3),
                    "allowance_ms": self._jitter_allowance_ms,
                    "elapsed_ms": round(elapsed, 3),
                },
            )

    async def send_raw(self, text: str) -> None:
        """Accept a fully-encoded frame, decode it, and audit it.

        Used by tests that drive the codec rather than the transport, so the encoder's
        output is checked by the same auditor that checks the bridge's. This is also the
        only path on which "is it a JSON **text** frame" (HC-1) is a meaningful question.
        """
        try:
            frame = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvariantViolation("We wrote a frame that is not valid JSON.") from exc
        if not isinstance(frame, dict):
            raise InvariantViolation("We wrote a frame that is not a JSON object.")
        event = frame.get("event")
        if self._dialect.echo_stream_sid and frame.get("stream_sid") != self._stream_sid:
            raise InvariantViolation(
                "Outbound frame did not echo the stream id this dialect requires.",
                detail={"event": str(event)},
            )
        match event:
            case "media":
                payload = base64.b64decode(str(frame["media"]["payload"]), validate=True)
                await self.send_media(payload)
            case "mark":
                await self.send_mark(str(frame["mark"]["name"]))
            case "clear":
                await self.clear_playback()
            case _:
                raise InvariantViolation(
                    "We wrote an outbound event Exotel does not accept.",
                    detail={"event": str(event)[:40]},
                )

    # -- assertions surface ------------------------------------------------

    @property
    def media_frames(self) -> tuple[SentFrame, ...]:
        return tuple(frame for frame in self.sent if frame.event == "media")

    @property
    def mark_frames(self) -> tuple[SentFrame, ...]:
        return tuple(frame for frame in self.sent if frame.event == "mark")

    @property
    def clear_count(self) -> int:
        return sum(1 for frame in self.sent if frame.event == "clear")

    @property
    def event_order(self) -> tuple[str, ...]:
        return tuple(frame.event for frame in self.sent)

    @property
    def played_bytes(self) -> int:
        return sum(len(frame.payload) for frame in self.media_frames)

    def played_ms(self) -> float:
        return ms_of_bytes(self.played_bytes, self._media_format)

    # -- internals ---------------------------------------------------------

    def _now_ms(self) -> float:
        return float(self._clock_ms()) if self._clock_ms is not None else 0.0

    def _require_open(self) -> None:
        if self._closed:
            raise InvariantViolation("Wrote to a telephony stream that has stopped.")

    def _is_lost_mark(self, ordinal: int) -> bool:
        """Deterministic mark loss. A random rate makes a test fail one run in twenty."""
        if self._faults.mark_loss_rate <= 0.0:
            return False
        if self._faults.mark_loss_rate >= 1.0:
            return True
        every = max(2, round(1 / self._faults.mark_loss_rate))
        return ordinal % every == 0

    async def _wait(self, milliseconds: float) -> None:
        if self._pace is Pace.REALTIME and milliseconds > 0:
            await asyncio.sleep(milliseconds / 1000.0)
        else:
            # A cooperative yield, not a wall-clock sleep: it costs no time and it is
            # what lets the bridge's other task make progress.
            await asyncio.sleep(0)

    def _audio_frames(self, step: InboundAudio) -> list[MediaEvent]:
        frame_bytes = bytes_of_ms(step.message_ms, self._media_format)
        total = bytes_of_ms(step.milliseconds, self._media_format)
        frames: list[MediaEvent] = []
        for sequence, offset in enumerate(range(0, total, frame_bytes or 1)):
            size = min(frame_bytes, total - offset)
            frames.append(
                MediaEvent(
                    payload=b"\x01\x00" * (size // 2),
                    sequence_number=sequence,
                    chunk=sequence,
                    timestamp_ms=offset * 1000 // self._media_format.bytes_per_second,
                )
            )
        return frames

    def _apply_frame_faults(self, frame: MediaEvent, index: int) -> list[MediaEvent]:
        """Turn one tape frame into what the provider actually delivers."""
        if index in self._faults.drop_frames:
            return []
        sequence = frame.sequence_number or 0
        if self._faults.sequence_jump_at is not None and index > self._faults.sequence_jump_at:
            frame = MediaEvent(
                payload=frame.payload,
                sequence_number=sequence + 1000,
                chunk=frame.chunk,
                timestamp_ms=frame.timestamp_ms,
            )
        if self._faults.duplicate_frame == index:
            return [frame, frame]
        return [frame]

    def _due_echoes(self, *, flush: bool = False) -> list[MarkEvent]:
        """Marks whose audio the provider has now finished playing (HC-9)."""
        due: list[MarkEvent] = []
        remaining: list[tuple[int, str]] = []
        for threshold, name in self._echo_queue:
            if flush or self._media_writes >= threshold:
                due.append(MarkEvent(name=name))
            else:
                remaining.append((threshold, name))
        self._echo_queue = remaining
        return due

    async def _wait_for_media_writes(self, count: int) -> None:
        """Yield to the event loop until the bridge has written `count` media frames.

        A bounded spin rather than a sleep: the pacer is on another task and the only
        thing being waited for is its progress, so a wall-clock sleep would be both
        slower and less reliable.
        """
        for _ in range(100_000):
            if self._media_writes >= count:
                return
            await asyncio.sleep(0)
        raise InvariantViolation(
            "The bridge never wrote the expected number of media frames.",
            detail={"expected": count, "written": self._media_writes},
        )
