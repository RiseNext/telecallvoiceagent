"""The embedding seam and its deterministic fake.

Two properties carry most of the weight here:

**Width is asserted, never assumed.** `EmbeddingBatch` refuses vectors that are not the
width it claims, because that width becomes a Postgres column type and a wrong-length
vector in a typmod'd column is a migration to fix.

**The fake is reproducible across processes.** `hash()` is salted per process for
`str`, so a fake built on it would return different vectors in two interpreters and
produce failures that look like flakes. The subprocess test is the only kind that can
actually catch that.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from rn_core.errors import InvariantViolation, ProviderError
from rn_providers.embeddings import EmbeddingBatch, EmbeddingProvider, EmbeddingUsage, TextRole
from rn_providers.fakes import FakeEmbeddingProvider

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# EmbeddingBatch
# ---------------------------------------------------------------------------


def test_a_batch_refuses_vectors_of_the_wrong_width() -> None:
    with pytest.raises(ProviderError, match="unexpected width"):
        EmbeddingBatch(vectors=((0.1, 0.2),), model_id="m", dimensions=3)


def test_a_batch_refuses_a_non_positive_width() -> None:
    with pytest.raises(ProviderError):
        EmbeddingBatch(vectors=(), model_id="m", dimensions=0)


def test_an_empty_batch_is_valid() -> None:
    """A document that chunks to nothing is a legitimate ingestion outcome."""
    batch = EmbeddingBatch(vectors=(), model_id="m", dimensions=8)
    assert len(batch) == 0


def test_only_returns_the_single_vector_and_refuses_otherwise() -> None:
    single = EmbeddingBatch(vectors=((1.0, 0.0),), model_id="m", dimensions=2)
    assert single.only == (1.0, 0.0)

    for bad in (
        EmbeddingBatch(vectors=(), model_id="m", dimensions=2),
        EmbeddingBatch(vectors=((1.0, 0.0), (0.0, 1.0)), model_id="m", dimensions=2),
    ):
        with pytest.raises(ProviderError, match="exactly one"):
            _ = bad.only


def test_usage_defaults_to_unknown_rather_than_zero() -> None:
    """`None`, not `0`. A zero would be a measurement nobody made, and it would sum
    into a cost total as though it were real."""
    usage = EmbeddingUsage()
    assert usage.prompt_tokens is None
    assert usage.total_tokens is None


# ---------------------------------------------------------------------------
# FakeEmbeddingProvider
# ---------------------------------------------------------------------------


def test_the_fake_satisfies_the_protocol_structurally() -> None:
    assert isinstance(FakeEmbeddingProvider(dimensions=16), EmbeddingProvider)


def test_the_fake_has_no_default_width() -> None:
    """`dimensions` is required, with no default.

    A default would become the number every fixture was written against, and D-8 has
    not chosen a width — a fake with a default width is how a vendor default becomes a
    de facto decision, which is the exact failure ADR-010 exists to prevent.
    """
    with pytest.raises(TypeError):
        FakeEmbeddingProvider()  # type: ignore[call-arg]


@pytest.mark.parametrize("dimensions", [0, 1, 9000, -4])
def test_the_fake_refuses_an_unusable_width(dimensions: int) -> None:
    with pytest.raises(InvariantViolation):
        FakeEmbeddingProvider(dimensions=dimensions)


async def test_the_fake_returns_the_configured_width() -> None:
    provider = FakeEmbeddingProvider(dimensions=64)
    batch = await provider.embed_documents(["hello", "world"])
    assert batch.dimensions == 64
    assert len(batch) == 2
    assert all(len(vector) == 64 for vector in batch.vectors)


async def test_the_fake_produces_unit_vectors() -> None:
    """Unit length means cosine is well defined and the ranker cannot produce NaNs."""
    provider = FakeEmbeddingProvider(dimensions=32)
    batch = await provider.embed_documents(["a website for a clinic in Hyderabad"])
    norm = sum(value * value for value in batch.only) ** 0.5
    assert norm == pytest.approx(1.0)


async def test_the_fake_handles_text_that_would_otherwise_be_a_zero_vector() -> None:
    provider = FakeEmbeddingProvider(dimensions=8)
    for text in ("", " ", "a"):
        batch = await provider.embed_documents([text])
        norm = sum(value * value for value in batch.only) ** 0.5
        assert norm == pytest.approx(1.0)


async def test_the_fake_is_deterministic_within_a_process() -> None:
    first = await FakeEmbeddingProvider(dimensions=32).embed_documents(["same text"])
    second = await FakeEmbeddingProvider(dimensions=32).embed_documents(["same text"])
    assert first.vectors == second.vectors


async def test_the_fake_carries_lexical_signal() -> None:
    """Overlapping text must score higher than unrelated text.

    Without this the harness could not be tested at all: a ranker driven by noise
    produces an arbitrary ordering, and an ordering bug would be invisible.
    """
    provider = FakeEmbeddingProvider(dimensions=512)
    batch = await provider.embed_documents(
        [
            "we build websites for indian businesses",
            "we build websites for indian companies",
            "support is open monday to saturday",
        ]
    )
    near_a, near_b, far = batch.vectors

    def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert cosine(near_a, near_b) > cosine(near_a, far)


async def test_the_fake_reports_no_token_usage() -> None:
    """It does not tokenise, so it must not pretend to."""
    batch = await FakeEmbeddingProvider(dimensions=8).embed_documents(["x"])
    assert batch.usage.prompt_tokens is None


async def test_document_and_query_roles_produce_different_vectors() -> None:
    """The fake applies a role prefix, exercising the asymmetric-model code path.

    Two separate methods exist precisely because several candidate models need a
    query/passage prefix, and a caller that used the wrong one would get quietly worse
    retrieval with no error.
    """
    provider = FakeEmbeddingProvider(dimensions=64)
    as_document = await provider.embed_documents(["how long does a website take"])
    as_query = await provider.embed_query("how long does a website take")
    assert as_document.only != as_query.only


async def test_the_fake_records_what_it_embedded_and_in_which_role() -> None:
    provider = FakeEmbeddingProvider(dimensions=8)
    await provider.embed_documents(["one", "two"])
    await provider.embed_query("three")
    assert provider.calls == (
        (TextRole.DOCUMENT, "one"),
        (TextRole.DOCUMENT, "two"),
        (TextRole.QUERY, "three"),
    )


async def test_scripted_vectors_override_the_hasher() -> None:
    """Lets a test pin an exact similarity ordering instead of reverse-engineering the
    hasher."""
    provider = FakeEmbeddingProvider(
        dimensions=2,
        scripted={
            (TextRole.QUERY, "q"): (1.0, 0.0),
            (TextRole.DOCUMENT, "near"): (1.0, 0.0),
            (TextRole.DOCUMENT, "far"): (0.0, 1.0),
        },
    )
    assert (await provider.embed_query("q")).only == (1.0, 0.0)
    documents = await provider.embed_documents(["near", "far"])
    assert documents.vectors == ((1.0, 0.0), (0.0, 1.0))


def test_a_scripted_vector_of_the_wrong_width_is_refused() -> None:
    with pytest.raises(InvariantViolation, match="width"):
        FakeEmbeddingProvider(dimensions=4, scripted={(TextRole.QUERY, "q"): (1.0, 0.0)})


async def test_nfc_means_both_nukta_spellings_embed_identically() -> None:
    """The same Unicode trap as everywhere else in this codebase, at the fake.

    Without NFC the two spellings hash to different trigrams and a Hindi passage would
    embed differently depending on which form the source file happened to use.
    """
    provider = FakeEmbeddingProvider(dimensions=32)
    suffix = chr(0x094B) + chr(0x0928)
    precomposed = chr(0x095E) + suffix
    decomposed = chr(0x092B) + chr(0x093C) + suffix
    assert precomposed != decomposed
    batch = await provider.embed_documents([precomposed, decomposed])
    assert batch.vectors[0] == batch.vectors[1]


def test_the_fake_is_deterministic_across_processes() -> None:
    """The reason it uses `hashlib` rather than `hash()`.

    Python salts `hash()` for `str` per process unless `PYTHONHASHSEED` is pinned, so a
    `hash()`-based fake returns different vectors in two interpreters. That failure only
    appears across process boundaries, so only a subprocess test can catch it. Two
    runs are launched with *different* explicit hash seeds to make the point sharply.
    """
    script = textwrap.dedent(
        """
        import asyncio, json
        from rn_providers.fakes import FakeEmbeddingProvider

        async def main():
            provider = FakeEmbeddingProvider(dimensions=16)
            batch = await provider.embed_documents(["websites for indian businesses"])
            print(json.dumps([round(v, 12) for v in batch.only]))

        asyncio.run(main())
        """
    )
    outputs = []
    for seed in ("0", "12345"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**_clean_env(), "PYTHONHASHSEED": seed},
        )
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1], "the fake is not reproducible across hash seeds"


def _clean_env() -> dict[str, str]:
    import os

    return dict(os.environ)
