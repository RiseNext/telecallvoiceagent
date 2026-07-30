"""RiseNext agent core — definitions, versioning, instructions, tools, guardrails.

**Framework-free by contract.** No LangChain, no LangGraph, no LangSmith, no vendor
SDK, no SQLAlchemy, no HTTP framework, no broker client, no Redis. Tool schemas come
from plain Pydantic; the LangChain adapter lives in `rn_orchestration` and walks this
registry from above. Enforced by `uv run lint-imports`, not by discipline — see
ADR-004 and AGENT_ARCHITECTURE §3.1.

That is not stylistic. `langchain-core` hard-depends on `langsmith`, so a tool
registry that imported it would put a tracing SaaS client into every process that
loads an agent — including, later, the one holding a live call's audio.

**Phase 2 scope.** Agent snapshot and versioning, deterministic instruction
composition, the typed tool registry with flat provider schema export, tool
authorization and dispatch, two READ-only demo tools, disclosure and opt-out
guardrails, and a text-mode conversation loop. No audio, no telephony, no realtime
provider, no retrieval.
"""

from rn_agent.conversation import (
    ConversationResult,
    StopReason,
    ToolExecutionRecord,
    run_text_conversation,
)
from rn_agent.errors import (
    AgentConfigurationError,
    SnapshotResolutionError,
    ToolBlocked,
    ToolRegistrationError,
)
from rn_agent.guardrails import (
    DisclosureFinding,
    DisclosureKind,
    OptOutFinding,
    OptOutLanguage,
    detect_disclosure,
    detect_opt_out,
)
from rn_agent.instructions import (
    PLATFORM_INSTRUCTIONS,
    CallInstructionContext,
    compose_instruction_prefix,
    render_call_instructions,
    untrusted_block,
)
from rn_agent.resolve import resolve_published_snapshot, snapshot_from_configuration
from rn_agent.snapshot import (
    AgentSnapshot,
    GuardrailConfig,
    TurnPolicy,
    VoiceRef,
    build_snapshot,
)
from rn_agent.tools import (
    REGISTRY,
    Effect,
    ToolArgs,
    ToolEnvelope,
    ToolOutcome,
    ToolRegistry,
    ToolReply,
    ToolRuntime,
    ToolServices,
    ToolSpec,
    dispatch_tool_call,
)

__version__ = "0.1.0"

__all__ = [
    "PLATFORM_INSTRUCTIONS",
    "REGISTRY",
    "AgentConfigurationError",
    "AgentSnapshot",
    "CallInstructionContext",
    "ConversationResult",
    "DisclosureFinding",
    "DisclosureKind",
    "Effect",
    "GuardrailConfig",
    "OptOutFinding",
    "OptOutLanguage",
    "SnapshotResolutionError",
    "StopReason",
    "ToolArgs",
    "ToolBlocked",
    "ToolEnvelope",
    "ToolExecutionRecord",
    "ToolOutcome",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolReply",
    "ToolRuntime",
    "ToolServices",
    "ToolSpec",
    "TurnPolicy",
    "VoiceRef",
    "build_snapshot",
    "compose_instruction_prefix",
    "detect_disclosure",
    "detect_opt_out",
    "dispatch_tool_call",
    "render_call_instructions",
    "resolve_published_snapshot",
    "run_text_conversation",
    "snapshot_from_configuration",
    "untrusted_block",
]
