"""The tool layer: contracts, registry, flat schema export, dispatch.

`REGISTRY` is the process-wide registry, populated at import and then **frozen**.
Freezing matters because one registry is read by every concurrent session: a
registration at runtime would be shared mutable state across live calls, and the
kind that only misbehaves under concurrency.

Tests build their own `ToolRegistry()` instances, which are not frozen. That is the
only reason `ToolRegistry` is a class rather than module state.
"""

from rn_agent.tools.base import (
    INJECTED_CONTEXT_KEYS,
    Effect,
    ToolArgs,
    ToolEnvelope,
    ToolHandler,
    ToolOutcome,
    ToolReply,
    ToolRuntime,
    ToolServices,
    ToolSpec,
)
from rn_agent.tools.builtin import register_builtin_tools
from rn_agent.tools.dispatch import dispatch_tool_call
from rn_agent.tools.registry import ToolRegistry
from rn_agent.tools.schema import build_flat_tool_spec

#: The process-wide tool registry.
REGISTRY = ToolRegistry()
register_builtin_tools(REGISTRY)
REGISTRY.freeze()

__all__ = [
    "INJECTED_CONTEXT_KEYS",
    "REGISTRY",
    "Effect",
    "ToolArgs",
    "ToolEnvelope",
    "ToolHandler",
    "ToolOutcome",
    "ToolRegistry",
    "ToolReply",
    "ToolRuntime",
    "ToolServices",
    "ToolSpec",
    "build_flat_tool_spec",
    "dispatch_tool_call",
    "register_builtin_tools",
]
