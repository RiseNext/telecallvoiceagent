"""In-repository fakes for every provider seam.

These are **first-class code, not test helpers.** They live in `rn_providers`
rather than in `tests/` for two reasons: a fake is the executable definition of
what a seam promises, and every consumer of a seam — unit tests, integration
tests, the evaluation harness, a local demo — needs the same one. A fake copied
into a test directory drifts from the interface it fakes, and the drift is
invisible until a real adapter arrives.

Four fakes exist, one per seam that exists: the LLM fake (Phase 2), the embedding
fake (Phase 3), and the realtime-voice and telephony fakes (Phase 4). STT, TTS,
messaging and storage fakes arrive with their seams.

**Nothing here performs I/O.** No sockets, no wall-clock sleeps, no clock reads,
no randomness — and, asserted by `tests/unit/test_framework_independence.py`, no
transport library either: importing this package must not load `httpx`,
`websockets` or a vendor SDK. That is why concrete adapters are reached by their
own module rather than re-exported from `rn_providers`.

The telephony fake does `await asyncio.sleep(0)` to yield to the event loop while
waiting for the bridge's other task to make progress. That is a cooperative yield,
not a wall-clock sleep: it costs no time and it is what lets an end-to-end call
simulation run to completion in milliseconds.

Determinism here means *reproducible across processes*, not merely "no `random`
call": `hash()` is salted per process for `str`, so a fake that hashed text with it
would return different values in two interpreters. The embedding fake uses
`hashlib` for exactly that reason.
"""

from rn_providers.fakes.embeddings import FakeEmbeddingProvider
from rn_providers.fakes.llm import (
    FakeLLMProvider,
    ScriptedToolCall,
    ScriptedTurn,
    ScriptExhaustedError,
)
from rn_providers.fakes.realtime import (
    OPENAI_LIKE_CAPABILITIES,
    SARVAM_LIKE_CAPABILITIES,
    CloseSession,
    DropSocket,
    EmitAudio,
    EmitError,
    EmitSpeechStarted,
    EmitToolCall,
    EmitTranscript,
    EndResponse,
    FakeRealtimeProvider,
    GoSilent,
    ScriptStep,
)
from rn_providers.fakes.telephony import (
    DEFAULT_JITTER_ALLOWANCE_MS,
    FakeTelephonyProvider,
    InboundAudio,
    InboundDtmf,
    InboundScriptStep,
    MalformedFrame,
    Pace,
    SentFrame,
    Stop,
    TelephonyFaults,
    WaitForOutbound,
)

__all__ = [
    "DEFAULT_JITTER_ALLOWANCE_MS",
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
    "FakeEmbeddingProvider",
    "FakeLLMProvider",
    "FakeRealtimeProvider",
    "FakeTelephonyProvider",
    "GoSilent",
    "InboundAudio",
    "InboundDtmf",
    "InboundScriptStep",
    "MalformedFrame",
    "Pace",
    "ScriptExhaustedError",
    "ScriptStep",
    "ScriptedToolCall",
    "ScriptedTurn",
    "SentFrame",
    "Stop",
    "TelephonyFaults",
    "WaitForOutbound",
]
