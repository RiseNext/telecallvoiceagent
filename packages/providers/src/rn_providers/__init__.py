"""RiseNext provider adapters — telephony, realtime voice, STT, TTS, LLM, embeddings, messaging, storage.

Every external system sits behind an interface declared here, so that swapping a
vendor is an adapter change rather than a change to business logic.

**Phase 2 delivers the text-mode `LLMProvider` seam and its fake, and nothing
else.** There is no vendor adapter in this package yet: the realtime voice
session, telephony, STT/TTS, messaging and storage seams arrive with the phases
that need them. `openai`, `boto3` and the identity SDKs are optional extras and
are not imported by anything here.
"""

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
    "FinishReason",
    "LLMProvider",
    "Message",
    "MessageRole",
    "ToolCallRequest",
    "ToolSpecPayload",
    "Usage",
    "assistant_message",
    "system_message",
    "tool_result_message",
    "user_message",
]
