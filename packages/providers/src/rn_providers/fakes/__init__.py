"""In-repository fakes for every provider seam.

These are **first-class code, not test helpers.** They live in `rn_providers`
rather than in `tests/` for two reasons: a fake is the executable definition of
what a seam promises, and every consumer of a seam — unit tests, integration
tests, the evaluation harness, a local demo — needs the same one. A fake copied
into a test directory drifts from the interface it fakes, and the drift is
invisible until a real adapter arrives.

Phase 2 ships the LLM fake only, because Phase 2 has only one seam. Telephony,
realtime voice, STT, TTS, messaging and storage fakes arrive with their seams.

**Nothing here performs I/O.** No sockets, no sleeps, no clock reads, no
randomness. A fake that is not deterministic is a source of flakes rather than a
defence against them.
"""

from rn_providers.fakes.llm import (
    FakeLLMProvider,
    ScriptedToolCall,
    ScriptedTurn,
    ScriptExhaustedError,
)

__all__ = [
    "FakeLLMProvider",
    "ScriptExhaustedError",
    "ScriptedToolCall",
    "ScriptedTurn",
]
