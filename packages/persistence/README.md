# rn-persistence — PostgreSQL

SQLAlchemy models, Alembic migrations, repositories, unit of work, and vector access.

## Owns

- ORM models and the mapping to/from domain entities.
- Alembic migrations — the only way schema changes.
- Repositories, which are the **only** place a tenant scope may be applied. A repository that can be called without an `organization_id` is a security bug waiting to happen.
- **The single `<=>`-issuing function.** Exactly one function here may construct or issue a pgvector distance query, so no caller can forget the tenant filter or the index tuning. The tenant predicate comes from the `TenantContext` the repository was constructed with, so there is no parameter by which a caller could supply a tenant.
  - **This is the SQL, not the orchestration.** Embedding the query text, resolving which knowledge bases are in scope, and shaping a result for a tool live in a retrieval *service* in `rn_services`, which is this function's only caller. `rn_agent` cannot reach here at all — an import contract forbids it. See [DATA_MODEL §7](../../docs/DATA_MODEL.md#the-single-retrieval-helper--and-the-two-layers-it-is-split-across).
  - **Not built yet.** Its column type and width are open decision **D-8**, so it cannot be written until ADR-011 lands.
- Engine and session lifecycle, including the two-connection split.

## Rules

- **Two connections, deliberately.** The pooled DSN runs in transaction mode: no session-level `SET`, no `LISTEN/NOTIFY`, no session advisory locks. Use `SET LOCAL` inside an explicit transaction. Migrations, index builds and the scheduler's leader lease use the direct DSN.
- **ORM models are never API contracts.** `rn_api` serializes from its own schemas.
- Migrations are reviewed for lock behaviour before they reach production. A migration that takes an exclusive lock on the calls table takes the phone system down.
- No business logic. A repository returns data; it does not decide anything.

## The trap worth repeating

Filtered approximate vector search **post-filters**: the index is scanned first, then the tenant predicate is applied, so a scoped query can return far fewer rows than requested without erroring. It looks like the agent forgot its knowledge base. See [ADR-006](../../docs/DECISIONS/ADR-006-pgvector-tenant-isolation-and-embeddings.md) and [DATA_MODEL.md](../../docs/DATA_MODEL.md).
