"""Built-in tool declarations.

Phase 2 ships two READ-only tools over knowledge-base metadata. The V1 tool set —
retrieval, pricing, leads, availability, booking, messaging, callbacks — arrives in
Phases 3, 9 and 10 with the machinery each part of it needs.
"""

from rn_agent.tools.builtin.knowledge import (
    FindKnowledgeBaseArgs,
    ListKnowledgeBasesArgs,
    register_builtin_tools,
)

__all__ = [
    "FindKnowledgeBaseArgs",
    "ListKnowledgeBasesArgs",
    "register_builtin_tools",
]
