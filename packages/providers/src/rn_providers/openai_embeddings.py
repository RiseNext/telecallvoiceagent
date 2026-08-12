"""The OpenAI embeddings adapter.

**Verified against primary documentation on 2026-07-30**, not from memory
(CLAUDE.md rule 6). The wire facts this file is written against:

* `POST /embeddings`; body fields `input`, `model` (both required), `dimensions`,
  `encoding_format` (`"float" | "base64"`), `user` (all optional).
* `dimensions` is **"Only supported in `text-embedding-3` and later models"** — so
  sending it for `text-embedding-ada-002` is an error, and this adapter refuses to.
* Response: `{"data": [{"embedding": [...], "index": n, "object": "embedding"}],
  "model": ..., "object": "list", "usage": {"prompt_tokens": n, "total_tokens": n}}`.
* Limits: **8192 tokens per input**, **300,000 tokens summed across one request**,
  array inputs capped at **2048** elements.
* `text-embedding-3-small` → 1536 native; `text-embedding-3-large` → 3072 native;
  both accept `dimensions`. Shortening is documented as supported, and manual
  shortening requires re-normalising — which is why this adapter asks the **API**
  for a reduced width instead of truncating client-side.

**Why `httpx` and not the `openai` SDK.** A reversible library choice, noted
rather than escalated. Two concrete reasons: `respx` is in the dev group
specifically so "provider adapters must be testable offline", and the `openai`
extra is **not installed in the default dev environment** — an SDK-based adapter
could not be exercised by the default `uv run pytest` at all, which is the only
test run most changes get. The request shape here is four fields; an SDK buys
nothing that offsets an untestable adapter.

**No internal retries.** Retry policy belongs to the caller that knows how much
budget is left: an ingestion job can retry for a minute, and a `search_knowledge`
tool call inside a live turn cannot. The dispatcher is single-shot for the same
reason. This adapter classifies failures and returns; it does not decide.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

import httpx

from rn_core.errors import ConfigurationError, ProviderError, RateLimitError, TransientError
from rn_core.logging import get_logger
from rn_providers.embeddings import (
    EmbeddingBatch,
    EmbeddingUsage,
    EmbeddingVector,
    TextRole,
)

__all__ = [
    "DIMENSION_CAPABLE_MODEL_PREFIX",
    "MAX_CHARS_PER_INPUT",
    "MAX_CHARS_PER_REQUEST",
    "MAX_INPUTS_PER_REQUEST",
    "NATIVE_DIMENSIONS",
    "OpenAIEmbeddingProvider",
]

_logger = get_logger(__name__)

#: Native output widths, from the embeddings guide (verified 2026-07-30). Used only
#: to validate a requested reduction — never to pick a width. D-8 picks the width.
NATIVE_DIMENSIONS: Final[Mapping[str, int]] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}

#: The `dimensions` parameter is documented as supported on `text-embedding-3` and
#: later only. Matched on the prefix rather than an allowlist so a future
#: `text-embedding-3-*` model works without an edit, while `ada-002` still does not.
DIMENSION_CAPABLE_MODEL_PREFIX: Final[str] = "text-embedding-3"

#: Inputs per request. The documented cap is 2048; this is deliberately lower so a
#: single failed request costs less to retry and one oversized chunk cannot push a
#: whole batch over the token ceiling.
MAX_INPUTS_PER_REQUEST: Final[int] = 256

#: Character ceilings, used as a **safety bound** rather than a measurement.
#:
#: We do not ship a tokeniser: `tiktoken` would be a dependency added to avoid a
#: 400 that a conservative bound already avoids, and CLAUDE.md rule 10 asks for a
#: reason a dependency exists. The bound has to be conservative in the right
#: direction, and the direction is not the obvious one — Devanagari and Telugu
#: routinely cost **more than one token per character** under a
#: byte-pair-encoding tokeniser trained mostly on Latin script, so "4 characters
#: per token" is exactly the assumption that breaks on the languages this platform
#: exists for. These bounds assume the pessimistic case of roughly one token per
#: character and still sit an order of magnitude inside the documented 300,000.
MAX_CHARS_PER_REQUEST: Final[int] = 100_000
#: Per input, against the documented 8192-token-per-input limit.
MAX_CHARS_PER_INPUT: Final[int] = 8_000

_DEFAULT_BASE_URL: Final[str] = "https://api.openai.com/v1"

#: Applied per role. Empty for both, because OpenAI's embedding models are
#: symmetric — the guide documents no query/passage prefix convention. The mapping
#: exists so that adding an asymmetric provider later is a new adapter rather than a
#: change to the seam, and so this adapter's role handling is explicit rather than
#: an omission a reader has to infer.
_ROLE_PREFIX: Final[Mapping[TextRole, str]] = {TextRole.DOCUMENT: "", TextRole.QUERY: ""}


class OpenAIEmbeddingProvider:
    """`EmbeddingProvider` over OpenAI's embeddings endpoint.

    Args:
        api_key: Never logged, never placed on a span, never in an error `detail`.
        model: The model id, sent verbatim.
        dimensions: The width to request. `None` means "the model's native width",
            which the caller must then know — so `self.dimensions` resolves it from
            `NATIVE_DIMENSIONS` and refuses a model whose native width we have not
            verified. There is no fallback guess: a wrong width becomes a Postgres
            column type.
        client: An injected `httpx.AsyncClient`. Injected rather than owned so the
            caller controls connection reuse — an ingestion job embedding thousands
            of chunks must not open a connection per batch.
    """

    __slots__ = ("_client", "_dimensions", "_model", "_owns_client", "_timeout_seconds", "_token")

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ConfigurationError(
                "An OpenAI embedding provider was constructed without an API key."
            )
        resolved = _resolve_dimensions(model=model, requested=dimensions)
        self._token = api_key
        self._model = model
        self._dimensions = resolved
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(timeout_seconds)
        )

    # -- protocol ----------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        return await self._embed(texts, role=TextRole.DOCUMENT)

    async def embed_query(self, text: str) -> EmbeddingBatch:
        return await self._embed([text], role=TextRole.QUERY)

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close the client, but only if this adapter created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- internals ---------------------------------------------------------

    async def _embed(self, texts: Sequence[str], *, role: TextRole) -> EmbeddingBatch:
        if not texts:
            # No provider call at all. An empty ingestion batch is an ordinary
            # thing (a document that chunked to nothing after normalisation), and
            # a request with an empty `input` array is a 400.
            return EmbeddingBatch(
                vectors=(),
                model_id=self._model,
                dimensions=self._dimensions,
                usage=EmbeddingUsage(),
            )

        prefix = _ROLE_PREFIX[role]
        prepared = [prefix + text for text in texts]
        for index, text in enumerate(prepared):
            if len(text) > MAX_CHARS_PER_INPUT:
                # Refuse rather than truncate. Truncating would embed something
                # other than the text we store in `document_chunks.content`, so the
                # vector would describe a document that does not exist — a silent
                # retrieval-quality bug that no test could see. The chunker's job
                # is to keep inputs well inside this.
                raise ProviderError(
                    "A text is too long to embed in one input; chunk it first.",
                    detail={
                        "index": index,
                        "characters": len(text),
                        "max_characters": MAX_CHARS_PER_INPUT,
                    },
                )

        vectors: list[EmbeddingVector] = []
        prompt_tokens = 0
        total_tokens = 0
        reported = False

        for window in _windows(prepared):
            payload, usage = await self._post(window)
            vectors.extend(payload)
            if usage.prompt_tokens is not None:
                prompt_tokens += usage.prompt_tokens
                reported = True
            if usage.total_tokens is not None:
                total_tokens += usage.total_tokens
                reported = True

        if len(vectors) != len(prepared):
            raise ProviderError(
                "The embedding provider returned a different number of vectors than requested.",
                detail={"requested": len(prepared), "returned": len(vectors)},
            )

        return EmbeddingBatch(
            vectors=tuple(vectors),
            model_id=self._model,
            dimensions=self._dimensions,
            usage=(
                EmbeddingUsage(prompt_tokens=prompt_tokens, total_tokens=total_tokens)
                if reported
                else EmbeddingUsage()
            ),
        )

    async def _post(self, window: Sequence[str]) -> tuple[list[EmbeddingVector], EmbeddingUsage]:
        body: dict[str, Any] = {
            "input": list(window),
            "model": self._model,
            # Sent explicitly rather than relying on the documented default, so a
            # change to that default cannot silently hand us base64.
            "encoding_format": "float",
        }
        if self._supports_dimensions:
            body["dimensions"] = self._dimensions

        try:
            response = await self._client.post(
                "/embeddings",
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.TimeoutException as exc:
            raise TransientError(
                "The embedding provider did not respond in time.",
                detail={"model": self._model, "timeout_seconds": self._timeout_seconds},
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientError(
                "The embedding provider could not be reached.",
                detail={"model": self._model, "error_type": type(exc).__name__},
            ) from exc

        self._raise_for_status(response)
        return self._parse(response)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return

        # The body can echo the request, and the request is tenant content. Only a
        # bounded slice goes into `detail`, which is logged and redacted and never
        # serialised to a client (and never to a model — the tool dispatcher maps
        # this to a fixed, caller-safe envelope message).
        excerpt = response.text[:200]
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitError(
                "The embedding provider is rate limiting us.",
                detail={"status": response.status_code, "body_excerpt": excerpt},
            )
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise TransientError(
                "The embedding provider returned a server error.",
                detail={"status": response.status_code, "body_excerpt": excerpt},
            )
        _logger.warning(
            "provider.embeddings.rejected",
            status=response.status_code,
            model=self._model,
        )
        raise ProviderError(
            "The embedding provider rejected the request.",
            detail={"status": response.status_code, "body_excerpt": excerpt},
        )

    def _parse(self, response: httpx.Response) -> tuple[list[EmbeddingVector], EmbeddingUsage]:
        try:
            document = response.json()
        except ValueError as exc:
            raise ProviderError(
                "The embedding provider returned a body that is not JSON.",
                detail={"status": response.status_code},
            ) from exc
        if not isinstance(document, Mapping):
            raise ProviderError("The embedding response was not a JSON object.")

        rows = document.get("data")
        if not isinstance(rows, list):
            raise ProviderError("The embedding response carried no `data` array.")

        # Ordered by the response's own `index` field, never by array position.
        # The API documents an index precisely because position is not the
        # contract, and a reordered batch would attach every vector to the wrong
        # chunk — which stores fine, retrieves plausibly, and is close to
        # undebuggable from the symptom.
        indexed: list[tuple[int, EmbeddingVector]] = []
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ProviderError("An embedding row was not an object.")
            raw = row.get("embedding")
            if not isinstance(raw, list):
                raise ProviderError(
                    "An embedding row carried no vector.", detail={"position": position}
                )
            order = row.get("index")
            indexed.append(
                (
                    order if isinstance(order, int) else position,
                    tuple(_as_float(value) for value in raw),
                )
            )
        indexed.sort(key=lambda item: item[0])

        usage_raw = document.get("usage")
        usage = EmbeddingUsage()
        if isinstance(usage_raw, Mapping):
            usage = EmbeddingUsage(
                prompt_tokens=_as_optional_int(usage_raw.get("prompt_tokens")),
                total_tokens=_as_optional_int(usage_raw.get("total_tokens")),
            )
        return [vector for _, vector in indexed], usage

    @property
    def _supports_dimensions(self) -> bool:
        return self._model.startswith(DIMENSION_CAPABLE_MODEL_PREFIX)


def _resolve_dimensions(*, model: str, requested: int | None) -> int:
    """Decide the width this provider will report, refusing anything unverifiable."""
    native = NATIVE_DIMENSIONS.get(model)
    if requested is None:
        if native is None:
            raise ConfigurationError(
                "This embedding model's native width is not recorded, so it must be "
                "given explicitly. A guessed width becomes a Postgres column type.",
                detail={"model": model, "known_models": sorted(NATIVE_DIMENSIONS)},
            )
        return native
    if requested < 1:
        raise ConfigurationError(
            "An embedding width must be positive.", detail={"model": model, "dimensions": requested}
        )
    # Refuse only what can be *proven* wrong. A model outside the `text-embedding-3`
    # family cannot be given a reduced width — but that is only checkable when the
    # native width is recorded. When it is not, the caller's explicit value is trusted
    # here and verified where it actually matters: `EmbeddingBatch` refuses a response
    # whose vectors are not the claimed width, which is the authoritative check.
    #
    # Refusing on `native != requested` with `native is None` made every model with an
    # unrecorded width unconstructible in both directions — no width was refused for
    # being absent, and any width was refused for disagreeing with `None`.
    if (
        native is not None
        and not model.startswith(DIMENSION_CAPABLE_MODEL_PREFIX)
        and requested != native
    ):
        raise ConfigurationError(
            "This model does not support the `dimensions` parameter, so only its "
            "native width can be requested.",
            detail={"model": model, "requested": requested, "native": native},
        )
    if native is not None and requested > native:
        raise ConfigurationError(
            "An embedding width cannot exceed the model's native width.",
            detail={"model": model, "requested": requested, "native": native},
        )
    return requested


def _windows(texts: Sequence[str]) -> Iterator[Sequence[str]]:
    """Split into requests that respect both the count and character ceilings.

    A single input longer than `MAX_CHARS_PER_REQUEST` cannot occur — the caller
    already refused anything over `MAX_CHARS_PER_INPUT`, which is smaller — so the
    loop cannot produce an empty window and cannot spin.
    """
    window: list[str] = []
    characters = 0
    for text in texts:
        exceeds_count = len(window) >= MAX_INPUTS_PER_REQUEST
        exceeds_chars = window and characters + len(text) > MAX_CHARS_PER_REQUEST
        if exceeds_count or exceeds_chars:
            yield window
            window = []
            characters = 0
        window.append(text)
        characters += len(text)
    if window:
        yield window


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProviderError("An embedding vector contained a non-numeric value.")
    result = float(value)
    if not math.isfinite(result):
        # A NaN or infinity would propagate into a distance calculation and make
        # every comparison against that row false, silently removing it from
        # results rather than erroring.
        raise ProviderError("An embedding vector contained a non-finite value.")
    return result


def _as_optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
