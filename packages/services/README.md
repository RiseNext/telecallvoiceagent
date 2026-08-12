# rn-services — application layer

The business use cases. This is where "what the product does" lives, and it is shared by all three planes so a given operation behaves identically whether a dashboard user triggered it, an agent called it as a tool mid-call, or a worker replayed it.

## Owns

- Use cases: campaign import and dispatch, contact and lead management, knowledge ingestion and retrieval, call lifecycle, meeting and callback booking, messaging, analytics queries, export generation.
- **Retrieval orchestration** — embedding a query through `EmbeddingProvider`, resolving which knowledge bases an agent version has in scope, choosing `k`, shaping the result for a tool envelope. It is the **only** caller of the one `<=>`-issuing function in `rn_persistence`, and it contains no SQL: business retrieval orchestration and the SQL vector-search implementation are two different things at two layers. See [DATA_MODEL §7](../../docs/DATA_MODEL.md#the-single-retrieval-helper--and-the-two-layers-it-is-split-across). *(Neither half exists yet — the physical schema is open decision D-8.)*
- The **authorization policy layer** — resource-oriented checks (`may this actor do X to this resource`), not conditionals scattered through route handlers.
- The **pre-dial compliance gate** — the ordered checks a contact must pass before a dial is enqueued.
- The **transactional outbox** — a state change and its intent-to-publish written in one transaction.

## Rules

- Framework-free: no FastAPI, no LangChain, no broker client. Enforced by contracts. This package is called from an HTTP handler, from a WebSocket session and from a job, so it must not assume any of them.
- The only layer that composes `rn_persistence` and `rn_providers`. Nothing above it touches either directly.
- Every tenant-scoped operation takes the organization identity as an explicit argument. There is no ambient tenant context to forget.
- Returns domain objects or typed results, never ORM models and never HTTP responses.

## Why no broker client

The voice gateway must not dual-write to Postgres and Redis — a crash between the two either loses a call-completion event or duplicates it. So services write to the outbox in the same transaction as the state change, and a relay in the worker publishes. See [ADR-008](../../docs/DECISIONS/ADR-008-transactional-outbox-for-call-events.md).
