"""The two built-in Phase-2 tools.

**Both are READ-only, and both read metadata that Phase 1 already stores.** They
exist to satisfy the Phase-2 gate — a scripted conversation making at least two tool
calls, in CI, with no network egress — and to exercise the parts of the pipeline
that only a real tool can: typed argument validation, `NOT_FOUND`, tenant scoping,
and a service handle reached through a protocol.

**They are not the V1 tool set.** `get_service_pricing`, `book_meeting`,
`send_whatsapp` and the rest are Phase 3, 9 and 10 (ROADMAP), and they need
idempotency keys, rate limits and compliance gates that do not exist yet. Content
retrieval now lives next door in `search.py`, over the `KnowledgeRetriever` seam;
**nothing in this module retrieves document content**, and there is still no
`documents` table and no vector column, because that is open decision **D-8**
(ADR-010).

Between them they cover a genuine conversational need — an agent that cannot say
what it is able to help with is a bad agent — while staying entirely inside what
Phase 1 built.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from rn_agent.tools.base import Effect, ToolArgs, ToolReply, ToolRuntime
from rn_agent.tools.registry import ToolRegistry

__all__ = ["FindKnowledgeBaseArgs", "ListKnowledgeBasesArgs", "register_knowledge_tools"]

#: Upper bound on how many topics one tool call returns.
#:
#: Small because the result is *spoken*: an agent reading out twelve topic names on
#: a phone call is a worse agent than one that names three and asks. The limit is a
#: conversation-design decision, not a database one.
MAX_TOPICS = 10
DEFAULT_TOPICS = 5


class ListKnowledgeBasesArgs(ToolArgs):
    """Arguments for `list_knowledge_bases`."""

    # `description` here is prompt surface, not documentation: it is what the model
    # reads when deciding what to pass (AGENT_ARCHITECTURE §3.3).
    limit: int = Field(
        default=DEFAULT_TOPICS,
        ge=1,
        le=MAX_TOPICS,
        description=(
            "How many topics to return. Keep it small — you will be saying these "
            "out loud on a phone call."
        ),
    )


class FindKnowledgeBaseArgs(ToolArgs):
    """Arguments for `find_knowledge_base`."""

    name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "The exact topic name, as returned by list_knowledge_bases. "
            "Never invent one; if you are unsure, call list_knowledge_bases first."
        ),
    )


def register_knowledge_tools(registry: ToolRegistry) -> None:
    """Declare the knowledge-**metadata** tools on a registry.

    A function rather than module-level decorators so that a test can build an
    isolated registry with exactly these tools. `register_builtin_tools` calls it
    alongside the retrieval tools; the process-wide registry calls that, once, at
    import.
    """

    @registry.tool(
        name="list_knowledge_bases",
        description=(
            "List the topics this business has information about, so you can tell "
            "the caller what you are able to help with. Returns topic names only, "
            "not their contents."
        ),
        args=ListKnowledgeBasesArgs,
        effect=Effect.READ,
        permission="org:knowledge:read",
        timeout=timedelta(seconds=3),
    )
    async def list_knowledge_bases(args: ListKnowledgeBasesArgs, rt: ToolRuntime) -> ToolReply:
        catalog = rt.services.require_knowledge()
        # Tenant scoping is not this function's concern and is not reachable from
        # here: the catalog was constructed with a server-derived TenantContext and
        # has no organization parameter to pass wrongly.
        summaries = await catalog.list_knowledge_bases(limit=args.limit)
        return ToolReply(
            data={
                "topics": [
                    {"name": summary.name, "description": summary.description}
                    for summary in summaries
                ],
                "count": len(summaries),
            },
            message=(
                "Found some topics I can help with."
                if summaries
                else "There are no topics configured for this business yet."
            ),
        )

    @registry.tool(
        name="find_knowledge_base",
        description=(
            "Check whether this business has information about one specific topic, "
            "by its exact name. Use it to confirm a topic exists before telling the "
            "caller you can help with it."
        ),
        args=FindKnowledgeBaseArgs,
        effect=Effect.READ,
        permission="org:knowledge:read",
        timeout=timedelta(seconds=3),
    )
    async def find_knowledge_base(args: FindKnowledgeBaseArgs, rt: ToolRuntime) -> ToolReply:
        catalog = rt.services.require_knowledge()
        # A miss raises `NotFoundError`, which the dispatcher turns into a
        # `NOT_FOUND` envelope. Deliberately not caught and turned into
        # `{"found": false}`: the model handles a typed outcome more reliably than
        # it handles a boolean buried in a payload, and the outcome is what gets
        # recorded for evaluation.
        summary = await catalog.find_knowledge_base(name=args.name)
        return ToolReply(
            data={
                "name": summary.name,
                "description": summary.description,
                # The id is deliberately absent. The model has no use for it, and a
                # tenant identifier in a model's context is one more thing that can
                # end up spoken out loud or echoed into a later tool call.
            },
            message=f"Yes, there is information about {summary.name}.",
        )
