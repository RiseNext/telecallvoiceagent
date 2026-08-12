"""In-repository fakes for every provider seam.

These are **first-class code, not test helpers.** They live in `rn_providers`
rather than in `tests/` for two reasons: a fake is the executable definition of
what a seam promises, and every consumer of a seam — unit tests, integration
tests, the evaluation harness, a local demo — needs the same one. A fake copied
into a test directory drifts from the interface it fakes, and the drift is
invisible until a real adapter arrives.

Two fakes exist, one per seam that exists: the LLM fake (Phase 2) and the
embedding fake (Phase 3). Telephony, realtime voice, STT, TTS, messaging and
storage fakes arrive with their seams.

**Nothing here performs I/O.** No sockets, no sleeps, no clock reads, no
randomness — and, asserted by `tests/unit/test_framework_independence.py`, no
transport library either: importing this package must not load `httpx`,
`websockets` or a vendor SDK. That is why concrete adapters are reached by their
own module rather than re-exported from `rn_providers`.

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

__all__ = [
    "FakeEmbeddingProvider",
    "FakeLLMProvider",
    "ScriptExhaustedError",
    "ScriptedToolCall",
    "ScriptedTurn",
]
