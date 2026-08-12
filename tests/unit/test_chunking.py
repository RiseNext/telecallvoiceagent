"""The chunker's invariants.

The four guarantees `chunk_document` documents are asserted here as properties over
several shapes of input, not as one happy-path example — the interesting failures are
all in the awkward cases: a paragraph longer than the ceiling, a single unbreakable
word, text that normalises to nothing, and Indic text where a naive splitter cuts a
matra away from its consonant.

`FROZEN_CHUNKING_V1` is asserted to be exactly what the D-8 bake-off was run against.
If someone changes it, that test fails and the D-8 numbers become incomparable — which
is the point: the failure is the notification.
"""

from __future__ import annotations

import itertools

import pytest

from rn_core.errors import InvariantViolation
from rn_domain.chunking import (
    FROZEN_CHUNKING_V1,
    ChunkingPolicy,
    chunk_document,
)
from rn_domain.text import grapheme_length, normalise_text

pytestmark = [pytest.mark.unit]

TIGHT = ChunkingPolicy(
    target_graphemes=100,
    max_graphemes=160,
    overlap_graphemes=15,
    min_graphemes=20,
    version="test-tight",
)

NO_OVERLAP = ChunkingPolicy(
    target_graphemes=60,
    max_graphemes=90,
    overlap_graphemes=0,
    min_graphemes=1,
    version="test-no-overlap",
)

ENGLISH = (
    "Website design and development. We build business websites, landing pages and "
    "online storefronts for Indian businesses.\n\n"
    "Every site is mobile-first because most customers arrive on a phone over a "
    "patchy connection. The work covers design, build, testing and launch.\n\n"
    "A typical brochure site takes four to six weeks. An e-commerce build with "
    "payments and inventory takes eight to twelve weeks."
)

HINDI = (
    "वेबसाइट डिज़ाइन और डेवलपमेंट। हम व्यवसायों के लिए वेबसाइट और ऑनलाइन दुकानें बनाते हैं।\n\n"
    "हर वेबसाइट पहले मोबाइल के लिए बनाई जाती है क्योंकि ग्राहक अक्सर फ़ोन से आते हैं।"
)

TELUGU = (
    "వెబ్‌సైట్ డిజైన్ మరియు అభివృద్ధి. మేము వ్యాపారాల కోసం వెబ్‌సైట్లు నిర్మిస్తాము.\n\nప్రతి వెబ్‌సైట్ మొదట మొబైల్ కోసం రూపొందించబడుతుంది."
)

CORPUS = [ENGLISH, HINDI, TELUGU]


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


def test_overlap_at_or_above_the_target_is_refused() -> None:
    """An overlap that re-seeds a full chunk means the packer can never advance.

    Refused at construction rather than discovered as a hang, which is what an
    unbounded packing loop would look like from the outside.
    """
    with pytest.raises(InvariantViolation):
        ChunkingPolicy(
            target_graphemes=100,
            max_graphemes=200,
            overlap_graphemes=100,
            min_graphemes=10,
            version="bad",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_graphemes": 0, "max_graphemes": 10, "overlap_graphemes": 0, "min_graphemes": 0},
        {"target_graphemes": 100, "max_graphemes": 50, "overlap_graphemes": 0, "min_graphemes": 0},
        {
            "target_graphemes": 100,
            "max_graphemes": 200,
            "overlap_graphemes": -1,
            "min_graphemes": 0,
        },
        {
            "target_graphemes": 100,
            "max_graphemes": 200,
            "overlap_graphemes": 0,
            "min_graphemes": 500,
        },
    ],
)
def test_incoherent_policies_are_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(InvariantViolation):
        ChunkingPolicy(version="bad", **kwargs)


def test_a_policy_must_carry_a_version() -> None:
    with pytest.raises(InvariantViolation):
        ChunkingPolicy(
            target_graphemes=100,
            max_graphemes=200,
            overlap_graphemes=10,
            min_graphemes=10,
            version="   ",
        )


def test_the_frozen_policy_is_exactly_what_d8_was_run_against() -> None:
    """A guard, not a tautology.

    Changing any of these numbers invalidates every recorded D-8 measurement, because
    a model comparison under two different chunkings is not a model comparison. The
    failing test is the notification that the numbers have to be re-taken.
    """
    assert FROZEN_CHUNKING_V1.version == "chunking-v1"
    assert FROZEN_CHUNKING_V1.target_graphemes == 700
    assert FROZEN_CHUNKING_V1.max_graphemes == 1000
    assert FROZEN_CHUNKING_V1.overlap_graphemes == 100
    assert FROZEN_CHUNKING_V1.min_graphemes == 80


# ---------------------------------------------------------------------------
# Documented guarantees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("policy", [TIGHT, NO_OVERLAP, FROZEN_CHUNKING_V1])
def test_no_chunk_exceeds_the_ceiling(text: str, policy: ChunkingPolicy) -> None:
    for chunk in chunk_document(text, policy=policy):
        assert chunk.grapheme_count <= policy.max_graphemes, chunk.text


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("policy", [TIGHT, NO_OVERLAP, FROZEN_CHUNKING_V1])
def test_no_chunk_is_empty_and_indices_are_dense(text: str, policy: ChunkingPolicy) -> None:
    chunks = chunk_document(text, policy=policy)
    assert chunks
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.text.strip()
        assert chunk.grapheme_count == grapheme_length(chunk.text)


@pytest.mark.parametrize("text", CORPUS)
def test_no_grapheme_cluster_is_split_across_chunks(text: str) -> None:
    """Every chunk must start and end on a cluster boundary.

    Checked by asserting each chunk's own segmentation round-trips: a chunk that began
    with an orphaned combining mark would still round-trip, so the stronger check is
    that no chunk *starts* with a combining mark, which is what a mid-cluster split
    produces.
    """
    for chunk in chunk_document(text, policy=TIGHT):
        assert "".join(list(chunk.text)) == chunk.text
        first = chunk.text[0]
        assert not _is_combining(first), f"chunk starts mid-cluster: {chunk.text[:20]!r}"


def _is_combining(char: str) -> bool:
    import unicodedata

    return unicodedata.category(char) in {"Mn", "Mc", "Me"}


@pytest.mark.parametrize("text", CORPUS)
def test_no_content_is_dropped(text: str) -> None:
    """Every word of the normalised source appears in at least one chunk.

    A weaker statement than exact reconstruction, and deliberately so: overlap means
    concatenating the chunks duplicates text, so exact reconstruction is not the
    property. What must hold is that nothing was *lost*, which is the failure that
    would silently remove a tenant's content from retrieval.
    """
    chunks = chunk_document(text, policy=TIGHT)
    combined = " ".join(chunk.text for chunk in chunks)
    for word in normalise_text(text).split():
        assert word in combined, f"word dropped by chunking: {word!r}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "\x00\x07"])
def test_text_that_normalises_to_nothing_yields_no_chunks(text: str) -> None:
    """A legitimate outcome, not an error: a document can be all whitespace or all
    control characters, and the ingestion pipeline has to handle zero chunks."""
    assert chunk_document(text) == ()


def test_a_single_unbreakable_word_is_split_at_the_ceiling() -> None:
    """The grapheme floor. Reached by a URL or an unbroken identifier."""
    word = "x" * 400
    chunks = chunk_document(word, policy=TIGHT)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.grapheme_count <= TIGHT.max_graphemes


@pytest.mark.parametrize(
    "policy",
    [
        # Overlap large relative to the ceiling — the shape that produced the bug.
        ChunkingPolicy(
            target_graphemes=80,
            max_graphemes=100,
            overlap_graphemes=60,
            min_graphemes=5,
            version="regression-wide-overlap",
        ),
        ChunkingPolicy(
            target_graphemes=30,
            max_graphemes=32,
            overlap_graphemes=29,
            min_graphemes=1,
            version="regression-extreme-overlap",
        ),
    ],
)
@pytest.mark.parametrize("text", [*CORPUS, "y" * 500, "word " * 200])
def test_the_ceiling_holds_even_when_overlap_dominates(policy: ChunkingPolicy, text: str) -> None:
    """Regression: a seeded chunk plus one unconditionally-appended atom.

    The packer must append one atom after a flush whatever its size, or an atom larger
    than the target could never be placed. That made the widest possible chunk
    `overlap + separator + atom`, so bounding atoms by `max_graphemes` produced chunks
    above `max_graphemes` — observed at 176 against a ceiling of 160 before
    `_atom_ceiling` existed. These policies make the overlap large enough that the old
    arithmetic cannot hide.
    """
    for chunk in chunk_document(text, policy=policy):
        assert chunk.grapheme_count <= policy.max_graphemes, (
            f"{chunk.grapheme_count} > {policy.max_graphemes} for {policy.version}"
        )


def test_short_text_is_one_chunk() -> None:
    chunks = chunk_document("Support is open Monday to Saturday.", policy=FROZEN_CHUNKING_V1)
    assert len(chunks) == 1
    assert chunks[0].index == 0


def test_chunking_is_deterministic() -> None:
    """Two runs must agree byte for byte, or a re-index changes retrieval results for
    reasons unrelated to the content."""
    first = chunk_document(ENGLISH, policy=TIGHT)
    second = chunk_document(ENGLISH, policy=TIGHT)
    assert [chunk.text for chunk in first] == [chunk.text for chunk in second]


def test_every_chunk_records_the_policy_that_produced_it() -> None:
    """Carried per chunk because a re-index can leave two policies' rows coexisting
    mid-migration, and "which policy produced this row" must be answerable from the
    row."""
    for chunk in chunk_document(ENGLISH, policy=TIGHT):
        assert chunk.policy_version == "test-tight"


def test_overlap_actually_overlaps() -> None:
    """Consecutive chunks must share text, or a fact that straddles a boundary is
    retrievable from neither side."""
    chunks = chunk_document(ENGLISH, policy=TIGHT)
    assert len(chunks) > 1
    shared = 0
    for previous, following in itertools.pairwise(chunks):
        tail_words = set(previous.text.split()[-6:])
        head_words = set(following.text.split()[:6])
        if tail_words & head_words:
            shared += 1
    assert shared > 0, "no consecutive pair shared any text despite a non-zero overlap"


def test_zero_overlap_produces_no_shared_text() -> None:
    """The control for the test above: with overlap disabled there is nothing shared,
    which is what makes the previous assertion meaningful rather than incidental."""
    chunks = chunk_document(ENGLISH, policy=NO_OVERLAP)
    assert len(chunks) > 1
    joined = " ".join(chunk.text for chunk in chunks).split()
    assert len(joined) == len(normalise_text(ENGLISH).split())


def test_a_paragraph_break_survives_inside_a_chunk() -> None:
    """The author chose that boundary and a caller hears the text quoted back."""
    chunks = chunk_document(ENGLISH, policy=FROZEN_CHUNKING_V1)
    assert any("\n\n" in chunk.text for chunk in chunks)
