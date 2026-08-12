"""RiseNext provider adapters — telephony, realtime voice, STT, TTS, LLM, embeddings, messaging, storage.

Every external system sits behind an interface declared here, so that swapping a
vendor is an adapter change rather than a change to business logic.

**What exists: the text-mode `LLMProvider` seam (Phase 2) and the
`EmbeddingProvider` seam (Phase 3), each with a deterministic offline fake.** The
realtime voice session, telephony, STT/TTS, messaging and storage seams arrive
with the phases that need them.

**Concrete adapters are deliberately NOT re-exported here.** Import one by its own
module — `rn_providers.openai_embeddings` — for a reason a test enforces:
importing any submodule runs this `__init__` first, so an adapter exported here
would drag `httpx` into every process that touches `rn_providers.fakes`, and
`tests/unit/test_framework_independence.py` asserts the fakes pull in no transport
library at all. Keeping this module free of adapters is what makes an offline,
dependency-light fake path genuinely offline.
"""

from rn_providers.embeddings import (
    EmbeddingBatch,
    EmbeddingProvider,
    EmbeddingUsage,
    EmbeddingVector,
    TextRole,
)
from rn_providers.llm import (
    Completion,
    FinishReason,
    LLMProvider,
    Message,
    MessageRole,
    ToolCallRequest,
    ToolSpecPayload,
    Usage,
    assistant_message,
    system_message,
    tool_result_message,
    user_message,
)

__version__ = "0.1.0"

__all__ = [
    "Completion",
    "EmbeddingBatch",
    "EmbeddingProvider",
    "EmbeddingUsage",
    "EmbeddingVector",
    "FinishReason",
    "LLMProvider",
    "Message",
    "MessageRole",
    "TextRole",
    "ToolCallRequest",
    "ToolSpecPayload",
    "Usage",
    "assistant_message",
    "system_message",
    "tool_result_message",
    "user_message",
]
