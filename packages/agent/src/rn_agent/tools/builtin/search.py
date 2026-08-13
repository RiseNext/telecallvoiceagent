"""`search_knowledge` — the first content-retrieval tool.

The agent's answer to *"what do you do?"*, and deliberately **not** its answer to
*"what does it cost?"*. Retrieval and authority are separate tools on purpose
(AGENT_ARCHITECTURE §3.4, PRD §6.5): knowledge is fuzzy and quotable, pricing is exact
and authoritative, and a price that comes out of a vector index is a commitment the
business never made. `get_service_pricing` is a later phase and this tool is not a
stand-in for it.

Three shape decisions worth stating.

**Retrieved text is data, never instruction.** It comes back inside `ToolReply.data`,
which the dispatcher wraps in an envelope and the loop serialises as a tool result —
the same channel every other tool's output uses. Nothing here can promote a retrieved
chunk into the instruction position, and instruction-shaped chunks never reach this
tool at all: they are withheld when the index is built, not filtered when it is read.

**An empty result is `OK`, not `NOT_FOUND`.** Unlike `find_knowledge_base`, which is
given an exact name and can say the name is wrong, a fuzzy query matching nothing is an
ordinary outcome the agent should speak to ("I don't have anything on that — shall I
have someone call you back?"). Raising here would make a routine conversational moment
look like a failure in the evaluation record.

**The result carries content and a source label, nothing else.** No score, no chunk id,
no knowledge-base id. The model has no use for any of them, and an identifier in a
model's context is one more thing that can be read out loud on a phone call.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from rn_agent.tools.base import Effect, ToolArgs, ToolReply, ToolRuntime
from rn_agent.tools.registry import ToolRegistry

__all__ = ["MAX_RESULTS", "SearchKnowledgeArgs", "register_search_tools"]

#: Upper bound on results per call, and a **conversation-design** limit rather than a
#: retrieval one. The agent reads these out; three short passages is already a long turn
#: on a phone call, and a model given eight will try to summarise all eight.
#:
#: It is a literal because the JSON schema is built once at import and a bound has to be
#: in it. `RetrievalSettings.max_k` (16) is the operational ceiling and sits above this,
#: so the service clamp can only ever narrow what this tool asks for, never widen it.
MAX_RESULTS = 5
DEFAULT_RESULTS = 3

#: Two characters, because a caller genuinely does ask about "AI". The upper bound is a
#: sentence, not an essay: a model pasting a whole conversation in as a query retrieves
#: worse than one asking a question, and the cost is paid on an embedding call.
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 400


class SearchKnowledgeArgs(ToolArgs):
    """Arguments for `search_knowledge`."""

    query: str = Field(
        min_length=MIN_QUERY_CHARS,
        max_length=MAX_QUERY_CHARS,
        description=(
            "What the caller wants to know, in your own words — a short question or "
            "topic, not the whole conversation."
        ),
    )
    limit: int = Field(
        default=DEFAULT_RESULTS,
        ge=1,
        le=MAX_RESULTS,
        description=(
            "How many passages to retrieve. Keep it small — you will be reading these "
            "out loud on a phone call."
        ),
    )


def register_search_tools(registry: ToolRegistry) -> None:
    """Declare the retrieval tools on a registry.

    Separate from `register_builtin_tools` so a test can build a registry with only the
    metadata tools, or only this one, and so the wiring each needs stays legible: these
    tools require `ToolServices.retrieval`, the metadata tools require
    `ToolServices.knowledge`, and a caller that wires the wrong one gets an
    `InvariantViolation` naming which handle is missing.
    """

    @registry.tool(
        name="search_knowledge",
        description=(
            "Search this business's own information to answer a caller's question about "
            "what it does, how it works, or its policies. Returns passages to quote from. "
            "Never use it for prices — those come from the pricing tool."
        ),
        args=SearchKnowledgeArgs,
        effect=Effect.READ,
        # Already in the frozen catalog (migration 0001), so this tool needs no
        # migration. `list_knowledge_bases` reads the same permission: listing topics
        # and reading their content are the same authority over the same data.
        permission="org:knowledge:read",
        # Longer than the metadata tools' 3s because this one embeds the query through a
        # provider before it ranks anything, and a network round trip sits inside it once
        # the provider is real. Still well inside what a live turn can absorb.
        timeout=timedelta(seconds=5),
    )
    async def search_knowledge(args: SearchKnowledgeArgs, rt: ToolRuntime) -> ToolReply:
        retriever = rt.services.require_retrieval()
        # No organization argument exists to pass: the retriever was constructed with a
        # server-derived TenantContext. Knowledge-base scoping is not applied here —
        # binding retrieval to the agent version's knowledge bases needs those bindings
        # on the runtime, which is a later change; until then the tenant's whole indexed
        # corpus is in scope, which is narrower than it sounds because the index is
        # built per tenant.
        result = await retriever.search(query=args.query, k=args.limit)

        if not result.chunks:
            return ToolReply(
                data={"results": [], "count": 0},
                message="I don't have anything on that in our information.",
            )
        return ToolReply(
            data={
                "results": [
                    {"content": chunk.content, "source": chunk.knowledge_base_name}
                    for chunk in result.chunks
                ],
                "count": len(result.chunks),
            },
            message="Here is what our own information says — quote from it, do not add to it.",
        )
