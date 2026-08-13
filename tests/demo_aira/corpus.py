"""Adapt the D-8 corpus into tenant knowledge documents. Read-only.

The 143 passages that Phase 3 built from the official Rise Next material, reused as the
demo tenant's knowledge base. Reused rather than copied, deliberately: a second copy of
the corpus would drift from the reviewed one, and the review is the expensive part.

**This module reads `tests/d8_bakeoff/` and never writes it.** It imports `load_dataset`
and nothing else from that package — no gate, no target, no metric, no builder. If this
file were deleted the bake-off would be exactly as it was.

**Why the passages become documents rather than chunks.** `Passage.text` is a *chunk
candidate*, but handing pre-chunked text to the index would skip `chunk_document` and
the demo would prove the retrieval path works on input the retrieval path will never
see. So each passage is fed in as a document and the frozen policy chunks it — which
for most of these passages produces exactly one chunk, and for the long ones produces
the several the policy is there to produce.

**The adversarial passages are loaded too.** All 17 distractors and all 3
instruction-shaped passages go in, because a demo that quietly drops the hard cases is a
demo of an easier corpus than the one we have. The instruction-shaped three are what the
index quarantines, and that is worth watching happen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from rn_core.ids import new_id
from rn_domain.identifiers import KnowledgeBaseId, OrganizationId
from rn_services.retrieval import KnowledgeDocument
from tests.d8_bakeoff.dataset import Passage, load_dataset

__all__ = [
    "KNOWLEDGE_BASE_NAMES",
    "DemoCorpus",
    "load_demo_corpus",
]

#: `provenance.source_type` -> the knowledge base a passage is filed under.
#:
#: A knowledge base is what a tenant admin organises content into, and `source_type` is
#: the closest thing the corpus records to that. Mapped explicitly rather than derived,
#: so a new source type in a future corpus build lands in "Company Information" and is
#: visible, instead of silently creating a knowledge base nobody configured.
_SOURCE_TYPE_TO_BASE: Final[Mapping[str, str]] = {
    "capabilities": "Services",
    "services": "Services",
    "technology_capabilities": "Services",
    "industries": "Services",
    "company": "Company Information",
    "company_profile": "Company Information",
    "faqs": "Frequently Asked Questions",
    "policies": "Policies",
    "business_process": "Policies",
    "pricing_policy": "Policies",
    "financing_disclaimer": "Policies",
}

_DEFAULT_BASE: Final[str] = "Company Information"

#: The knowledge bases the demo tenant has, in a stable order.
KNOWLEDGE_BASE_NAMES: Final[tuple[str, ...]] = (
    "Company Information",
    "Frequently Asked Questions",
    "Policies",
    "Services",
)


@dataclass(frozen=True, slots=True)
class DemoCorpus:
    """One tenant's worth of knowledge documents, plus the ids they were filed under."""

    organization_id: OrganizationId
    documents: tuple[KnowledgeDocument, ...]
    knowledge_base_ids: Mapping[str, KnowledgeBaseId]
    #: How many passages the D-8 dataset held. Reported so the demo can say what it
    #: loaded rather than asserting a number that would have to be maintained here.
    passage_count: int


def _base_name(passage: Passage) -> str:
    return _SOURCE_TYPE_TO_BASE.get(passage.provenance.source_type or "", _DEFAULT_BASE)


def load_demo_corpus(
    *,
    organization_id: OrganizationId,
    knowledge_base_ids: Mapping[str, KnowledgeBaseId] | None = None,
    passages: Sequence[Passage] | None = None,
) -> DemoCorpus:
    """Load the D-8 corpus as one tenant's knowledge documents.

    Args:
        organization_id: The tenant these documents belong to. Required — there is no
            default tenant, because a default is how content ends up filed under
            whichever organization happened to be constructed first.
        knowledge_base_ids: Reuse these ids instead of minting new ones. What the tenant
            isolation test uses to give two organizations knowledge bases with the same
            *names*, which is the case where a name-based scope would leak.
        passages: Override the source passages. For tests that need a small, pinned
            corpus; `None` loads the real one.
    """
    source = tuple(passages) if passages is not None else load_dataset().passages
    base_ids: dict[str, KnowledgeBaseId] = (
        dict(knowledge_base_ids)
        if knowledge_base_ids is not None
        else {name: KnowledgeBaseId(new_id()) for name in KNOWLEDGE_BASE_NAMES}
    )

    documents: list[KnowledgeDocument] = []
    for passage in source:
        name = _base_name(passage)
        if name not in base_ids:
            base_ids[name] = KnowledgeBaseId(new_id())
        documents.append(
            KnowledgeDocument(
                organization_id=organization_id,
                knowledge_base_id=base_ids[name],
                knowledge_base_name=name,
                # The passage id, so a retrieved chunk id traces straight back to the
                # reviewed corpus row it came from.
                document_id=passage.id,
                text=passage.text,
            )
        )

    return DemoCorpus(
        organization_id=organization_id,
        documents=tuple(documents),
        knowledge_base_ids=base_ids,
        passage_count=len(source),
    )
