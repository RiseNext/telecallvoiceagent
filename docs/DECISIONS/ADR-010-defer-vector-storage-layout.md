# ADR-010: Defer the embedding model and vector storage layout to the RAG phase

- Status: Accepted
- Date: 2026-07-29
- Deciders: Platform architecture
- Supersedes / Superseded by: **supersedes the Decision of [ADR-006](ADR-006-pgvector-tenant-isolation-and-embeddings.md)**; ADR-006's constraint analysis remains valid

> **Scope:** whether to commit now to `halfvec(1536)` and `PARTITION BY LIST (organization_id)`, or to defer both until the RAG workload is measured.
> **Companions:** [ADR-006](ADR-006-pgvector-tenant-isolation-and-embeddings.md) (the constraints, still valid) · [../DATA_MODEL.md](../DATA_MODEL.md) §6–7 · [../../PRD.md](../../PRD.md) §12 **D-8** · [../ROADMAP.md](../ROADMAP.md).

## Context

[ADR-006](ADR-006-pgvector-tenant-isolation-and-embeddings.md) chose a `document_chunks` table typed `halfvec(1536)` and partitioned `BY LIST (organization_id)`, and correctly flagged the column type as *close to irreversible*: the dimension is part of the type, so changing it is a full re-embed of every tenant plus a table rewrite. Partitioning is worse — it cannot be retrofitted onto a live vector table without a migration that rewrites everything.

So ADR-006 made two of the least reversible decisions in the system. The question this ADR asks is whether they were *earned* at the time they were made. They were not, for four reasons:

**1. The dimension was chosen by the model, and the model is not chosen.** 1536 is the native output width of `text-embedding-3-small`. Nothing about 1536 was evaluated on merit — it is a consequence of a provider default that we adopted before measuring anything. ADR-006 itself records the problem: **L-8 — no official per-language benchmark exists for OpenAI embeddings on Indic languages**, and this is an India-first product whose corpus is English/Hindi/Telugu and code-mixed. We have not benchmarked OpenAI embeddings against Indic-specialised alternatives on our own data. Baking a provider's default width into a column type *before* that bake-off inverts the order of the decisions.

**2. `halfvec` was chosen to dodge a cap we are not near.** The HNSW dimension caps (**HC-24**: `vector` ≤ 2000, `halfvec` ≤ 4000) only bind above 2000 dimensions — which is `text-embedding-3-large` at 3072, a model we have not selected. At 1536 *both* types are indexable, so the `halfvec` argument reduces to storage and build-time savings against an unmeasured recall cost. Halving float precision to save disk, on a corpus whose size we have not estimated, for a recall delta we have not measured, is an optimisation ahead of a measurement.

**3. Provider independence is a stated platform requirement.** The PRD requires every external system to sit behind an interface, and `EmbeddingProvider` is one of them. A vector column whose width is a provider's default quietly makes the embedding provider the single hardest thing in the system to change — which is the opposite of what the seam exists for.

**4. LIST partitioning is unjustified at our tenant count.** V1 is RiseNext plus a handful of pilot organizations — single digits. The roadmap's ambition is tens to low hundreds of organizations, not tens of thousands. Partitioning solves a planner problem that appears when a *single* table's index no longer serves selective per-tenant queries well; at single-digit tenant counts with modest per-tenant corpora, a `document_chunks` table with a `(organization_id, ...)` B-tree and an exact scan is not merely adequate, it is *faster and exactly correct* — no ANN recall loss at all. Adopting partitioning now would be introducing structure for architectural appearance, which is explicitly something this codebase says it will not do.

The one thing in ADR-006 that is **not** deferrable, and stays: **HC-25 — filtered approximate search post-filters and silently under-returns.** That is a correctness trap and it shapes the retrieval helper regardless of physical layout.

## Options considered

| Option | Case for | Why it lost / won |
|---|---|---|
| **Keep ADR-006 as-is** — `halfvec(1536)`, LIST partitioning, in Phase 1 | Decided; the schema is ready; no further analysis needed. | Freezes the two least reversible choices in the system on the basis of a provider default and a tenant count we do not have. If the Indic bake-off picks a different model, the cost is a full re-embed and a table rewrite of every tenant. **Rejected.** |
| **Build it flexible now** — a generic `embeddings` table with a JSONB payload, or one nullable vector column per candidate model | Never have to migrate. | JSONB vectors are not indexable by pgvector, so this is not a vector store. Multiple nullable typed columns is a table that gets wider with every experiment and whose indexes multiply. Solving an unmeasured problem with a more complex unmeasured solution. **Rejected.** |
| **Defer: no vector column until the RAG phase** *(chosen)* | The decision is made when the evidence exists, at a cost of zero — nothing in Phases 1–2 needs a vector column. Tenancy, scoping and RLS land in Phase 1 and are entirely independent of physical vector layout. | Cost: knowledge tables arrive later than the rest of the schema, and Phase 3 carries a decision that would otherwise have been closed. Accepted — that is where the evidence is. |

## Decision

**Neither `halfvec(1536)` nor LIST partitioning is approved. Both are deferred to the RAG architecture phase (Phase 3), and are recorded as open decision D-8.**

Concretely:

1. **Phase 1 creates no vector column and no `document_chunks` table.** Phase 1 delivers tenancy, the core entities, scoped repositories, RLS and migrations. None of it depends on the vector layout.

2. **The starting position for Phase 3 is the simplest thing that is correct:** a single `document_chunks` table, tenant-scoped by `organization_id` with a B-tree, exact (non-ANN) search. At single-digit tenants with modest corpora this has **100% recall** and no HC-25 exposure at all. An ANN index is added when a measurement says exact search is too slow — not before.

3. **The schema records what produced each vector**, from the first migration that introduces one: `embedding_model`, `embedding_dim` and `embedded_at` per row. This is what makes model migration and coexistence tractable, and it costs nothing.

4. **Multiple embedding models may coexist during a migration**, by design. The intended mechanism is a per-knowledge-base *active model version*, with re-embedding running as a background job that writes new rows alongside the old ones and flips the active pointer when complete — so a re-embed is a rolling operation per tenant, not a platform-wide outage. Whether coexistence needs separate tables per model (different widths cannot share a typed column) or a per-model partition is part of D-8.

5. **Partitioning is not adopted.** It is revisited only against a measurement — see the triggers below.

6. **Tenant isolation is unaffected and unconditional.** It is enforced by scoped repositories plus RLS, both independent of physical layout. Partitioning was never the isolation mechanism and its absence weakens nothing. The single retrieval helper stays mandatory regardless, because HC-25 applies to any approximate index we ever add.

### What must be answered to close D-8

| Question | How it gets answered |
|---|---|
| Which embedding model? | Bake-off on **real Indic and code-mixed transcript/knowledge data**: OpenAI `text-embedding-3-small`/`-large`, at native and reduced widths, against Indic-specialised alternatives. Retrieval quality, not benchmark scores from elsewhere (L-8). |
| Which dimension? | Falls out of the model, plus a measured recall-vs-cost curve if the model supports reduction. |
| `vector` or `halfvec`? | Measured: storage, index build time, query latency and **recall delta** at our corpus size. Above 2000 dims HC-24 forces `halfvec`; below it, the choice must be earned. |
| Exact scan, or ANN? | Measured p95 retrieval latency at realistic per-tenant corpus sizes. Exact wins until it does not. |
| Partition or not? | Only if a measured planner or index-maintenance problem appears at the actual tenant count and corpus distribution. |
| Coexistence mechanism? | Follows from the width question — same-width models can share a column; different widths cannot. |

## Consequences

**Positive.** The two least reversible decisions in the system are now made with evidence rather than ahead of it. The `EmbeddingProvider` seam becomes real rather than nominal — nothing in the schema presumes a vendor's default width. Phase 1 gets smaller and lands sooner. The starting design (exact search) is the one with perfect recall, so the first version of RAG cannot exhibit the silent under-return failure that HC-25 describes.

**Negative.** Phase 3 carries an open decision that felt closed, and a bake-off is real work requiring a labelled Indic evaluation set we do not yet have — that set has to be built regardless for D-2, but this pulls it earlier. Exact search will not scale indefinitely; we are accepting a known future migration to an indexed layout in exchange for not guessing which one. If the corpus turns out to be far larger than expected, we will add an ANN index under time pressure rather than at leisure.

**What this forces us to do.** Build the Indic retrieval evaluation set as part of Phase 3, not as an afterthought. Keep `embedding_model`/`embedding_dim` on every vector row from day one. Write the retrieval helper so that swapping exact for ANN is a change inside it, with the tenant filter and `SET LOCAL` tuning already in place — the helper's *interface* must not change when the index does.

## Revisit when

- **Any tenant's chunk count approaches the point where exact scan misses the retrieval latency budget.** Measure first; then choose an index type against that measurement.
- **Tenant count reaches the low hundreds *and* a measured planner problem appears** on the single table. Partitioning is justified by both together, never by either alone.
- **The D-8 bake-off completes.** Then this ADR is discharged and a successor records the chosen model, width, column type and index strategy — with the numbers that chose them.
- **A second embedding model must run in production simultaneously** (a rolling re-embed, or per-tenant model choice). That forces the coexistence question early.
