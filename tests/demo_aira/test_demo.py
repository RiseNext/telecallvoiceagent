"""The retrieval gate: Aira answers from the reviewed corpus, offline, end to end.

**This is Phase 3 Stage 2 work, not Phase 4.** Retrieval and the 12 tools are Phase 3
deliverables (ROADMAP Phase 3, PRD D-8, DATA_MODEL §7); this slice was built ahead of
the rest of Stage 2 in a form that touches no schema — see ADR-012.

This is the test the milestone is defined by. It drives the **real** conversation loop,
the **real** process-wide registry and the **real** dispatcher over the **real** D-8
corpus — only the LLM and the embedding model are fakes, both at seams that exist
because neither a realtime provider nor an embedding model has been chosen.

What it does **not** assert, deliberately: that any particular passage ranks first for
any particular question. The embedder is a trigram hasher and such an assertion would be
a claim about retrieval quality, which is D-8's to make with a real model on a paid run
that has not been approved.
"""

from __future__ import annotations

import pytest

from rn_agent.guardrails.disclosure import DisclosureKind
from rn_agent.tools.base import ToolOutcome
from rn_core.ids import new_id
from rn_domain.identifiers import OrganizationId
from rn_domain.sanitisation import looks_price_shaped
from rn_services.retrieval import InMemoryKnowledgeRetriever, build_in_memory_index
from tests.demo_aira.corpus import KNOWLEDGE_BASE_NAMES, load_demo_corpus
from tests.demo_aira.pipeline import (
    DEMO_DIMENSIONS,
    build_demo_tenant,
    run_demo_conversation,
)
from tests.demo_aira.run import main

pytestmark = pytest.mark.agent_eval

QUESTION = "what services does the company offer?"


# ---------------------------------------------------------------------------
# The corpus, as loaded
# ---------------------------------------------------------------------------


def test_the_demo_loads_the_reviewed_corpus_without_touching_it() -> None:
    corpus = load_demo_corpus(organization_id=OrganizationId(new_id()))

    assert corpus.passage_count > 0
    assert len(corpus.documents) == corpus.passage_count
    assert set(KNOWLEDGE_BASE_NAMES) <= set(corpus.knowledge_base_ids)
    # Every document is filed under a knowledge base that has an id, and every one
    # belongs to the tenant it was loaded for.
    assert all(document.organization_id == corpus.organization_id for document in corpus.documents)
    assert {document.knowledge_base_id for document in corpus.documents} <= set(
        corpus.knowledge_base_ids.values()
    )


async def test_the_adversarial_passages_are_quarantined_not_indexed() -> None:
    """The corpus deliberately contains instruction-shaped passages. They are withheld
    before they are embedded, so retrieval cannot serve one however it is queried."""
    tenant = await build_demo_tenant()

    assert tenant.index.report.quarantined > 0
    assert len(tenant.index.quarantined_chunk_ids) == tenant.index.report.quarantined
    indexed = {chunk.chunk_id for chunk in tenant.index.chunks}
    assert indexed.isdisjoint(tenant.index.quarantined_chunk_ids)
    assert all(
        "ignore all previous instructions" not in chunk.content.casefold()
        for chunk in tenant.index.chunks
    )


async def test_the_index_records_what_produced_it() -> None:
    tenant = await build_demo_tenant()

    assert tenant.index.chunking_policy_version == "chunking-v1"
    assert tenant.index.dimensions == DEMO_DIMENSIONS
    # The fake's model id could not be mistaken for a real model in a stored row or a
    # log line, which is why it reads the way it does. D-8 has selected nothing.
    assert "fake" in tenant.index.embedding_model


# ---------------------------------------------------------------------------
# The conversation
# ---------------------------------------------------------------------------


async def test_aira_answers_a_question_through_the_real_pipeline() -> None:
    run = await run_demo_conversation(QUESTION)
    result = run.result

    assert result.tool_names_called == ("search_knowledge",)
    assert result.successful_tool_calls == 1
    assert result.stop_reason.value == "completed"
    # Disclosure is a gate, not a score: a version that fails it cannot be published.
    assert result.disclosure.kind is not DisclosureKind.NONE
    assert result.opt_out is None


async def test_the_retrieved_content_is_real_corpus_text() -> None:
    """The assistant's turns are scripted; the tool result is not. This asserts the
    part that is real — that what came back is text from the tenant's own corpus."""
    run = await run_demo_conversation(QUESTION)
    envelope = run.result.tool_executions[0].envelope

    assert envelope.outcome is ToolOutcome.OK
    assert envelope.data is not None
    results = envelope.data["results"]
    assert results, "the demo question must retrieve something or it demonstrates nothing"

    corpus_text = "\n".join(document.text for document in run.tenant.corpus.documents)
    for item in results:
        assert item["content"] in corpus_text
        assert item["source"] in KNOWLEDGE_BASE_NAMES


async def test_the_agent_states_no_price() -> None:
    """Pricing is authoritative data from `get_service_pricing`, which does not exist
    yet. Nothing retrieved may be spoken as a price (PRD §6.5)."""
    run = await run_demo_conversation(QUESTION)
    assert not any(looks_price_shaped(turn) for turn in run.result.assistant_turns)


async def test_the_demo_offers_only_the_tool_it_wired_a_service_for() -> None:
    tenant = await build_demo_tenant()
    assert tenant.snapshot.enabled_tools == frozenset({"search_knowledge"})
    assert tenant.runtime.services.retrieval is not None
    assert tenant.runtime.services.knowledge is None


# ---------------------------------------------------------------------------
# Tenant isolation, end to end
# ---------------------------------------------------------------------------


async def test_a_second_tenant_sharing_one_index_sees_none_of_the_first_s_corpus() -> None:
    """Both tenants' documents in one index, under knowledge bases with the same names.
    A retriever that scoped by name, or did not scope at all, would pass every other
    test in this file and fail this one."""
    org_a = OrganizationId(new_id())
    org_b = OrganizationId(new_id())
    corpus_a = load_demo_corpus(organization_id=org_a)
    corpus_b = load_demo_corpus(
        organization_id=org_b, knowledge_base_ids=corpus_a.knowledge_base_ids
    )

    from rn_providers.fakes import FakeEmbeddingProvider

    provider = FakeEmbeddingProvider(dimensions=DEMO_DIMENSIONS)
    shared = await build_in_memory_index(
        documents=[*corpus_a.documents, *corpus_b.documents], provider=provider
    )
    tenant_a = await build_demo_tenant(organization_id=org_a, index=shared, corpus=corpus_a)

    result = await tenant_a.retriever.search(query=QUESTION, k=5)
    assert result.chunks, "tenant A must still retrieve its own content"

    a_ids = {document.document_id for document in corpus_a.documents}
    assert all(chunk.chunk_id.split("#")[0] in a_ids for chunk in result.chunks)

    # And the shared index really did hold both tenants, or the assertion above is
    # trivially true.
    organizations = {chunk.organization_id for chunk in shared.chunks}
    assert organizations == {org_a, org_b}


async def test_a_tenant_with_an_empty_index_retrieves_nothing_rather_than_erroring() -> None:
    provider_org = OrganizationId(new_id())
    corpus = load_demo_corpus(organization_id=provider_org)

    from rn_providers.fakes import FakeEmbeddingProvider

    provider = FakeEmbeddingProvider(dimensions=DEMO_DIMENSIONS)
    index = await build_in_memory_index(documents=corpus.documents, provider=provider)
    stranger = InMemoryKnowledgeRetriever(
        context=_context(OrganizationId(new_id())), index=index, provider=provider
    )
    result = await stranger.search(query=QUESTION, k=3)
    assert result.chunks == ()
    assert result.underfilled is True


def _context(organization_id: OrganizationId):  # type: ignore[no-untyped-def]
    from rn_domain.tenancy import TenantContext

    return TenantContext(
        organization_id=organization_id, permissions=frozenset({"org:knowledge:read"})
    )


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_the_cli_runs_end_to_end_and_says_what_it_is(capsys: pytest.CaptureFixture[str]) -> None:
    """The documented command, exercised. A demo whose entry point is only ever run by
    hand is a demo that is broken about a third of the time."""
    assert main([QUESTION]) == 0

    printed = capsys.readouterr().out
    assert "chunks indexed" in printed
    assert "quarantined" in printed
    assert "search_knowledge -> ok" in printed
    # The honesty note is part of the output, not an afterthought in a doc nobody opens.
    assert "measures no model" in printed
    assert "D-8 is open" in printed
