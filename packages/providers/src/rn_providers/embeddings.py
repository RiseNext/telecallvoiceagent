"""The embedding seam.

Text in, vectors out. This is the seam that open decision **D-8** exists to keep
honest: the production model, its output width, the Postgres column type and the
index strategy are **not chosen yet** (ADR-010), and nothing in this module
presumes any of them.

Three shape decisions, each with a reason that is not obvious:

**`embed_documents` and `embed_query` are separate methods.** Several candidate
models are *asymmetric*: they expect a role prefix (`"passage: "` / `"query: "`,
or an instruction preamble) and score measurably worse without it. Folding both
roles into one method would make remembering the prefix the caller's job, and a
caller that forgets it produces slightly-worse retrieval with no error — the same
failure shape as HC-25. Two methods make the role part of the type.

**Batching is the adapter's problem, not the caller's.** `embed_documents`
accepts any number of texts. Per-request input counts and token ceilings are
vendor limits (OpenAI documents 8192 tokens per input, 300,000 tokens summed
across a request), so they belong inside the adapter, which splits and
recombines. A seam that exposed `max_batch_size` would push a vendor's
arithmetic into the ingestion pipeline and would have to change every time a
vendor moved it.

**The returned width is asserted, never assumed.** `EmbeddingBatch` carries the
`model_id` and `dimensions` that actually produced it, and an adapter must refuse
a response whose vectors are not that length. Once a width lands in a Postgres
column type, a silently-wrong width is a corrupt row in a typed column and a
migration to fix it — so the cheapest possible check happens at the boundary.

**Nothing here imports a vendor SDK.** `openai` is an optional extra imported by
one adapter module, and an import-linter contract keeps vendor SDKs out of every
package except this one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from rn_core.errors import ProviderError

__all__ = [
    "EmbeddingBatch",
    "EmbeddingProvider",
    "EmbeddingUsage",
    "EmbeddingVector",
    "TextRole",
]

#: One embedding. A tuple rather than a list because a vector that has been
#: computed is a fact about a piece of text: nothing downstream should be able to
#: append to it, and an accidental in-place edit of a cached query vector would be
#: invisible.
type EmbeddingVector = tuple[float, ...]


class TextRole(StrEnum):
    """Whether a text is being embedded to be *stored* or to *search*.

    Exists because asymmetric models need to know, and because an adapter for a
    symmetric model can ignore it entirely. Passing the role explicitly is what
    lets one adapter implementation serve both kinds without a flag on the
    constructor that callers have to get right.
    """

    DOCUMENT = "document"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class EmbeddingUsage:
    """Token accounting for one logical embedding operation.

    Mirrors what the provider reports rather than what we wish it reported:
    embeddings have no output tokens, so there is no `output_tokens` field to
    populate with a zero that would later look like a measurement.

    When an adapter splits a call across several requests, these are the **sums**
    across those requests, because the caller asked for one operation and is
    billed for one operation. An adapter that cannot report tokens leaves both
    fields `None` rather than guessing.
    """

    prompt_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """The result of embedding one or more texts, in input order.

    `model_id` and `dimensions` are carried on the result, not just available on
    the provider, because they are written to `document_chunks.embedding_model`
    and `.embedding_dim` for **every row** (ADR-010). Reading them off the batch
    that produced the vectors means the recorded provenance cannot drift from the
    vectors it describes — which is what makes a later per-tenant re-embed
    tractable at all.
    """

    vectors: tuple[EmbeddingVector, ...]
    model_id: str
    dimensions: int
    usage: EmbeddingUsage = EmbeddingUsage()

    def __post_init__(self) -> None:
        if self.dimensions < 1:
            raise ProviderError(
                "An embedding batch reported a non-positive dimension.",
                detail={"model_id": self.model_id, "dimensions": self.dimensions},
            )
        wrong = [
            (index, len(vector))
            for index, vector in enumerate(self.vectors)
            if len(vector) != self.dimensions
        ]
        if wrong:
            # A width mismatch is not a caller error to be handled — it means the
            # provider returned something other than what it was asked for, and
            # storing it would put a wrong-length vector in a typmod'd column.
            raise ProviderError(
                "An embedding provider returned vectors of an unexpected width.",
                detail={
                    "model_id": self.model_id,
                    "expected_dimensions": self.dimensions,
                    "mismatches": wrong[:5],
                },
            )

    def __len__(self) -> int:
        return len(self.vectors)

    @property
    def only(self) -> EmbeddingVector:
        """The single vector, for a one-text call.

        Raises `ProviderError` when the batch does not hold exactly one vector.
        Callers that embed a query want one vector and reaching for `[0]` on an
        empty batch would be an `IndexError` three frames from the cause.
        """
        if len(self.vectors) != 1:
            raise ProviderError(
                "Expected exactly one embedding.",
                detail={"model_id": self.model_id, "returned": len(self.vectors)},
            )
        return self.vectors[0]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """A text-embedding provider.

    Deliberately narrow: two methods and two properties. There is no streaming
    method, no similarity method and no index method, because embedding is the
    only thing this seam is for — similarity is Postgres' job and lives behind
    the single retrieval helper in `rn_persistence`.

    Implementations must raise `rn_core.ProviderError` (or `RateLimitError` /
    `TransientError`) for provider failures. A vendor exception escaping this
    seam would force the ingestion pipeline to know which vendor it is talking
    to, which is the thing the seam exists to prevent.
    """

    @property
    def model_id(self) -> str:
        """The model actually used. Recorded on every chunk it embeds."""
        ...

    @property
    def dimensions(self) -> int:
        """The width of the vectors this provider returns.

        A property rather than a per-call argument: a provider instance produces
        one width for its whole lifetime, and that width is what a Postgres column
        is typed against. Making it per-call would make "which width is in this
        column" a runtime question.
        """
        ...

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed texts that will be **stored and searched against**.

        Args:
            texts: Any number of texts. The adapter splits them across as many
                provider requests as its own limits require and returns one batch
                in input order. An empty sequence returns an empty batch without
                calling the provider.
        """
        ...

    async def embed_query(self, text: str) -> EmbeddingBatch:
        """Embed one text that will be **used to search**.

        Separate from `embed_documents` so an asymmetric model gets its query
        prefix. Returns a batch rather than a bare vector so the caller can read
        `usage` — a query embedding is on the tool-call path and its cost and
        token count are worth metering from the first day.
        """
        ...
