"""The OpenAI embeddings adapter, against a mocked transport. **No network, no cost.**

Marked `provider` per TESTING §2: "adapter behaviour against a fake or mocked
transport — `respx` for HTTP adapters. Never a paid API." The dev group carries `respx`
for exactly this, which is also why the adapter is built on `httpx` rather than the
`openai` SDK — the SDK is an optional extra that is not installed in the default dev
environment, so an SDK-based adapter could not be exercised by the default test run at
all.

Every request/response shape asserted here comes from documentation read on
2026-07-30, not from memory.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from rn_core.errors import ConfigurationError, ProviderError, RateLimitError, TransientError
from rn_providers.embeddings import EmbeddingProvider
from rn_providers.openai_embeddings import (
    MAX_CHARS_PER_INPUT,
    MAX_INPUTS_PER_REQUEST,
    NATIVE_DIMENSIONS,
    OpenAIEmbeddingProvider,
)

pytestmark = [pytest.mark.provider]

BASE = "https://api.openai.com/v1"
KEY = "sk-test-not-a-real-key"


def _body(width: int, count: int, *, shuffle: bool = False) -> dict[str, Any]:
    """A response in the documented shape.

    Each vector's first component encodes its index, so a test can prove the adapter
    ordered by the response's `index` field rather than by array position.
    """
    rows = [
        {
            "object": "embedding",
            "index": position,
            "embedding": [float(position)] + [0.0] * (width - 1),
        }
        for position in range(count)
    ]
    if shuffle:
        rows.reverse()
    return {
        "object": "list",
        "data": rows,
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 11 * count, "total_tokens": 11 * count},
    }


def _provider(**overrides: Any) -> OpenAIEmbeddingProvider:
    kwargs: dict[str, Any] = {
        "api_key": KEY,
        "model": "text-embedding-3-small",
        "dimensions": 8,
        "base_url": BASE,
    }
    kwargs.update(overrides)
    return OpenAIEmbeddingProvider(**kwargs)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_the_adapter_satisfies_the_protocol_structurally() -> None:
    assert isinstance(_provider(), EmbeddingProvider)


def test_an_empty_api_key_is_refused_at_construction() -> None:
    with pytest.raises(ConfigurationError):
        _provider(api_key="   ")


def test_a_model_with_no_recorded_native_width_needs_an_explicit_one() -> None:
    """No guessing. A guessed width becomes a Postgres column type."""
    with pytest.raises(ConfigurationError, match="native width is not recorded"):
        _provider(model="text-embedding-future", dimensions=None)


def test_the_native_width_is_used_when_none_is_requested() -> None:
    assert _provider(model="text-embedding-3-small", dimensions=None).dimensions == 1536
    assert _provider(model="text-embedding-3-large", dimensions=None).dimensions == 3072
    assert NATIVE_DIMENSIONS["text-embedding-3-large"] == 3072


def test_a_width_above_the_model_native_width_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="cannot exceed"):
        _provider(model="text-embedding-3-small", dimensions=3072)


def test_a_non_reducible_model_with_a_known_width_cannot_be_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented: `dimensions` is "Only supported in `text-embedding-3` and later
    models", so a reduced width on an older model is an error rather than a no-op.

    Driven through a monkeypatched width table rather than through `ada-002`, because
    the embeddings guide **does not state ada-002's native width** and this repository
    refuses to record a provider fact it has not read. Patching supplies the one
    condition the guard needs — a known native width on a non-reducible model — without
    asserting anything untrue about a real model.
    """
    monkeypatch.setitem(NATIVE_DIMENSIONS, "text-embedding-legacy-1", 1024)
    with pytest.raises(ConfigurationError, match="does not support"):
        _provider(model="text-embedding-legacy-1", dimensions=512)


def test_a_non_reducible_model_with_an_unknown_width_trusts_the_explicit_value() -> None:
    """Only provable errors are refused at construction.

    When the native width is not recorded there is nothing to compare against, so the
    caller's explicit value stands and the real check happens on the response —
    `EmbeddingBatch` refuses vectors that are not the claimed width. An earlier version
    compared against `None` here and made every unrecorded model unconstructible in
    both directions at once.
    """
    provider = _provider(model="text-embedding-ada-002", dimensions=1536)
    assert provider.dimensions == 1536


# ---------------------------------------------------------------------------
# Happy path and wire shape
# ---------------------------------------------------------------------------


@respx.mock
async def test_the_request_carries_the_documented_fields() -> None:
    route = respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(200, json=_body(8, 2))
    )
    provider = _provider()
    batch = await provider.embed_documents(["one", "two"])

    assert len(batch) == 2
    assert batch.dimensions == 8
    assert batch.model_id == "text-embedding-3-small"

    sent = json.loads(route.calls.last.request.content)
    assert sent["input"] == ["one", "two"]
    assert sent["model"] == "text-embedding-3-small"
    assert sent["dimensions"] == 8
    # Sent explicitly rather than relying on the documented default, so a change to
    # that default cannot silently hand us base64.
    assert sent["encoding_format"] == "float"
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    await provider.aclose()


@respx.mock
async def test_dimensions_is_omitted_for_a_model_that_does_not_support_it() -> None:
    route = respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(200, json=_body(1536, 1))
    )
    # ada-002 has no recorded native width, so it must be given one explicitly — that
    # refusal is tested above. Here it is constructed *with* a width equal to what it
    # natively returns, to prove the request omits the unsupported field rather than
    # sending it and getting a 400.
    provider = OpenAIEmbeddingProvider(
        api_key=KEY, model="text-embedding-ada-002", dimensions=1536, base_url=BASE
    )
    await provider.embed_documents(["x"])
    assert "dimensions" not in json.loads(route.calls.last.request.content)
    await provider.aclose()


@respx.mock
async def test_vectors_are_ordered_by_the_response_index_not_array_position() -> None:
    """The API documents an `index` because position is not the contract.

    A reordered batch attaches every vector to the wrong chunk. It stores fine,
    retrieves plausibly, and is close to undebuggable from the symptom — so the adapter
    sorts, and this test reverses the array to prove it.
    """
    respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(200, json=_body(8, 3, shuffle=True))
    )
    provider = _provider()
    batch = await provider.embed_documents(["a", "b", "c"])
    assert [vector[0] for vector in batch.vectors] == [0.0, 1.0, 2.0]
    await provider.aclose()


@respx.mock
async def test_an_empty_input_makes_no_request_at_all() -> None:
    route = respx.post(f"{BASE}/embeddings")
    provider = _provider()
    batch = await provider.embed_documents([])
    assert len(batch) == 0
    assert not route.called
    await provider.aclose()


@respx.mock
async def test_a_query_is_embedded_as_a_single_input() -> None:
    route = respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(200, json=_body(8, 1))
    )
    provider = _provider()
    batch = await provider.embed_query("how long does a website take")
    assert len(batch.only) == 8
    assert json.loads(route.calls.last.request.content)["input"] == ["how long does a website take"]
    await provider.aclose()


@respx.mock
async def test_usage_is_summed_across_split_requests() -> None:
    """A caller asked for one operation and is billed for one operation."""
    counts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        counts.append(len(inputs))
        return httpx.Response(200, json=_body(8, len(inputs)))

    respx.post(f"{BASE}/embeddings").mock(side_effect=handler)
    provider = _provider()
    texts = [f"text number {index}" for index in range(MAX_INPUTS_PER_REQUEST + 5)]
    batch = await provider.embed_documents(texts)

    assert len(batch) == len(texts)
    assert len(counts) == 2, "the batch should have been split across two requests"
    assert counts == [MAX_INPUTS_PER_REQUEST, 5]
    assert batch.usage.prompt_tokens == 11 * len(texts)
    await provider.aclose()


# ---------------------------------------------------------------------------
# Refusals and error mapping
# ---------------------------------------------------------------------------


@respx.mock
async def test_an_over_long_input_is_refused_rather_than_truncated() -> None:
    """Truncating would embed something other than the text stored in
    `document_chunks.content`, so the vector would describe a document that does not
    exist — a silent retrieval-quality bug no test could see."""
    route = respx.post(f"{BASE}/embeddings")
    provider = _provider()
    with pytest.raises(ProviderError, match="too long"):
        await provider.embed_documents(["x" * (MAX_CHARS_PER_INPUT + 1)])
    assert not route.called
    await provider.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, RateLimitError),
        (500, TransientError),
        (503, TransientError),
        (400, ProviderError),
        (401, ProviderError),
    ],
)
async def test_http_failures_map_onto_the_error_taxonomy(
    status: int, expected: type[Exception]
) -> None:
    respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(status, json={"error": {"message": "nope"}})
    )
    provider = _provider()
    with pytest.raises(expected):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
async def test_a_timeout_becomes_a_transient_error() -> None:
    respx.post(f"{BASE}/embeddings").mock(side_effect=httpx.ReadTimeout("slow"))
    provider = _provider()
    with pytest.raises(TransientError, match="did not respond"):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
async def test_a_connection_failure_becomes_a_transient_error() -> None:
    respx.post(f"{BASE}/embeddings").mock(side_effect=httpx.ConnectError("down"))
    provider = _provider()
    with pytest.raises(TransientError, match="could not be reached"):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {"object": "list"},
        {"data": "not a list"},
        {"data": [{"index": 0}]},
        {"data": [{"index": 0, "embedding": "not a list"}]},
    ],
)
async def test_a_malformed_response_becomes_a_provider_error(payload: dict[str, Any]) -> None:
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json=payload))
    provider = _provider()
    with pytest.raises(ProviderError):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
async def test_a_non_json_body_becomes_a_provider_error() -> None:
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, text="<html>"))
    provider = _provider()
    with pytest.raises(ProviderError, match="not JSON"):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
async def test_a_non_finite_value_is_refused() -> None:
    """A NaN would propagate into a distance calculation and make every comparison
    against that row false — silently removing it from results rather than erroring."""
    respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(
                {"data": [{"index": 0, "embedding": [1.0, float("nan")]}], "usage": {}}
            ).replace("NaN", "NaN"),
            headers={"content-type": "application/json"},
        )
    )
    provider = _provider(dimensions=2)
    with pytest.raises(ProviderError, match="non-finite"):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
async def test_a_wrong_width_from_the_provider_is_refused() -> None:
    """The batch's own invariant, reached through the adapter: a provider that returns
    a different width than requested must not produce a batch claiming otherwise."""
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json=_body(16, 1)))
    provider = _provider(dimensions=8)
    with pytest.raises(ProviderError, match="unexpected width"):
        await provider.embed_documents(["x"])
    await provider.aclose()


@respx.mock
async def test_a_short_count_from_the_provider_is_refused() -> None:
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json=_body(8, 1)))
    provider = _provider()
    with pytest.raises(ProviderError, match="different number of vectors"):
        await provider.embed_documents(["a", "b"])
    await provider.aclose()


@respx.mock
async def test_the_api_key_never_appears_in_an_error_detail() -> None:
    """`detail` is logged. A key in a log line is a rotation event."""
    respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(400, text="bad request echoing nothing sensitive")
    )
    provider = _provider()
    with pytest.raises(ProviderError) as caught:
        await provider.embed_documents(["x"])
    rendered = f"{caught.value.message} {caught.value.detail}"
    assert KEY not in rendered
    await provider.aclose()


async def test_an_injected_client_is_not_closed_by_the_adapter() -> None:
    """An ingestion job embedding thousands of chunks owns its connection pool; an
    adapter closing a client it did not create would break the next batch."""
    async with httpx.AsyncClient(base_url=BASE) as client:
        provider = _provider(client=client)
        await provider.aclose()
        assert not client.is_closed
