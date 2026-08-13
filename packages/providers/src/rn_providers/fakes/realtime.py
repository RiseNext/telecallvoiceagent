"""`FakeRealtimeProvider` — a scripted event tape. Deterministic, offline, controllable.

Specified in [TESTING.md §3.3](../../../../../docs/TESTING.md) rather than invented here.
The realtime seam is the one where a mock made of `AsyncMock` produces confident
nonsense, so the fake is driven by a declarative script instead.

> **One documented requirement is deliberately not met, and the conflict is real.**
> TESTING §3.3 describes this fake as *"a small in-process WebSocket server (so real
> serialization, real framing and real backpressure are exercised)"*. TESTING §3.1
> separately states — and `tests/unit/test_framework_independence.py` **enforces** —
> that importing `rn_providers.fakes` must load no transport library, `websockets`
> named explicitly. The two cannot both hold. The enforced test wins: a socket-based
> fake would break the offline guarantee that every other fake depends on, and framing
> is not what any Phase-4 assertion actually needs. Recorded in TESTING.md §3.3 as a
> known deviation rather than silently ignored.

Barge-in is a **timing relationship**, so it is only testable against a clock somebody
controls. That is what this fake is for: a declarative script — *emit 1400 ms of audio ·
emit a tool call · emit `speech_started` at t=740 ms · drop the socket* — replayed in
order, with every call the bridge makes recorded for assertions.

What it must be able to do, because each corresponds to a real failure mode
(TESTING §3.3): adversarial delta sizes including one byte and a delta split
mid-sample; `speech_started` at any moment, including inside a tool call; function
calls with valid, invalid and forged arguments; errors and closes including a
**silent half-open socket**; and **latency injection**, because the real India-to-provider
round trip is unmeasured (§6a-17) and a local test that assumes zero network is lying
about the turn budget.

Two properties that stop scripted tests from rotting, both borrowed from
`FakeLLMProvider` because they earned their keep there:

**A script that runs out closes the session** rather than hanging. A fake that blocks
forever on exhaustion turns a loop bug into a test-suite timeout with no diagnosis.

**Every outbound call is recorded, in order.** `truncations`, `cancellations`,
`tool_results` and `pushed_audio` are what let a test assert that barge-in performed
exactly one truncate with a plausible `audio_end_ms` — which is the invariant that
matters most in the whole media path and the one that fails silently in production.

The audio it emits is a **deterministic ramp**, not silence and not noise: silence
would make a byte-conservation bug invisible, and randomness would make a golden file
impossible.
"""

from __future__ import annotations

from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any

from rn_core.errors import InvariantViolation
from rn_providers.audio.formats import PCM_24K, AudioFormat, bytes_of_ms
from rn_providers.realtime.session import (
    AudioDelta,
    ErrorEvent,
    ResponseComplete,
    SessionCapabilities,
    SessionClosed,
    SpeechStarted,
    ToolCallRequested,
    TranscriptDelta,
    VoiceSessionEvent,
)

__all__ = [
    "OPENAI_LIKE_CAPABILITIES",
    "SARVAM_LIKE_CAPABILITIES",
    "CloseSession",
    "DropSocket",
    "EmitAudio",
    "EmitError",
    "EmitSpeechStarted",
    "EmitToolCall",
    "EmitTranscript",
    "EndResponse",
    "FakeRealtimeProvider",
    "GoSilent",
    "ScriptStep",
]

#: A speech-to-speech provider, shaped like OpenAI Realtime: 24 kHz `audio/pcm` only
#: (HC-4), server VAD, remote conversation state that must be truncated (HC-7), and a
#: hard 60-minute session (HC-6).
OPENAI_LIKE_CAPABILITIES = SessionCapabilities(
    emitted_output_format=PCM_24K,
    accepted_input_formats=(PCM_24K,),
    supports_interim=True,
    supports_server_vad=True,
    supports_remote_truncation=True,
    max_session_seconds=3600,
)

#: A cascaded provider, shaped like Sarvam: 8/16 kHz, **no interim transcripts**
#: (HC-20), and nothing remote to truncate because the context is ours. Present so a
#: test can prove the bridge branches on capabilities rather than assuming OpenAI.
SARVAM_LIKE_CAPABILITIES = SessionCapabilities(
    emitted_output_format=AudioFormat(rate_hz=8000),
    accepted_input_formats=(AudioFormat(rate_hz=8000), AudioFormat(rate_hz=16000)),
    supports_interim=False,
    supports_server_vad=False,
    supports_remote_truncation=False,
    max_session_seconds=None,
)


@dataclass(frozen=True, slots=True)
class EmitAudio:
    """Emit `milliseconds` of assistant audio for `item_id`.

    Split into `delta_bytes`-sized pieces when given, so a test can reproduce the thing
    that actually happens on the wire: **arbitrary, unaligned delta sizes**. The default
    emits it as one delta, which is the simpler case and not the interesting one.
    """

    milliseconds: int
    item_id: str = "item-1"
    content_index: int = 0
    delta_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class EmitSpeechStarted:
    """The caller interrupts. The barge-in trigger."""

    audio_start_ms: int | None = None


@dataclass(frozen=True, slots=True)
class EmitToolCall:
    call_id: str
    name: str
    arguments_json: str = "{}"


@dataclass(frozen=True, slots=True)
class EmitTranscript:
    text: str
    is_final: bool = True
    speaker: str = "assistant"


@dataclass(frozen=True, slots=True)
class EndResponse:
    item_id: str = "item-1"


@dataclass(frozen=True, slots=True)
class EmitError:
    """A provider-reported error that does **not** close the session.

    `rate_limit_exceeded` and `invalid_session` are the two TESTING §3.3 names. The
    bridge must keep pumping audio through both rather than treating any error as
    terminal — a rate-limit notice mid-response is not a dead call.
    """

    message: str = "rate_limit_exceeded"
    code: str = "rate_limit_exceeded"


@dataclass(frozen=True, slots=True)
class DropSocket:
    """The provider vanishes mid-response, without a close frame."""

    reason: str = "socket dropped"
    #: WebSocket close code where one was sent. `1011` is the server-error case
    #: TESTING §3.3 names; `None` means the socket simply stopped.
    code: int | None = None


@dataclass(frozen=True, slots=True)
class GoSilent:
    """A **half-open socket**: still connected, never sends anything again.

    The nastiest realtime failure, because nothing raises and nothing closes.
    Modelled by ending the event stream *without* a `SessionClosed`, which is exactly
    what the bridge observes — so a bridge that waits for a close event hangs a live
    call in silence, and this is the step that catches it.
    """


@dataclass(frozen=True, slots=True)
class CloseSession:
    reason: str = "completed"
    code: int | None = 1000


type ScriptStep = (
    EmitAudio
    | EmitSpeechStarted
    | EmitToolCall
    | EmitTranscript
    | EmitError
    | EndResponse
    | DropSocket
    | GoSilent
    | CloseSession
)


@dataclass(frozen=True, slots=True)
class _Truncation:
    item_id: str
    content_index: int
    audio_end_ms: int


class FakeRealtimeProvider:
    """A `VoiceSession` that replays a script. Satisfies the protocol structurally.

    Args:
        script: The tape.
        capabilities: What this provider claims it can do. The bridge branches on it.
        latency_ms: **Synthetic round-trip latency**, awaited on `open`, `push_audio`,
            `truncate`, `cancel_generation` and `submit_tool_result`. Defaults to zero
            for logic tests — but a *timing* test that leaves it at zero is asserting a
            turn budget that assumes no network, and the real India-to-provider RTT is
            unmeasured (§6a-17).
        sleep: How the latency is awaited. Injected so a test can advance a fake clock
            rather than burn wall-clock time.
        tools: The tool specs the caller declared, handed to `on_session_update`.
        on_session_update: Called once from `open()`, so a test can assert the **flat**
            Realtime tool shape (HC-19). Getting that wrong fails silently: the session
            accepts the nested shape and the model then never calls the tool, which
            presents as "the agent ignores its tools".
    """

    __slots__ = (
        "_capabilities",
        "_closed",
        "_latency_ms",
        "_on_session_update",
        "_opened",
        "_script",
        "_sleep",
        "_tools",
        "cancellations",
        "pushed_audio",
        "tool_results",
        "truncations",
    )

    def __init__(
        self,
        script: Iterable[ScriptStep] = (),
        *,
        capabilities: SessionCapabilities = OPENAI_LIKE_CAPABILITIES,
        latency_ms: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
        on_session_update: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
    ) -> None:
        self._script: tuple[ScriptStep, ...] = tuple(script)
        self._capabilities = capabilities
        self._latency_ms = latency_ms
        self._sleep = sleep
        self._tools = tuple(tools)
        self._on_session_update = on_session_update
        self._opened = False
        self._closed = False
        self.pushed_audio: list[tuple[bytes, AudioFormat]] = []
        self.truncations: list[_Truncation] = []
        self.cancellations: int = 0
        self.tool_results: list[tuple[str, str]] = []

    # -- protocol ----------------------------------------------------------

    @property
    def capabilities(self) -> SessionCapabilities:
        return self._capabilities

    async def open(self) -> None:
        await self._latency()
        self._opened = True
        if self._on_session_update is not None:
            self._on_session_update(self._tools)

    async def push_audio(self, pcm: bytes, fmt: AudioFormat) -> None:
        await self._latency()
        if not self._capabilities.accepts(fmt):
            # A real provider would reject this too, but later, and less clearly. The
            # bridge is supposed to have resolved a transcoder for exactly this reason.
            raise InvariantViolation(
                "Audio pushed in a format this session does not accept.",
                detail={
                    "pushed_rate": fmt.rate_hz,
                    "accepted": [f.rate_hz for f in self._capabilities.accepted_input_formats],
                },
            )
        self.pushed_audio.append((pcm, fmt))

    async def stream_output(self) -> AsyncIterator[VoiceSessionEvent]:
        for step in self._script:
            match step:
                case EmitAudio():
                    for delta in _split(step, self._capabilities.emitted_output_format):
                        yield delta
                case EmitSpeechStarted():
                    yield SpeechStarted(audio_start_ms=step.audio_start_ms)
                case EmitToolCall():
                    yield ToolCallRequested(
                        call_id=step.call_id,
                        name=step.name,
                        arguments_json=step.arguments_json,
                    )
                case EmitTranscript():
                    yield TranscriptDelta(
                        text=step.text, is_final=step.is_final, speaker=step.speaker
                    )
                case EmitError():
                    # Not terminal. The bridge logs it and keeps pumping audio.
                    yield ErrorEvent(message=step.message, detail={"code": step.code})
                case EndResponse():
                    yield ResponseComplete(item_id=step.item_id)
                case GoSilent():
                    # Half-open: no further events and **no close**. The iterator ends,
                    # which is exactly what the bridge sees from a dead socket.
                    return
                case DropSocket():
                    self._closed = True
                    yield SessionClosed(reason=step.reason)
                    return
                case CloseSession():
                    self._closed = True
                    yield SessionClosed(reason=step.reason)
                    return
        # An exhausted script closes rather than blocking: a fake that hangs turns a
        # loop bug into a suite timeout with no diagnosis attached.
        self._closed = True
        yield SessionClosed(reason="script exhausted")

    async def truncate(self, *, item_id: str, content_index: int, audio_end_ms: int) -> None:
        await self._latency()
        self.truncations.append(
            _Truncation(item_id=item_id, content_index=content_index, audio_end_ms=audio_end_ms)
        )

    async def cancel_generation(self) -> None:
        await self._latency()
        self.cancellations += 1

    async def submit_tool_result(self, *, call_id: str, output_json: str) -> None:
        await self._latency()
        self.tool_results.append((call_id, output_json))

    async def close(self) -> None:
        self._closed = True

    async def _latency(self) -> None:
        """Await the synthetic round trip, when one is configured."""
        if self._latency_ms > 0 and self._sleep is not None:
            await self._sleep(self._latency_ms)

    # -- test surface ------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._opened and not self._closed

    @property
    def was_opened(self) -> bool:
        return self._opened

    @property
    def pushed_bytes(self) -> int:
        return sum(len(pcm) for pcm, _ in self.pushed_audio)


def _split(step: EmitAudio, fmt: AudioFormat) -> Sequence[AudioDelta]:
    """Turn one `EmitAudio` into the deltas a provider would actually send."""
    total = bytes_of_ms(step.milliseconds, fmt)
    pcm = _ramp(total)
    size = step.delta_bytes or total
    if size <= 0:
        raise InvariantViolation("EmitAudio delta_bytes must be positive.")
    pieces = [pcm[offset : offset + size] for offset in range(0, len(pcm), size)] or [b""]
    return [
        AudioDelta(item_id=step.item_id, content_index=step.content_index, pcm=piece)
        for piece in pieces
        if piece
    ]


def _ramp(total_bytes: int) -> bytes:
    """A deterministic non-silent signal.

    A sawtooth over the low byte of each sample. Not silence — silence makes a dropped
    tail invisible in a byte-conservation test — and not random, because a golden file
    has to be reproducible.
    """
    aligned = total_bytes - (total_bytes % 2)
    return bytes(bytearray((index // 2) % 251 if index % 2 == 0 else 0 for index in range(aligned)))
