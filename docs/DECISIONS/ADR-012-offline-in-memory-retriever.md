# ADR-012: An offline in-memory knowledge retriever, built ahead of Phase 3 Stage 2

- Status: Accepted
- Date: 2026-08-12 (scope corrected 2026-08-13)
- Deciders: Platform architecture
- Supersedes / Superseded by: none. **Constrained by [ADR-010](ADR-010-defer-vector-storage-layout.md)**, which stays in force.

> **Scope:** where the orchestration half of Phase 3 Stage 2's retrieval lives, whether it may be built before D-8 closes, and how it is prevented from becoming an accidental answer to D-8.
> **Companions:** [ADR-010](ADR-010-defer-vector-storage-layout.md) · [../DATA_MODEL.md](../DATA_MODEL.md) §7 · [../research/D8_BAKEOFF.md](../research/D8_BAKEOFF.md) · [../ROADMAP.md](../ROADMAP.md) Phase 3.

> **Phase labelling correction (2026-08-13).** An earlier revision of this ADR called this work "Phase 4A". **No such phase exists.** Retrieval and the twelve knowledge/lead tools are **Phase 3** deliverables ([ROADMAP](../ROADMAP.md) Phase 3, [PRD §12 D-8](../../PRD.md), [ARCHITECTURE §8.2](../ARCHITECTURE.md), [DATA_MODEL §7](../DATA_MODEL.md)); the repository's Phase 4 is provider seams, fakes and the audio transcoder, and has nothing to do with knowledge. The label is corrected throughout. Nothing about the decision itself changed.

## Context

Phase 3 Stage 1 closed on 2026-08-11 with a human-reviewed corpus of 143 passages. The chunker, the embedding seam, the deterministic offline embedder, the sanitisation flags, the tool registry, the dispatcher and the conversation loop all exist. What did not exist was anything that turns a question into passages — and it could not be built the intended way, because the intended way is a `document_chunks` table with a vector column, and [ADR-010](ADR-010-defer-vector-storage-layout.md) forbids creating one before ADR-011 records a measured answer to **D-8**.

[DATA_MODEL §7](../DATA_MODEL.md) already resolves where retrieval lives, and it is two places: **orchestration** in `rn_services` (embed the query, resolve knowledge-base scope, decide `k`, shape the result, report under-return) and **implementation** in `rn_persistence` (one function, one parameterised statement, the tenant predicate, the tuning, the distance ordering). Only the second half is blocked by D-8. The first half is blocked by nothing.

## The rule this decision makes an exception to

`CLAUDE.md` states, and continues to state:

> Stage 2 (migration `0003`, `document_chunks`, `vector_search()`, the 12 Phase-3 tools) must not be written until ADR-011 records the measured answer.

`search_knowledge` is one of those twelve. **This ADR is the exception, recorded explicitly, and it is deliberately narrow.** The rule exists because Stage-2 work depends on the D-8 answer — the column type, the width, the index. What was built depends on none of them: no schema, no vector, no model, no width, no distance operator. The rule's *intent* is untouched; its *letter* would have forbidden this, so the exception is written down here rather than granted by quietly editing the rule.

**A previous working session did edit that rule** — changing "the 12 Phase-3 tools" to "the remaining 11" — to accommodate work already built. That was the wrong repair: it made the architecture's own constraint follow the implementation instead of the other way round. The sentence has been restored and this section is the correct mechanism.

## Options considered

| Option | Case for | Why it lost / won |
|---|---|---|
| **Wait for Stage 2 entirely** | No exception to record, no prototype to delete later. | D-8 is blocked on two business inputs that are not engineering's to supply, and had been for weeks. Waiting means the reviewed corpus stays unexercised and the tool seam stays unproven for an unbounded period. **Rejected**, but note this was the honest default and the exception has to earn its place against it. |
| **Keep the whole retriever in `tests/`** — a stub, like `StubKnowledgeCatalog` | Production packages untouched. There is precedent in the Phase-2 evaluation suite. | The precedent does not carry. A stub stands in for a service that exists elsewhere; here there would *be* nowhere else, so `search_knowledge` would ship with no callable path outside pytest and the demo would be demonstrating a fixture. **Rejected.** |
| **Build the SQL half now against a provisional schema** | One implementation, the real one, from the start. | Requires choosing a column type and a width — which *is* D-8, decided by the need to get a demo running. Exactly the failure ADR-010 exists to prevent. **Rejected outright.** |
| **Orchestration in `rn_services`, over an injected in-memory index** *(chosen)* | Puts the layer where DATA_MODEL §7 already says it goes. The seam the agent depends on is the one Stage 2 keeps, so swapping the index backend changes no caller. Nothing about the physical layout is implied, because there is no physical layout. | Cost: a production module that holds a tenant's corpus in memory and re-embeds it at start-up, which will be superseded. Accepted, bounded by the constraints below. |

## Decision

**`rn_services.retrieval` holds `build_in_memory_index()` and `InMemoryKnowledgeRetriever`, implementing the `KnowledgeRetriever` protocol declared in `rn_services.contracts`. It is offline, tenant-scoped, exact, and issues no SQL. `search_knowledge` is registered against it.**

Six constraints make it safe to have:

1. **The protocol is the stable artifact; the implementation is not.** `KnowledgeRetriever` is what `rn_agent` depends on and what Stage 2's service will satisfy. `InMemoryKnowledgeRetriever` is expected to be deleted.
2. **No storage decision is implied.** No column type, no width, no index strategy, no partitioning. The embedding provider is injected, and the model id and width are read off the batch that produced the vectors rather than configured anywhere.
3. **No embedding model is chosen.** `EmbeddingSettings.model` and `.dimensions` stay `None` with refusing accessors. The only provider used is `FakeEmbeddingProvider`, constructed in the test tree with an explicit width, and **no number it produces is evidence about retrieval quality**.
4. **Search is exact and the filter precedes the ranking**, so HC-25 cannot occur here. `RetrievalResult.underfilled` is nonetheless computed and logged, because it is the only observable symptom of that trap and the caller that reads it must already exist when the SQL implementation, which *can* suffer it, replaces this one.
5. **Ingestion sanitisation is applied at index time, not query time.** Instruction-shaped chunks are withheld before they are embedded, so no vector for one exists to be served by a later filter bug ([SECURITY §5.4](../SECURITY.md) step 6). Price-shaped chunks are kept and flagged, never repaired.
6. **It stays generic.** No tenant's content, names or vocabulary appear in it. The Rise Next corpus is loaded only by `tests/demo_aira`, which reads the D-8 data files read-only.

### What this does **not** authorise

The other **11** Phase-3 tools, migration `0003`, `document_chunks`, any vector column, `vector_search()`, any ANN index, any ingestion API or job, and any embedding-model selection all remain behind **ADR-011**. This exception covers one tool and one in-memory orchestration module, and nothing else.

## Consequences

**Good.** The reviewed corpus is exercised end to end — real registry, real dispatcher, real conversation loop — with no database, no network, no credential and no cost, while every irreversible decision stays open. `search_knowledge` needed no migration: `org:knowledge:read` has been in the frozen permission catalog since migration `0001`.

**Bad.** There is a production module that does not scale and will be replaced. It re-embeds a tenant's whole corpus on every process start and holds it in memory. Anyone reading it out of context could mistake it for the retrieval path. And the phase boundary is now genuinely blurred: part of Stage 2 exists while Stage 2 is formally blocked, which is a state that has to be *read* to be understood rather than inferred from the roadmap.

**Mitigation, and the rule this ADR exists to state:** *do not extend it.* The answer to "this is slow", "this needs to persist" or "this needs an index" is the rest of Stage 2 and ADR-011 — never a cache, a background rebuild or an approximate index bolted on here. Nothing in the production tree may construct it: the composition happens in `tests/demo_aira`, and it should stay that way until an application composition root exists to construct the real one.

**What stays untouched.** No migration, no `document_chunks`, no vector column, no pgvector index, no `TARGET_PASSAGES` change and no corpus change. The Phase-1 invariant test that asserts no vector column exists still passes, and it remains the guard against D-8 being decided by accident.
