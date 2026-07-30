"""The text-mode LLM seam.

This is **not** the realtime voice seam. `VoiceSession` — audio in, audio out,
barge-in, truncation — is a different interface arriving with the realtime phase
(AGENT_ARCHITECTURE §9). This one covers the text path: scripted evaluation, the
cascaded LLM leg, post-call reasoning. It exists in Phase 2 because a text
conversation loop needs *something* to talk to, and because a seam declared
before its first vendor adapter is a seam that fits more than one vendor.

**Phase 2 ships this protocol and a fake. There is no vendor adapter.** The
roadmap's Phase-2 gate is a scripted conversation running in CI *with no network
egress*, which a real adapter cannot satisfy. `openai` is not imported here, and
an import-linter contract keeps vendor SDKs out of every package except this one.

Three shape decisions worth knowing:

**Tool arguments cross this seam as raw text.** `ToolCallRequest.arguments_json`
is the provider's string, unparsed. Providers emit a JSON string and it is
routinely malformed; parsing it is a validation event with a defined outcome
(`INVALID_ARGUMENTS`), so it belongs at one boundary — `rn_agent`'s dispatcher —
rather than being attempted here and re-raised as something less specific.

**Tool specs cross as the flat shape.** `{"type": "function", "name", "description",
"parameters"}`, properties at the top level. That is our canonical internal shape
and it is byte-identical to OpenAI Realtime's, deliberately: the latency-critical
consumer should not pay a translation. An adapter for a nested Chat-Completions
API re-nests at its own boundary, which is where vendor shape belongs (HC-19 and
AGENT_ARCHITECTURE §3.2 explain why getting this backwards fails *silently*).

**Nothing here is a `dict` the caller can mutate mid-flight.** Messages are frozen
dataclasses. A provider adapter that needs vendor payloads builds them itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

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

#: One flat tool spec, as produced by `rn_agent.tools.schema`. Typed as a mapping
#: rather than a concrete model because its contents are a provider wire format,
#: not our domain — pinning it to a class would invite adding fields to it.
type ToolSpecPayload = Mapping[str, Any]


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """Why the provider stopped generating.

    `LENGTH` and `CONTENT_FILTER` are distinct from `STOP` because a conversation
    loop must not treat a truncated turn as a complete one — that is how an agent
    ends up half-saying a price.
    """

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A tool call the model asked for. **Entirely untrusted.**

    `name` may not exist. `arguments_json` may not be valid JSON, may omit
    required fields, and may contain keys that collide with server-injected
    context. None of that is checked here; all of it is checked once, in
    `rn_agent.tools.dispatch`.
    """

    #: Provider-assigned correlation id, echoed back with the result so the
    #: provider can match them. Opaque to us.
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation item.

    `content` is the transcript-bearing field and is therefore **never logged**
    and never placed on a span. `rn_core`'s span filter already drops attribute
    keys containing `content`; that is a backstop, not a licence.
    """

    role: MessageRole
    content: str | None = None
    #: Populated on an assistant message that requested tools.
    tool_calls: tuple[ToolCallRequest, ...] = ()
    #: Populated on a `TOOL` message, matching the request it answers.
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalised token accounting.

    `cached_input_tokens` is broken out because the cached/fresh spread is the
    dominant cost lever on this platform, and an adapter that folds it into
    `input_tokens` makes the saving invisible. Adapters that cannot report it
    leave it `None` rather than guessing zero.
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    """One provider response."""

    message: Message
    finish_reason: FinishReason
    usage: Usage | None = None
    #: Provider-side identifier for this generation, for support correlation.
    provider_response_id: str | None = None
    #: Arbitrary adapter diagnostics. Never shown to a model, never logged raw.
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """A text-mode completion provider.

    Deliberately narrow. There is no streaming method, no embedding method and no
    "chat session" object: Phase 2 needs one request/response call, and a seam
    that speculates about the next three consumers usually fits none of them.
    Streaming arrives with the consumer that needs it.

    Implementations must raise `rn_core.ProviderError` (or `RateLimitError` /
    `TransientError`) for provider failures. A vendor exception escaping this seam
    would force every caller to know which vendor it is talking to.
    """

    @property
    def model_id(self) -> str:
        """The model actually used. Recorded against evaluations and calls."""
        ...

    async def complete(
        self,
        *,
        instructions: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpecPayload] = (),
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Produce one assistant turn.

        Args:
            instructions: The composed system instruction. Byte-stable across a
                given agent version, which is what makes prompt caching work.
            messages: Conversation so far, oldest first.
            tools: Flat tool specs the model may call. An empty sequence means
                the model may not call anything — enforcement is structural
                (`rn_agent` refuses a tool absent from the snapshot regardless),
                but not offering it is the cheaper half.
            max_output_tokens: Upper bound on the generated turn.
        """
        ...


# ---------------------------------------------------------------------------
# Small constructors. Present because `Message(role=MessageRole.USER, ...)` at
# every call site reads worse than `user_message(...)`, and because a `TOOL`
# message without its `tool_call_id` is a mistake worth making hard to write.
# ---------------------------------------------------------------------------


def system_message(content: str) -> Message:
    return Message(role=MessageRole.SYSTEM, content=content)


def user_message(content: str) -> Message:
    return Message(role=MessageRole.USER, content=content)


def assistant_message(
    content: str | None = None, *, tool_calls: Sequence[ToolCallRequest] = ()
) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=content, tool_calls=tuple(tool_calls))


def tool_result_message(*, tool_call_id: str, content: str) -> Message:
    return Message(role=MessageRole.TOOL, content=content, tool_call_id=tool_call_id)
