"""The bridge: telephony on one side, a realtime session on the other.

This is the loop the whole media plane exists to serve. Two pumps run concurrently —

    caller audio   → transcode → session.push_audio
    session audio  → transcode → ring buffer → pacer → telephony

— plus a barge-in trigger that crosses between them, and a tool-call sink that leaves
the audio path entirely.

**What is deliberately *not* here.** This is Phase 4: the bridge is driven by fakes and
proves the byte pipeline. Session pre-warming and the 10-second connect deadline,
tool dispatch on a separate task with filler speech, the agent-snapshot cache, session
rollover across the two independent 60-minute clocks, and turn-latency instrumentation
are **Phase 5**. Building them now against a fake provider would mean writing them
twice.

**The tool sink is a callable, and that is a layering decision.** `rn_voice` does not
import `rn_agent`: a tool call arrives here as three strings, is handed to whatever the
composition root injected, and the bridge returns to pumping audio. That keeps the
gateway's dependency on the agent runtime at zero for the whole of Phase 4, and it is
what lets the end-to-end test wire the *real* dispatcher in without the bridge knowing.

**Barge-in has one call site and it is here.** `rn_voice.media.handle_barge_in` is
called from exactly one place in this file. Anything that looks like it needs a second
call site is a bug in this comment's premise, not a reason to add one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from rn_core.logging import get_logger
from rn_providers.audio.transcoder import ResamplerQuality, resolve_transcoder
from rn_providers.realtime.session import (
    AudioDelta,
    ErrorEvent,
    ResponseComplete,
    SessionClosed,
    SpeechStarted,
    SpeechStopped,
    ToolCallRequested,
    TranscriptDelta,
    VoiceSession,
)
from rn_providers.telephony.base import (
    ConnectedEvent,
    DtmfEvent,
    FrameDecodeFailed,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
    TelephonyTransport,
)
from rn_voice.media.align import SampleAligner
from rn_voice.media.bargein import BargeInOutcome, handle_barge_in
from rn_voice.media.clock import Clock, SystemClock
from rn_voice.media.ledger import PlaybackLedger
from rn_voice.media.pacer import DEFAULT_LEAD_CHUNKS, Pacer, PacerSinks
from rn_voice.media.ring import OutboundRingBuffer
from rn_voice.media.tap import MediaDirection, MediaTap, NullMediaTap

__all__ = ["AudioBridge", "BridgeResult", "ToolCallSink"]

_logger = get_logger(__name__)

#: Handles one tool call and returns the JSON text to give back to the model.
#:
#: A callable, not a service handle: `rn_voice` must not import `rn_agent`, and a tool
#: call on this side of the seam is three strings and nothing more.
type ToolCallSink = Callable[[ToolCallRequested], Awaitable[str]]


@dataclass(slots=True)
class BridgeResult:
    """What one bridged call did. Counts and durations only — never audio, never text."""

    inbound_frames: int = 0
    inbound_bytes: int = 0
    outbound_chunks: int = 0
    outbound_bytes: int = 0
    marks_echoed: int = 0
    barge_ins: list[BargeInOutcome] = field(default_factory=list)
    tool_calls: int = 0
    decode_errors: int = 0
    dtmf_digits: list[str] = field(default_factory=list)
    stop_reason: str = "completed"

    @property
    def barge_in_count(self) -> int:
        return len(self.barge_ins)


class AudioBridge:
    """Wires one telephony stream to one realtime session.

    Args:
        transport: The telephony side. Its `chunk_policy` and `media_format` are read
            once at start and obeyed for the life of the call.
        session: The realtime side.
        tap: Media observer for the D-5 recording decision. Defaults to a disabled
            no-op; see `rn_voice.media.tap`. It may not block and may not raise.
        tool_sink: Where tool calls go. `None` means tool calls are counted and
            acknowledged with an empty result — enough for a media-path test, and not a
            substitute for Phase 5's dispatcher.
        clock: Injected. Nothing in the audio path reads `time` directly.
        sleep: Injected, so a test runs a 20-second response in no wall-clock time.
        quality: Resampler preset (ADR-003: start high).
        lead_chunks: How shallow the pacing lead is. See `Pacer`.
    """

    __slots__ = (
        "_buffer",
        "_clock",
        "_from_session",
        "_inbound_align",
        "_lead_chunks",
        "_ledger",
        "_outbound_align",
        "_pacer",
        "_quality",
        "_result",
        "_session",
        "_sleep",
        "_started",
        "_tap",
        "_to_session",
        "_tool_sink",
        "_transport",
    )

    def __init__(
        self,
        *,
        transport: TelephonyTransport,
        session: VoiceSession,
        tool_sink: ToolCallSink | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        quality: ResamplerQuality = ResamplerQuality.HIGH,
        lead_chunks: int = DEFAULT_LEAD_CHUNKS,
        tap: MediaTap | None = None,
    ) -> None:
        self._transport = transport
        self._session = session
        self._tool_sink = tool_sink
        self._clock = clock or SystemClock()
        self._sleep = sleep or _default_sleep
        # Open decision D-5 (recording) is Phase 8. The tap point exists now because
        # retrofitting one into a latency-critical loop is expensive and leaving an
        # unused, disabled-by-default one is not (ROADMAP, Phase 4).
        self._tap = tap or NullMediaTap()
        self._quality = quality
        self._lead_chunks = lead_chunks
        self._started = False
        self._result = BridgeResult()
        # One aligner per direction. A shared one would splice a byte of the caller's
        # audio into the agent's, which is the exact bug it exists to prevent.
        self._inbound_align = SampleAligner()
        self._outbound_align = SampleAligner()

        telephony_format = transport.media_format
        self._buffer = OutboundRingBuffer(policy=transport.chunk_policy)
        self._ledger = PlaybackLedger(fmt=telephony_format, clock=self._clock)
        self._pacer = Pacer(
            buffer=self._buffer,
            ledger=self._ledger,
            fmt=telephony_format,
            clock=self._clock,
            sinks=PacerSinks(
                # Wrapped rather than passed straight through, so the byte total is
                # counted at the one place every outbound payload actually goes.
                send_media=self._send_media_counted,
                send_mark=transport.send_mark,
                sleep=self._sleep,
            ),
            lead_chunks=lead_chunks,
        )
        # Transcoders are resolved here, at session open, from the two declared formats
        # — never chosen at build time. This is what makes a 24 kHz OpenAI-primary agent
        # and an 8 kHz Sarvam-primary agent the same code path, with no branch.
        capabilities = session.capabilities
        self._to_session = resolve_transcoder(
            source=telephony_format,
            target=capabilities.preferred_input_format,
            quality=quality,
        )
        self._from_session = resolve_transcoder(
            source=capabilities.emitted_output_format,
            target=telephony_format,
            quality=quality,
        )

    async def _send_media_counted(self, payload: bytes) -> None:
        await self._transport.send_media(payload)
        self._result.outbound_bytes += len(payload)
        # After transcoding and alignment, so what is observed is what was actually
        # played rather than what the model generated.
        self._observe(MediaDirection.OUTBOUND, payload)

    def _observe(self, direction: MediaDirection, pcm: bytes) -> None:
        """Hand a frame to the tap, and never let the tap break the call.

        A recording feature that can drop a call is worse than no recording feature,
        so a misbehaving tap is disabled for the rest of the call rather than allowed
        to raise into the audio pump.
        """
        try:
            self._tap.observe(direction, pcm)
        except Exception:
            _logger.warning("voice.media.tap_failed", direction=direction.value)
            self._tap = NullMediaTap()

    @property
    def ledger(self) -> PlaybackLedger:
        return self._ledger

    @property
    def result(self) -> BridgeResult:
        return self._result

    async def run(self) -> BridgeResult:
        """Pump both directions until the telephony stream stops.

        The two pumps are separate tasks because they are genuinely independent: caller
        audio must keep flowing into the model while the model's audio is being paced
        out, and a single loop that alternated would stall one on the other.
        """
        await self._session.open()
        outbound = asyncio.create_task(self._pump_session())
        try:
            await self._pump_telephony()
        finally:
            outbound.cancel()
            # Awaited so a failure inside the outbound pump surfaces here rather than as
            # a "task exception was never retrieved" warning after the call is over.
            with contextlib.suppress(asyncio.CancelledError):
                await outbound
            await self._session.close()
        return self._result

    # -- caller -> model ---------------------------------------------------

    async def _pump_telephony(self) -> None:
        async for event in self._transport.events():
            match event:
                case ConnectedEvent():
                    continue
                case StartEvent():
                    self._started = True
                case MediaEvent():
                    self._result.inbound_frames += 1
                    self._result.inbound_bytes += len(event.payload)
                    self._observe(MediaDirection.INBOUND, event.payload)
                    # A telephony frame is a byte count, not a sample count: an odd one
                    # would byte-swap every subsequent sample if it were passed straight
                    # through. The orphan byte rides along to the next frame.
                    converted = self._to_session.process(self._inbound_align.feed(event.payload))
                    if converted:
                        await self._session.push_audio(
                            converted, self._session.capabilities.preferred_input_format
                        )
                case MarkEvent():
                    self._result.marks_echoed += 1
                    self._ledger.note_mark_echoed(event.name)
                case DtmfEvent():
                    self._result.dtmf_digits.append(event.digit)
                case FrameDecodeFailed():
                    # One bad frame, not a dead call. Counted and logged without any
                    # payload — a frame that failed to decode may still hold caller
                    # audio, and the point of the log line is diagnosis, not capture.
                    self._result.decode_errors += 1
                    _logger.warning("voice.telephony.frame_rejected", reason=event.reason)
                case StopEvent():
                    await self._flush_inbound()
                    self._result.stop_reason = "telephony_stopped"
                    break

    async def _flush_inbound(self) -> None:
        """Drain the inbound resampler so the caller's last words are not lost.

        A streaming resampler holds a filter tail — tens of milliseconds at 8→24 kHz.
        Without this the final fragment of every call never reaches the model, which is
        invisible mid-conversation and shows up as the agent ignoring the last thing the
        caller said before hanging up.
        """
        tail = self._to_session.flush()
        self._to_session.reset()
        self._inbound_align.reset()
        if tail:
            await self._session.push_audio(tail, self._session.capabilities.preferred_input_format)

    # -- model -> caller ---------------------------------------------------

    async def _pump_session(self) -> None:
        async for event in self._session.stream_output():
            match event:
                case AudioDelta():
                    await self._on_audio_delta(event)
                case SpeechStarted():
                    # THE ONLY CALL SITE. See the module docstring.
                    outcome = await handle_barge_in(
                        ledger=self._ledger,
                        buffer=self._buffer,
                        session=self._session,
                        clear_playback=self._transport.clear_playback,
                    )
                    self._result.barge_ins.append(outcome)
                    self._pacer.reset_playhead()
                    # The freeze is released once the interruption has been accounted
                    # for; the next item resets the ledger anyway, and leaving it frozen
                    # would pin the estimate for a response that has already ended.
                    self._ledger.resume()
                    self._from_session.reset()
                    # A carried byte across a discontinuity would put the tail of the
                    # abandoned utterance at the head of the next one.
                    self._outbound_align.reset()
                case ToolCallRequested():
                    await self._on_tool_call(event)
                case ResponseComplete():
                    # Drain the tail, padding the final sub-minimum chunk. Without this
                    # the last words of every utterance sit in the buffer until the next
                    # response pushes them out, which sounds like the agent swallowing
                    # its own sentence.
                    await self._flush_tail()
                case SpeechStopped() | TranscriptDelta():
                    continue
                case ErrorEvent():
                    _logger.warning("voice.session.error", message=event.message)
                case SessionClosed():
                    await self._flush_tail()
                    self._result.stop_reason = "session_closed"
                    return

    async def _on_audio_delta(self, event: AudioDelta) -> None:
        # Model deltas arrive at arbitrary sizes and are under no obligation to end on a
        # sample boundary — so this is the general case, not a defensive one.
        converted = self._from_session.process(self._outbound_align.feed(event.pcm))
        if converted:
            self._buffer.append(converted, item_id=event.item_id, content_index=event.content_index)
        written = await self._pacer.drain()
        self._result.outbound_chunks += written

    async def _flush_tail(self) -> None:
        # Flush drains the resampler's filter tail, and a streaming resampler refuses
        # further input afterwards — so the reset immediately after is what lets the
        # *next* response use the same transcoder. Without it, a second utterance on the
        # same call dies inside the resampler rather than at a seam anyone would look at.
        tail = self._from_session.flush()
        self._from_session.reset()
        self._outbound_align.reset()
        if tail and self._buffer.item_id is not None:
            self._buffer.append(
                tail,
                item_id=self._buffer.item_id,
                content_index=self._ledger.content_index,
            )
        self._result.outbound_chunks += await self._pacer.drain(final=True)

    async def _on_tool_call(self, event: ToolCallRequested) -> None:
        """Hand a tool call off and give the model whatever came back.

        Awaited inline in Phase 4. **Phase 5 moves this to a separate task** so audio
        keeps flowing while a slow tool runs, together with the filler-acknowledgement
        policy that makes the pause sound intentional. Doing it now against a fake
        session would be writing it twice.
        """
        self._result.tool_calls += 1
        if self._tool_sink is None:
            await self._session.submit_tool_result(call_id=event.call_id, output_json="{}")
            return
        output = await self._tool_sink(event)
        await self._session.submit_tool_result(call_id=event.call_id, output_json=output)


async def _default_sleep(milliseconds: float) -> None:
    await asyncio.sleep(max(0.0, milliseconds) / 1000.0)
