"""Retrieval orchestration, over an in-memory index. Offline. No SQL.

[DATA_MODEL §7](../../../../docs/DATA_MODEL.md) splits retrieval across two layers:
*orchestration* here in `rn_services` — embed the query, resolve which knowledge bases
are in scope, decide `k`, shape the result, report under-return — and one
distance-operator-issuing *implementation* function in `rn_persistence`. (The operator
itself is deliberately not spelled here: Stage 2 adds a structural test that greps the
tracked source for it and asserts one match, and a mention in prose would be a false
positive that costs someone an afternoon.) This module is the first
half. **The second half is deliberately still unwritten**, because the physical schema
it would query is open decision **D-8** (ADR-010): there is no `document_chunks` table,
no vector column and no pgvector index, and none of them may be created before
ADR-011 records a measured answer.

So the index here is a tuple of chunks in process memory, ranked by cosine in Python.
Three things that makes true, which the split above is what buys us:

* **The seam the agent layer depends on is the real one.** A tool reaches
  `KnowledgeRetriever`, which is what it will reach in Stage 2. Swapping this
  implementation for the SQL-backed one changes no caller.
* **Nothing here pre-empts D-8.** No width is chosen, no column type is implied, no
  index strategy is assumed. The embedding provider is injected and its model and width
  are read off the batch that produced the vectors.
* **It runs with no database, no network and no cost**, which is what makes an
  end-to-end retrieval demo possible before any of the storage decisions are made.

**What it is not.** It is not the production retrieval path, it holds a whole tenant's
corpus in memory, and it re-embeds the corpus on every process start. It is correct and
it does not scale, in that order. Nothing here should be extended to make it scale —
the answer to "this is slow" is Stage 2, not a cache.

**Exact search, so HC-25 cannot occur here.** The tenant and knowledge-base predicates
are applied *before* ranking rather than after an approximate index scan, so a scoped
query cannot silently under-return the way a filtered ANN query does. `underfilled` is
still reported, because it can legitimately be true — a tenant with four chunks cannot
answer a request for eight — and because the signal must exist from the first day so
that Stage 2 inherits a caller that already reads it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from rn_core.errors import InvariantViolation
from rn_core.logging import get_logger
from rn_core.settings import RetrievalSettings
from rn_domain.chunking import FROZEN_CHUNKING_V1, ChunkingPolicy, chunk_document
from rn_domain.identifiers import KnowledgeBaseId, OrganizationId
from rn_domain.sanitisation import inspect_content
from rn_domain.tenancy import TenantContext
from rn_domain.text import normalise_text
from rn_providers.embeddings import EmbeddingProvider, EmbeddingVector
from rn_services.contracts import RetrievalResult, RetrievedChunk

__all__ = [
    "InMemoryKnowledgeIndex",
    "InMemoryKnowledgeRetriever",
    "IndexBuildReport",
    "IndexedChunk",
    "KnowledgeDocument",
    "build_in_memory_index",
]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """One document to index, with the tenant and knowledge base that own it.

    `organization_id` is on the **document**, not on the index, so that one index can
    hold several tenants' content and the retriever's tenant filter has something real
    to filter. That is not a hypothetical convenience: an index that can only ever hold
    one tenant makes the isolation test vacuous — it would pass against a retriever
    with no filter at all.
    """

    organization_id: OrganizationId
    knowledge_base_id: KnowledgeBaseId
    knowledge_base_name: str
    #: Stable within a build. Chunk ids are derived from it, so two documents sharing
    #: one would produce colliding chunk ids and a silently smaller index.
    document_id: str
    text: str


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """One chunk, embedded and ready to rank."""

    chunk_id: str
    organization_id: OrganizationId
    knowledge_base_id: KnowledgeBaseId
    knowledge_base_name: str
    content: str
    vector: EmbeddingVector
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexBuildReport:
    """What one index build did, in numbers a human can check.

    `quarantined` is the number that matters. A build that quarantines everything and a
    build that quarantines nothing both produce a working index and a working demo; the
    difference is only visible if the count is reported.
    """

    documents: int
    chunks_indexed: int
    #: Instruction-shaped chunks withheld from the index entirely (SECURITY §5.4).
    quarantined: int
    #: Chunks kept but flagged as carrying a money-shaped number. Kept because a case
    #: study that quotes a price is legitimate content; flagged because a price a caller
    #: is quoted must come from the service catalogue, never from retrieval (PRD §6.5).
    price_flagged: int
    #: Documents whose text normalised to nothing. A legitimate outcome, not an error.
    empty_documents: int
    embedding_model: str
    dimensions: int
    chunking_policy_version: str


@dataclass(frozen=True, slots=True)
class InMemoryKnowledgeIndex:
    """An embedded corpus held in process memory."""

    chunks: tuple[IndexedChunk, ...]
    embedding_model: str
    dimensions: int
    chunking_policy_version: str
    report: IndexBuildReport
    #: Ids of chunks withheld for review. Ids only — the *text* of a quarantined chunk
    #: is exactly the text a human reviews and an attacker wrote, and it has no business
    #: being carried around by anything that also talks to a model.
    quarantined_chunk_ids: tuple[str, ...] = field(default_factory=tuple)


async def build_in_memory_index(
    *,
    documents: Sequence[KnowledgeDocument],
    provider: EmbeddingProvider,
    policy: ChunkingPolicy = FROZEN_CHUNKING_V1,
) -> InMemoryKnowledgeIndex:
    """Chunk, inspect, quarantine, embed. Deterministic for a deterministic provider.

    The pipeline is the one ingestion will run in Stage 2, minus the writes:

        document -> normalise + chunk (frozen policy) -> inspect
                 -> withhold instruction-shaped -> embed as DOCUMENT -> index

    **Inspection happens before embedding, not after.** A quarantined chunk is never
    embedded at all, so there is no vector for it anywhere and no way for a later
    filter bug to serve one. Filtering at query time instead would leave the chunk one
    forgotten `WHERE` clause away from a caller.

    Every chunk is embedded with `TextRole.DOCUMENT` via a single `embed_documents`
    call. Batching is the adapter's problem by design, so this passes the whole corpus
    and lets the provider split it.

    Raises:
        InvariantViolation: if the provider returns a different number of vectors than
            it was given texts. That would attach vectors to the wrong chunks, and every
            ranking afterwards would be meaningless while still looking plausible.
    """
    kept: list[tuple[KnowledgeDocument, str, str, tuple[str, ...]]] = []
    quarantined: list[str] = []
    price_flagged = 0
    empty_documents = 0

    for document in documents:
        chunks = chunk_document(document.text, policy=policy)
        if not chunks:
            empty_documents += 1
            continue
        for chunk in chunks:
            chunk_id = f"{document.document_id}#{chunk.index}"
            finding = inspect_content(chunk.text)
            if finding.instruction_shaped:
                # Withheld, counted, and its text goes no further. The excerpts on the
                # finding are tenant content for a human reviewer; they are not logged.
                quarantined.append(chunk_id)
                continue
            flags = tuple(sorted(flag.value for flag in finding.flags))
            if finding.price_shaped:
                price_flagged += 1
            kept.append((document, chunk_id, chunk.text, flags))

    batch = await provider.embed_documents([text for _, _, text, _ in kept])
    if len(batch) != len(kept):
        raise InvariantViolation(
            "The embedding provider returned a different number of vectors than chunks.",
            detail={"chunks": len(kept), "vectors": len(batch)},
        )

    indexed = tuple(
        IndexedChunk(
            chunk_id=chunk_id,
            organization_id=document.organization_id,
            knowledge_base_id=document.knowledge_base_id,
            knowledge_base_name=document.knowledge_base_name,
            content=text,
            vector=batch.vectors[position],
            flags=flags,
        )
        for position, (document, chunk_id, text, flags) in enumerate(kept)
    )

    report = IndexBuildReport(
        documents=len(documents),
        chunks_indexed=len(indexed),
        quarantined=len(quarantined),
        price_flagged=price_flagged,
        empty_documents=empty_documents,
        embedding_model=batch.model_id,
        dimensions=batch.dimensions,
        chunking_policy_version=policy.version,
    )
    _logger.info(
        "knowledge.index.built",
        # Counts only. No tenant id, no content, no chunk id: this line exists to make a
        # build's shape visible, and none of those make it more visible.
        documents=report.documents,
        chunks_indexed=report.chunks_indexed,
        quarantined=report.quarantined,
        price_flagged=report.price_flagged,
        embedding_model=report.embedding_model,
        dimensions=report.dimensions,
        chunking_policy=report.chunking_policy_version,
    )
    return InMemoryKnowledgeIndex(
        chunks=indexed,
        embedding_model=batch.model_id,
        dimensions=batch.dimensions,
        chunking_policy_version=policy.version,
        report=report,
        quarantined_chunk_ids=tuple(quarantined),
    )


class InMemoryKnowledgeRetriever:
    """`KnowledgeRetriever` over an `InMemoryKnowledgeIndex`, scoped to one tenant.

    Satisfies the protocol structurally. Constructed with a server-derived
    `TenantContext`; there is no organization parameter on `search`, so a caller has
    nothing to pass wrongly and a model has nothing to influence.

    Args:
        context: The tenant this retriever may read. Chunks belonging to any other
            organization are invisible to it.
        index: The embedded corpus. May legitimately hold several tenants' chunks.
        provider: Embeds the query, with `TextRole.QUERY`. Must be the same model that
            built the index — checked at construction, because comparing a query vector
            from one model against document vectors from another produces a confident
            ranking of noise rather than an error.
        settings: Supplies the `k` ceiling. Defaults to the documented defaults rather
            than reading the environment: a retriever that silently depends on process
            configuration is one whose behaviour differs between a test and a demo.
    """

    __slots__ = ("_context", "_index", "_provider", "_settings")

    def __init__(
        self,
        *,
        context: TenantContext,
        index: InMemoryKnowledgeIndex,
        provider: EmbeddingProvider,
        settings: RetrievalSettings | None = None,
    ) -> None:
        if provider.model_id != index.embedding_model:
            raise InvariantViolation(
                "The query provider is not the model that built this index.",
                detail={"index_model": index.embedding_model, "provider_model": provider.model_id},
            )
        if provider.dimensions != index.dimensions:
            raise InvariantViolation(
                "The query provider's width does not match this index.",
                detail={
                    "index_dimensions": index.dimensions,
                    "provider_dimensions": provider.dimensions,
                },
            )
        self._context = context
        self._index = index
        self._provider = provider
        self._settings = settings or RetrievalSettings(_env_file=None)

    async def search(
        self,
        *,
        query: str,
        knowledge_base_ids: Sequence[KnowledgeBaseId] | None = None,
        k: int,
    ) -> RetrievalResult:
        """Rank this tenant's chunks against `query` and return the best `k`.

        `k` is clamped to `[1, RetrievalSettings.max_k]` rather than refused. A caller
        asking for more than the ceiling has made a configuration mistake, not a request
        that should fail mid-conversation, and the clamped value is reported back as
        `requested_k` so the result stays self-describing.

        A query that normalises to nothing returns an empty result without embedding
        anything: the empty string embeds to a meaningless vector that would then rank
        somewhere arbitrary and be quoted to a caller as an answer.
        """
        effective_k = min(max(k, 1), self._settings.max_k)
        normalised = normalise_text(query)
        if not normalised:
            return RetrievalResult(
                chunks=(), requested_k=effective_k, embedding_model=self._index.embedding_model
            )

        scope = frozenset(knowledge_base_ids) if knowledge_base_ids is not None else None
        candidates = [
            chunk
            for chunk in self._index.chunks
            # The tenant predicate is first and unconditional. Everything else is a
            # narrowing of what this tenant may already see.
            if chunk.organization_id == self._context.organization_id
            and (scope is None or chunk.knowledge_base_id in scope)
        ]
        if not candidates:
            return self._result((), effective_k, query_embedded=False)

        vector = (await self._provider.embed_query(normalised)).only
        scored = [(_cosine_similarity(vector, chunk.vector), chunk) for chunk in candidates]
        # Ties break on chunk id so a run is reproducible: two chunks with identical
        # scores must not swap places between runs, or a passing test becomes a flake.
        scored.sort(key=lambda item: (-item[0], item[1].chunk_id))

        chunks = tuple(
            RetrievedChunk(
                chunk_id=chunk.chunk_id,
                knowledge_base_id=chunk.knowledge_base_id,
                knowledge_base_name=chunk.knowledge_base_name,
                content=chunk.content,
                score=score,
                embedding_model=self._index.embedding_model,
                flags=chunk.flags,
            )
            for score, chunk in scored[:effective_k]
        )
        return self._result(chunks, effective_k, query_embedded=True)

    def _result(
        self, chunks: tuple[RetrievedChunk, ...], requested_k: int, *, query_embedded: bool
    ) -> RetrievalResult:
        result = RetrievalResult(
            chunks=chunks, requested_k=requested_k, embedding_model=self._index.embedding_model
        )
        if result.underfilled:
            # The only observable symptom of the filtered-scan trap, logged at warning
            # even though this implementation cannot suffer it — so the signal is
            # already wired when the SQL implementation, which can, replaces this one.
            _logger.warning(
                "knowledge.retrieval.underfilled",
                organization_id=str(self._context.organization_id),
                requested_k=requested_k,
                returned=len(chunks),
                query_embedded=query_embedded,
            )
        return result


def _cosine_similarity(left: EmbeddingVector, right: EmbeddingVector) -> float:
    """Cosine similarity. Higher is better.

    Computed in full rather than assuming unit vectors: the `EmbeddingProvider` seam
    does not promise normalisation, providers differ, and a reduced-width embedding
    needs re-normalising after truncation. Assuming a unit norm would mis-rank exactly
    the reduced-width case D-8 exists to evaluate.

    `math.fsum` rather than `sum` so the result does not depend on summation order.
    """
    if len(left) != len(right):
        raise InvariantViolation(
            "Cannot compare embeddings of different widths.",
            detail={"left": len(left), "right": len(right)},
        )
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return math.fsum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
