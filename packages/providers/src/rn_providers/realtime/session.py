"""The realtime voice seam: one session, whatever is behind it.

Two very different things must satisfy this protocol: **OpenAI Realtime**, a single
speech-to-speech socket, and the **Sarvam cascade**, which is STT → LLM → TTS across
four sockets and an HTTP call. Phase 5 writes the first; a later phase writes the
second. Phase 4 writes only the seam and a deterministic fake, which is what makes the
bridge testable before either exists.

**The differences are exposed, not hidden.** `SessionCapabilities` is a required
property rather than a set of optional methods, because an interface that pretends the
two providers are the same produces a fallback path that fails silently at 2 a.m. The
sharpest example is `supports_interim`: Sarvam's STT WebSocket emits **nothing** until
VAD end-of-speech (HC-20), so a turn-taking layer written against streamed partials
simply stops working on the fallback path, with no error. It exists from day one, and
nothing may fake partials to paper over it.

**Barge-in is unified at the *effect* level, not the mechanism.** `truncate()` is what
OpenAI needs — `conversation.item.truncate` with a truthful `audio_end_ms` (HC-7) —
and `cancel_generation()` is what the cascade needs, where there is no remote
conversation state to truncate and the work is flushing a TTS socket. A caller performs
both; a session that does not need one of them implements it as a no-op and says so
through `supports_remote_truncation`.

**No vendor SDK is imported here or anywhere below `rn_providers`.** An import-linter
contract enforces it, and this module is a `Protocol` plus frozen event types on
purpose: it can be satisfied by a fake with no network at all, which is the whole
basis of Phase 4's "zero paid API calls" criterion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rn_providers.audio.formats import AudioFormat

__all__ = [
    "AudioDelta",
    "ErrorEvent",
    "ResponseComplete",
    "SessionCapabilities",
    "SessionClosed",
    "SpeechStarted",
    "SpeechStopped",
    "ToolCallRequested",
    "TranscriptDelta",
    "VoiceSession",
    "VoiceSessionEvent",
]


@dataclass(frozen=True, slots=True)
class SessionCapabilities:
    """What this provider can actually do. Branch on it; never assume it.

    Attributes:
        supports_interim: Whether partial transcripts arrive before end-of-speech.
            `False` for Sarvam (HC-20) and it is not negotiable — the turn-taking layer
            needs a genuine VAD-only path rather than synthesised partials.
        supports_server_vad: Whether the provider decides turn boundaries. When `False`
            the bridge must run its own VAD, and barge-in has a different trigger.
        supports_remote_truncation: Whether `truncate()` means anything. `True` for
            OpenAI, where the model holds conversation state and must be corrected
            (HC-7). `False` for the cascade, where the context is ours.
        emitted_output_format: What `AudioDelta.pcm` is in. The bridge resolves its
            transcoder from this and the telephony rate — which is why an OpenAI-primary
            agent at 24 kHz resamples nothing.
        accepted_input_formats: What `push_audio` will take. Ordered by preference, so a
            bridge can pick the one that avoids conversion.
        max_session_seconds: Hard provider-side lifetime, or `None` if unbounded.
            OpenAI's is 60 minutes (HC-6) and it runs on a *different clock* from
            Exotel's 60-minute stream cap (HC-5) — they coincide numerically and must
            never be treated as one. Session rollover is Phase 5.
    """

    emitted_output_format: AudioFormat
    accepted_input_formats: tuple[AudioFormat, ...]
    supports_interim: bool
    supports_server_vad: bool
    supports_remote_truncation: bool
    max_session_seconds: int | None = None

    def accepts(self, fmt: AudioFormat) -> bool:
        return fmt in self.accepted_input_formats

    @property
    def preferred_input_format(self) -> AudioFormat:
        """The format to feed when there is a choice. First is best."""
        return self.accepted_input_formats[0]


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioDelta:
    """A slice of assistant audio.

    **`item_id` is load-bearing.** `conversation.item.truncate` takes an item id and an
    `audio_end_ms` measured *from the start of that item's audio*, so playback
    accounting resets whenever this changes. A ledger that ignores it is a guaranteed,
    silent, unbounded corruption of the value HC-7 requires us to report truthfully —
    which is why `PlaybackLedger` keys on it and a test asserts two items produce two
    ledgers.

    Deltas arrive at **arbitrary sizes** (HC-4 context). Anything that assumes a frame
    boundary here is wrong; that is the ring buffer's job.
    """

    item_id: str
    content_index: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    """The caller began speaking — the barge-in trigger.

    On OpenAI this is `input_audio_buffer.speech_started`, a **server-side** VAD
    decision, which means the round trip to the model sits inside our barge-in latency
    and is unmeasured (§13-4).
    """

    #: Provider-reported offset into the input buffer, when it gives one. Advisory:
    #: playback accounting never derives from it.
    audio_start_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SpeechStopped:
    audio_end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptDelta:
    """Text for a turn. `is_final=False` never appears when `supports_interim` is False."""

    text: str
    is_final: bool
    speaker: str = "assistant"


@dataclass(frozen=True, slots=True)
class ToolCallRequested:
    """The model wants a tool run.

    Carries the arguments as **text**, exactly as the provider sent them, because
    parsing them is `rn_agent`'s job and doing it here would put an untrusted-input
    boundary in the transport layer. `rn_voice` never interprets this — it hands it to
    a sink and returns to pumping audio.
    """

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ResponseComplete:
    """The assistant finished a response. No further audio for `item_id`."""

    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionClosed:
    """The session ended, cleanly or otherwise."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """A provider-reported error that did not close the session."""

    message: str
    detail: dict[str, Any] = field(default_factory=dict)


type VoiceSessionEvent = (
    AudioDelta
    | SpeechStarted
    | SpeechStopped
    | TranscriptDelta
    | ToolCallRequested
    | ResponseComplete
    | SessionClosed
    | ErrorEvent
)


@runtime_checkable
class VoiceSession(Protocol):
    """One live realtime session with a voice provider."""

    @property
    def capabilities(self) -> SessionCapabilities: ...

    async def open(self) -> None:
        """Establish the session. Must not be called inside a telephony accept path.

        Exotel requires a bot response within 10 seconds of connect (HC-5), and the
        bridge satisfies that deadline with a silence frame *independently* of whether
        this has finished — so this being slow degrades the greeting, never the call.
        """
        ...

    async def push_audio(self, pcm: bytes, fmt: AudioFormat) -> None:
        """Send caller audio. `fmt` must be one the capabilities accept."""
        ...

    def stream_output(self) -> AsyncIterator[VoiceSessionEvent]:
        """Provider events, in arrival order, until the session closes."""
        ...

    async def truncate(self, *, item_id: str, content_index: int, audio_end_ms: int) -> None:
        """Correct the model's belief about what the caller heard (HC-7).

        `audio_end_ms` must be **truthful and biased low**. Over-reporting makes the
        model reference content the caller never heard — no error, no log line, clean
        audio, and an unrecoverable conversation.
        """
        ...

    async def cancel_generation(self) -> None:
        """Stop producing audio for the current response.

        The cascade's half of barge-in: flush the TTS socket and drop buffered text.
        A speech-to-speech session may implement it as a no-op.
        """
        ...

    async def submit_tool_result(self, *, call_id: str, output_json: str) -> None:
        """Return a tool envelope to the model, as text."""
        ...

    async def close(self) -> None:
        """Release the session. Idempotent."""
        ...


def formats_tuple(*formats: AudioFormat) -> Sequence[AudioFormat]:
    """Tiny helper so capability declarations read as a list rather than a tuple literal."""
    return tuple(formats)
