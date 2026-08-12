"""A deterministic, offline `EmbeddingProvider`.

**What this is.** A character-trigram hashing embedder: it folds the trigrams of a
text into `dimensions` signed buckets and L2-normalises the result. That makes it
a real *lexical* similarity function — identical text scores 1.0, overlapping text
scores higher than unrelated text — so the ingestion pipeline, the retrieval
helper and the D-8 harness can all be exercised end to end with no network, no
credentials and no cost.

**What this is not, and must never be quoted as.** It has no semantic
understanding, no cross-lingual capability and no cross-script capability
whatsoever: a Hindi sentence and its English translation share almost no
trigrams, so they score near zero. **No number produced with this provider is
evidence about retrieval quality**, and the D-8 harness refuses to write a
decision-grade report from it (see `tests/d8_bakeoff/harness.py`). It tests
machinery. It does not measure models.

Determinism is load-bearing and slightly fiddly:

* **`hashlib`, never `hash()`.** Python salts `hash()` for `str` per process
  unless `PYTHONHASHSEED` is pinned, so a `hash()`-based fake would return
  different vectors in two processes and produce test failures that look like
  flakes. `blake2b` is stable across processes, machines and Python versions.
* **NFC normalisation before hashing.** Devanagari and Telugu text arrives in
  both composed and decomposed forms, and the precomposed nukta trap that bit the
  Phase-2 opt-out matcher applies here too: without normalising, two spellings of
  the same word hash to different trigrams. The guardrail matchers already learned
  this the expensive way.
* **Signed buckets.** An all-positive vector makes every pair of texts look
  similar and turns ranking into noise, which would hide a genuine ordering bug in
  the harness.
* **No clock, no randomness, no I/O.** A fake that is not reproducible is a source
  of flakes rather than a defence against them.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Mapping, Sequence

from rn_core.errors import InvariantViolation
from rn_providers.embeddings import (
    EmbeddingBatch,
    EmbeddingUsage,
    EmbeddingVector,
    TextRole,
)

__all__ = ["MAX_FAKE_DIMENSIONS", "MIN_FAKE_DIMENSIONS", "FakeEmbeddingProvider"]

#: Bounds on the configured width. The lower bound is 2 because a 1-dimensional
#: L2-normalised vector can only ever be +1 or -1, which makes every cosine either
#: 1 or -1 and would let a broken ranker pass. The upper bound catches a typo like
#: `dimensions=15360` before it turns a unit test into a minute of arithmetic.
MIN_FAKE_DIMENSIONS = 2
MAX_FAKE_DIMENSIONS = 8192

#: Trigrams. Long enough to carry word-shape signal, short enough that two
#: sentences sharing a phrase overlap. Fixed rather than configurable: a knob here
#: would be a knob whose effect on a test nobody could reason about.
_NGRAM = 3

#: Prefixes applied per role, so the fake exercises the asymmetric-model code path
#: that `embed_documents`/`embed_query` exist for. The consequence is deliberate
#: and slightly counter-intuitive: embedding the *same* text as a document and as a
#: query gives *different* vectors, exactly as a real asymmetric model would. A
#: test that assumed otherwise was relying on symmetry it should not rely on.
_ROLE_PREFIX: Mapping[TextRole, str] = {
    TextRole.DOCUMENT: "passage: ",
    TextRole.QUERY: "query: ",
}


class FakeEmbeddingProvider:
    """A deterministic lexical embedder. Satisfies `EmbeddingProvider` structurally.

    Args:
        dimensions: The width to produce. **Deliberately required, with no
            default.** A default here would quietly become the number every test
            and fixture was written against, and D-8 has not chosen a width — a
            fake with a default width is how a vendor default becomes a de facto
            decision, which is the exact failure ADR-010 exists to prevent.
        model_id: Recorded on every batch. Defaults to a name that could never be
            mistaken for a real model in a log line or a stored row.
        scripted: Exact vectors for exact texts, keyed by `(role, text)`. Lets a
            test pin a precise similarity ordering instead of reverse-engineering
            what the hasher will do. Looked up on the **raw** text, before any
            normalisation, so a test writes what it means.
    """

    __slots__ = ("_calls", "_dimensions", "_model_id", "_scripted")

    def __init__(
        self,
        *,
        dimensions: int,
        model_id: str = "fake-lexical-hashing-embedder",
        scripted: Mapping[tuple[TextRole, str], Sequence[float]] | None = None,
    ) -> None:
        if not MIN_FAKE_DIMENSIONS <= dimensions <= MAX_FAKE_DIMENSIONS:
            raise InvariantViolation(
                "FakeEmbeddingProvider was configured with an unusable width.",
                detail={
                    "dimensions": dimensions,
                    "min": MIN_FAKE_DIMENSIONS,
                    "max": MAX_FAKE_DIMENSIONS,
                },
            )
        self._dimensions = dimensions
        self._model_id = model_id
        self._scripted: dict[tuple[TextRole, str], EmbeddingVector] = {}
        for key, vector in (scripted or {}).items():
            if len(vector) != dimensions:
                raise InvariantViolation(
                    "A scripted embedding does not match the configured width.",
                    detail={
                        "role": key[0].value,
                        "expected_dimensions": dimensions,
                        "actual": len(vector),
                    },
                )
            self._scripted[key] = tuple(float(value) for value in vector)
        self._calls: list[tuple[TextRole, str]] = []

    # -- protocol ----------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        return self._batch(texts, role=TextRole.DOCUMENT)

    async def embed_query(self, text: str) -> EmbeddingBatch:
        return self._batch([text], role=TextRole.QUERY)

    # -- test surface ------------------------------------------------------

    @property
    def calls(self) -> tuple[tuple[TextRole, str], ...]:
        """Every text embedded, with its role, in order.

        The point is the *role*: it lets a test assert that the ingestion pipeline
        embedded chunks as documents and the retrieval path embedded the query as a
        query. Getting those the wrong way round is a silent quality regression on
        an asymmetric model, which is precisely the failure the two-method seam
        exists to make visible.
        """
        return tuple(self._calls)

    # -- internals ---------------------------------------------------------

    def _batch(self, texts: Sequence[str], *, role: TextRole) -> EmbeddingBatch:
        vectors: list[EmbeddingVector] = []
        for text in texts:
            self._calls.append((role, text))
            scripted = self._scripted.get((role, text))
            vectors.append(scripted if scripted is not None else self._embed(text, role=role))
        return EmbeddingBatch(
            vectors=tuple(vectors),
            model_id=self._model_id,
            dimensions=self._dimensions,
            # Both `None`, not zero. This provider does not tokenise, so it has no
            # token count; reporting `0` would be a measurement it did not make,
            # and someone would eventually assert on it or sum it into a cost.
            usage=EmbeddingUsage(),
        )

    def _embed(self, text: str, *, role: TextRole) -> EmbeddingVector:
        prepared = _prepare(_ROLE_PREFIX[role] + text)
        buckets = [0.0] * self._dimensions
        for gram in _trigrams(prepared):
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:6], "big") % self._dimensions
            # One bit of the same digest chooses the sign, so the bucket and the
            # sign cannot disagree between runs.
            buckets[index] += 1.0 if digest[7] & 1 else -1.0
        return _l2_normalise(buckets)


def _prepare(text: str) -> str:
    """NFC, casefolded, whitespace-collapsed, with sentinel padding.

    Padding with a character that cannot appear in text means a one- or two-
    character input still produces trigrams, so a short query is not silently
    embedded as the zero vector.
    """
    normalised = unicodedata.normalize("NFC", text).casefold()
    collapsed = " ".join(normalised.split())
    return f"\x02\x02{collapsed}\x03\x03"


def _trigrams(text: str) -> list[str]:
    if len(text) < _NGRAM:
        return [text]
    return [text[index : index + _NGRAM] for index in range(len(text) - _NGRAM + 1)]


def _l2_normalise(values: list[float]) -> EmbeddingVector:
    """Scale to unit length, or return a fixed unit vector for a zero result.

    `math.fsum` rather than `sum`: the buckets are ±1 accumulations and a plain
    float sum of many terms drifts, which would make the norm — and therefore every
    similarity — depend on iteration order.
    """
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm == 0.0:
        # Reachable when every trigram cancels itself out. A zero vector has no
        # defined cosine, so return a deterministic unit vector instead of
        # producing NaNs three layers away in the ranker.
        first, *rest = [1.0] + [0.0] * (len(values) - 1)
        return (first, *rest)
    return tuple(value / norm for value in values)
