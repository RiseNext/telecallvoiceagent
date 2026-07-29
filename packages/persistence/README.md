# rn-persistence — PostgreSQL

SQLAlchemy models, Alembic migrations, repositories, unit of work, and vector access.

## Owns

- ORM models and the mapping to/from domain entities.
- Alembic migrations — the only way schema changes.
- Repositories, which are the **only** place a tenant scope may be applied. A repository that can be called without an `organization_id` is a security bug waiting to happen.
- The single vector-search helper. Every retrieval goes through it so that no caller can forget the tenant filter or the index tuning.
- Engine and session lifecycle, including the two-connection split.

## Rules

- **Two connections, deliberately.** The pooled DSN runs in transaction mode: no session-level `SET`, no `LISTEN/NOTIFY`, no session advisory locks. Use `SET LOCAL` inside an explicit transaction. Migrations, index builds and the scheduler's leader lease use the direct DSN.
- **ORM models are never API contracts.** `rn_api` serializes from its own schemas.
- Migrations are reviewed for lock behaviour before they reach production. A migration that takes an exclusive lock on the calls table takes the phone system down.
- No business logic. A repository returns data; it does not decide anything.

## The trap worth repeating

Filtered approximate vector search **post-filters**: the index is scanned first, then the tenant predicate is applied, so a scoped query can return far fewer rows than requested without erroring. It looks like the agent forgot its knowledge base. See [ADR-006](../../docs/DECISIONS/ADR-006-pgvector-tenant-isolation-and-embeddings.md) and [DATA_MODEL.md](../../docs/DATA_MODEL.md).
