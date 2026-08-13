"""Compose the demo: index, retriever, agent version, runtime, scripted conversation.

This is the composition root the demo has instead of an application — `apps/` has no
entrypoint yet, so the wiring that an API process will eventually do lives here, in one
readable function per thing that gets constructed.

**The conversation runs through the real pipeline.** `run_text_conversation`, the real
`ToolRegistry`, the real `dispatch_tool_call`, a real `ToolRuntime` carrying a real
`TenantContext`. Only two things are fakes, both at seams that exist for exactly this:
the LLM (scripted, because no realtime provider is selected yet) and the embedding model
(deterministic, because D-8 has chosen none). There is no parallel agent loop here and
there must never be one — a demo that reimplements the loop demonstrates the
reimplementation.

**What the demo proves, and what it does not.**

It proves the machinery: that a document is chunked by the frozen policy, that
instruction-shaped content is withheld before it is ever embedded, that a query is
embedded in the query role, that ranking is tenant-scoped, that a tool call reaches a
service through the dispatcher with server-injected context, and that the retrieved text
comes back to the model as data.

It proves **nothing about retrieval quality**. `FakeEmbeddingProvider` is a character-
trigram hasher: it has no semantic understanding and no cross-script capability at all,
so an English query against English content ranks sensibly and a Hindi or Telugu query
against the same content scores near zero. That is the fake behaving exactly as
documented, not a bug and not a finding about any model. D-8 remains open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from rn_agent.conversation import ConversationResult, run_text_conversation
from rn_agent.snapshot import AgentSnapshot, build_snapshot
from rn_agent.tools import REGISTRY
from rn_agent.tools.base import ToolRuntime, ToolServices
from rn_core.clock import now_utc
from rn_core.ids import new_id
from rn_domain.entities.agents import AgentVersion
from rn_domain.enums import AgentVersionStatus
from rn_domain.identifiers import AgentId, AgentVersionId, CallId, OrganizationId, UserId
from rn_domain.tenancy import TenantContext
from rn_domain.values import LanguagePolicy, LanguageTag
from rn_providers.fakes import FakeEmbeddingProvider
from rn_providers.fakes.llm import FakeLLMProvider, ScriptedToolCall, ScriptedTurn
from rn_services.retrieval import (
    InMemoryKnowledgeIndex,
    InMemoryKnowledgeRetriever,
    build_in_memory_index,
)
from tests.demo_aira.corpus import DemoCorpus, load_demo_corpus

__all__ = [
    "DEMO_DIMENSIONS",
    "DemoRun",
    "DemoTenant",
    "build_demo_tenant",
    "run_demo_conversation",
]

#: The fake embedder's width. **Stated at every call site, never defaulted.**
#:
#: `FakeEmbeddingProvider` requires it for a reason worth repeating: a default width is
#: how a number becomes the one every fixture is written against, and then a de facto
#: answer to D-8 — which is a Postgres column type and a full re-embed to change. 256 is
#: a demo's arithmetic budget and is evidence about nothing.
DEMO_DIMENSIONS: Final[int] = 256

#: The agent brief. Aira is **a tenant's configuration**, which is why this string lives
#: in a test package and not in any platform module.
DEMO_INSTRUCTIONS: Final[str] = """\
You are Aira, a sales assistant for a business services company in India.
Greet the caller and disclose that you are an AI in your first turn.
When a caller asks what the business does or how something works, call search_knowledge
and answer only from what it returns. Never state a price. Keep turns short.
"""


@dataclass(frozen=True, slots=True)
class DemoTenant:
    """One wired tenant: corpus, index, retriever, agent version, tool runtime."""

    corpus: DemoCorpus
    index: InMemoryKnowledgeIndex
    retriever: InMemoryKnowledgeRetriever
    snapshot: AgentSnapshot
    runtime: ToolRuntime


@dataclass(frozen=True, slots=True)
class DemoRun:
    """One scripted conversation, plus the tenant it ran against."""

    tenant: DemoTenant
    result: ConversationResult


async def build_demo_tenant(
    *,
    organization_id: OrganizationId | None = None,
    index: InMemoryKnowledgeIndex | None = None,
    corpus: DemoCorpus | None = None,
) -> DemoTenant:
    """Build a tenant with the Rise Next corpus indexed and Aira configured.

    Args:
        organization_id: The tenant. Minted when absent.
        index: Reuse an index instead of building one — how the isolation test gives two
            tenants one shared index, which is the only arrangement in which a missing
            tenant filter would actually be observable.
        corpus: Reuse an already-loaded corpus.
    """
    org = organization_id or OrganizationId(new_id())
    loaded = corpus or load_demo_corpus(organization_id=org)
    provider = FakeEmbeddingProvider(dimensions=DEMO_DIMENSIONS)
    built = index or await build_in_memory_index(documents=loaded.documents, provider=provider)

    retriever = InMemoryKnowledgeRetriever(
        context=_tenant_context(org),
        index=built,
        provider=provider,
    )
    version = AgentVersion(
        id=AgentVersionId(new_id()),
        organization_id=org,
        agent_id=AgentId(new_id()),
        version_number=1,
        instructions=DEMO_INSTRUCTIONS,
        language_policy=LanguagePolicy(
            primary=LanguageTag("en"),
            allowed=(LanguageTag("en"), LanguageTag("hi-IN"), LanguageTag("te-IN")),
        ),
        status=AgentVersionStatus.PUBLISHED,
        published_at=now_utc(),
    )
    snapshot = build_snapshot(
        version=version,
        # Only the retrieval tool. The metadata tools would need a `KnowledgeCatalog`
        # this demo has no database to build, and offering a tool whose service is not
        # wired is how an `InvariantViolation` reaches a caller mid-turn.
        enabled_tool_names=["search_knowledge"],
        knowledge_base_ids=tuple(loaded.knowledge_base_ids.values()),
        registry=REGISTRY,
    )
    runtime = ToolRuntime(
        context=_tenant_context(org),
        agent_version_id=version.id,
        call_id=CallId(new_id()),
        services=ToolServices(retrieval=retriever),
    )
    return DemoTenant(
        corpus=loaded, index=built, retriever=retriever, snapshot=snapshot, runtime=runtime
    )


def _tenant_context(organization_id: OrganizationId) -> TenantContext:
    """A server-derived tenant context, with only what the demo's one tool needs.

    `org:knowledge:read` and nothing else. A demo context carrying every permission
    would make the dispatcher's authorization check unobservable — it would pass for
    every tool, including one that should have been refused.
    """
    return TenantContext(
        organization_id=organization_id,
        actor_id=UserId(new_id()),
        permissions=frozenset({"org:knowledge:read"}),
    )


async def run_demo_conversation(question: str, *, tenant: DemoTenant | None = None) -> DemoRun:
    """Ask Aira one question, through the real conversation loop.

    The tape is two turns, which is what one caller utterance costs when the agent uses
    a tool: the model asks for `search_knowledge`, the loop dispatches it and feeds the
    envelope back, and the model then speaks.

    **The assistant's words are scripted and are not a model's output.** They are
    deliberately free of any business fact — the facts are in the tool envelope, which
    is real. A scripted turn that recited a retrieved detail would look like an answer
    the platform produced, and this demo would then be claiming something it did not
    measure.
    """
    wired = tenant or await build_demo_tenant()
    provider = FakeLLMProvider(
        [
            ScriptedTurn(
                # Discloses in the first turn, because the disclosure guardrail reads
                # the first assistant text and a demo that trips a compliance gate is
                # demonstrating the wrong thing.
                content=(
                    "Hi, I'm Aira — I'm an AI assistant from Rise Next. "
                    "Let me look that up for you."
                ),
                tool_calls=(
                    ScriptedToolCall(name="search_knowledge", arguments={"query": question}),
                ),
                # Asserts the loop actually offered the tool and passed the caller's
                # words through. Without these the tape produces the same output even if
                # the loop stopped wiring either, and the test still passes.
                expect_last_user_contains=question[:20],
                expect_tool_offered="search_knowledge",
            ),
            ScriptedTurn(
                content=(
                    "Here's what our information says — would you like me to go into "
                    "any of that in more detail?"
                ),
                # Matched against the tool message's *content*, which is the serialised
                # envelope: outcome, message, data. The tool's name is deliberately not
                # in it, so this asserts the thing that matters anyway — the model does
                # not get to speak until a successful tool result has been fed back.
                expect_tool_result_for='"outcome": "ok"',
            ),
        ]
    )
    result = await run_text_conversation(
        provider=provider,
        snapshot=wired.snapshot,
        runtime=wired.runtime,
        registry=REGISTRY,
        caller_utterances=[question],
    )
    return DemoRun(tenant=wired, result=result)
