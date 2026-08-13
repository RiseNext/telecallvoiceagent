"""Built-in tool declarations.

Phase 2 shipped two READ-only tools over knowledge-base **metadata**. A first slice of Phase 3 Stage 2
adds `search_knowledge`, the first tool that returns knowledge **content** — over the
`KnowledgeRetriever` seam, which has an in-memory implementation today and a
SQL-backed one once D-8 is closed (ADR-010). The rest of the V1 tool set — pricing,
leads, availability, booking, messaging, callbacks — arrives in Phases 3, 9 and 10 with
the machinery each part of it needs.

`register_builtin_tools` registers **all** of them, which is what the process-wide
registry wants. The per-group functions stay exported so a test can build a registry
holding exactly the tools it wires services for.
"""

from rn_agent.tools.builtin.knowledge import (
    FindKnowledgeBaseArgs,
    ListKnowledgeBasesArgs,
    register_knowledge_tools,
)
from rn_agent.tools.builtin.search import SearchKnowledgeArgs, register_search_tools
from rn_agent.tools.registry import ToolRegistry

__all__ = [
    "FindKnowledgeBaseArgs",
    "ListKnowledgeBasesArgs",
    "SearchKnowledgeArgs",
    "register_builtin_tools",
    "register_knowledge_tools",
    "register_search_tools",
]


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Declare every built-in tool on a registry.

    A function rather than module-level decorators so that a test can build an isolated
    registry with exactly these tools. The process-wide registry calls it once, at
    import.
    """
    register_knowledge_tools(registry)
    register_search_tools(registry)
