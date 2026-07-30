# CLAUDE.md — working instructions for this repository

## What this is

**RiseNext Voice AI Platform** — a multi-tenant platform for realtime AI voice agents that make and receive real phone calls in Indian languages (English, Hindi, Telugu, and code-mixed speech).

The first agent, *Aira*, is RiseNext's own sales assistant. **She is a tenant configuration, not the product.** If you find yourself writing `if org == "risenext"` or `risenext_agent.py`, stop — that is an architecture violation.

**Current state: Phase 2 complete.** `rn_core`, `rn_domain`, `rn_persistence` (21 tables, migrations `0001`+`0002`), the `rn_services` authorization seam and agent use cases, the `rn_providers` text-mode `LLMProvider` seam with its fake, and `rn_agent` — snapshot, instruction composition, typed tool registry, dispatch pipeline, guardrails, text conversation loop — are implemented and tested. **Nothing above them exists** — no audio, no telephony, no realtime voice, no retrieval, no job broker, no API endpoints, no frontend pages, and none of the 18 V1 tools. Check [docs/ROADMAP.md](docs/ROADMAP.md) before assuming anything works.

Three things that are deliberately absent and must stay that way until their phase:

- **Row-level security.** Phase 15. Tenant isolation today is application scoping plus composite foreign keys. Do not describe the current state as having RLS.
- **Any vector column or `document_chunks` table.** Phase 3, open decision **D-8** ([ADR-010](docs/DECISIONS/ADR-010-defer-vector-storage-layout.md)). A test asserts they do not exist — that test is the guard against D-8 being decided by accident.
- **Clerk, or any identity vendor.** `rn_services.authorization` is provider-independent on purpose; Clerk arrives behind `rn_providers.IdentityProvider`.

---

## Read these before changing anything

| Question | File |
|---|---|
| What are we building and what counts as done? | [PRD.md](PRD.md) |
| How is the system structured, and why? | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| What phase are we in, what is next? | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Why was a decision made this way? | [docs/DECISIONS/](docs/DECISIONS/) |
| **What did we actually verify about our providers?** | [docs/research/PROVIDER_CONSTRAINTS.md](docs/research/PROVIDER_CONSTRAINTS.md) |

That last one matters more than it looks. It separates **[C] confirmed against primary docs** from **[A] assumed**, and its final section lists plausible-sounding claims that could **not** be confirmed. Do not promote an assumption into a fact by writing it into code or docs.

---

## Non-negotiable rules

These are not style preferences. Breaking one produces a security hole, a silent correctness bug, or an unfixable latency problem.

1. **Nothing slow goes in the audio path.** `apps/voice-gateway` may not do a database query, a vector search, or a blocking log write while a call is live. If you think you need one, you need a cache, a pre-load, or a background task.
2. **`rn_voice.media` is permanently framework-free.** The audio transport layer — transport, codecs, ring buffer, played-ms accounting, VAD plumbing, barge-in mechanics — may never import LangChain, LangGraph, `rn_orchestration`, `rn_agent` or `rn_services`. This one does not relax for any benchmark. Higher layers are different: `rn_voice.runtime` **may** consult orchestration, subject to the gate in [ADR-009](docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md). LangChain/LangGraph is still only ever *written* in `packages/orchestration`. All enforced by `import-linter`.
3. **The model requests; the platform decides.** Tool arguments and RAG content are untrusted input. `organization_id`, `call_id` and `agent_version_id` are injected from server-side session context and **never** read from model output.
4. **Tenant isolation is a security boundary.** Every tenant-owned query is scoped by `organization_id`, derived from the verified token — never from a request body or a frontend-supplied value.
5. **Postgres is truth. Redis is coordination.** Nothing that matters may live only in Redis.
6. **Never invent a provider API.** If you are writing against Exotel, OpenAI Realtime, Sarvam, Clerk or Neon and the exact request/response shape matters, verify it against official docs (WebFetch) in that session. Model memory of provider APIs is stale and these APIs change — the realtime beta interface was removed outright in May 2026, so most tutorials and OSS examples are wrong.
7. **No secrets in the repository.** Add new configuration to `.env.example` with a placeholder. Never a real key, never in a test fixture, never in a log line.
8. **No real customer data in tests.** Test numbers must be internal and consented.
9. **Business logic never lives in a route handler.** Routes validate, authorize, delegate to `rn_services`, and serialize.
10. **Don't add a dependency without a reason** you can state in one sentence in the PR description.

---

## Layer map

Inside the voice gateway, the separation that matters:

```
rn_voice.media     ← MEDIA TRANSPORT. bytes, buffers, timings. framework-free. PERMANENT.
       ▲
rn_voice.session   ← REALTIME SESSION. provider session, turn lifecycle, tool dispatch.
       ▲
rn_voice.runtime   ← AGENT RUNTIME. may consult orchestration (gated — see ADR-009).
```

And across packages:

```
apps/api      apps/voice-gateway          apps/worker
    │                 │                        │
    │      (rn_voice.runtime, gated)           │
    └───────────► rn_orchestration ◄───────────┘   ← LangChain/LangGraph WRITTEN only here
                        │
                    rn_agent          ← definitions, tool registry, guardrails
                        │                (framework-free, on purpose)
                   rn_services        ← business use cases
                    ╱        ╲
        rn_persistence      rn_providers   ← every external system behind an interface
                    ╲        ╱
                    rn_domain          ← pure: entities, events, policies. no I/O.
                        │
                     rn_core           ← config, errors, IDs, time, logging, telemetry, redaction
```

`apps/worker` is `rn_orchestration`'s only caller today. `rn_voice.runtime` and `rn_api` are *permitted* to call it but do not — adding that is an ordinary change, gated by [ADR-009](docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md) if it lands inside a live turn.

Imports point **downward only**. Verify with `uv run lint-imports` — ten contracts, in the root `pyproject.toml`, executable rather than aspirational. They have been negative-tested: a deliberate violating import does break the build.

Note that the apps legitimately depend on `rn_persistence` and `rn_providers` directly (they are the composition roots that construct engines and adapters). What is forbidden is `rn_agent`/`rn_orchestration` reaching domain data outside `rn_services`, and the voice gateway holding a session of its own.

If a task seems to require breaking a contract, that is an architecture change: **write an ADR and raise it**, do not quietly edit the contract.

---

## Commands

```bash
# Setup (one time)
uv sync                          # Python workspace — installs all packages + dev tools
npm install                      # frontend workspace
cp .env.example .env             # then fill in real values (never commit .env)

# Local infrastructure
docker compose -f infrastructure/local/docker-compose.yml up -d    # Postgres+pgvector, Redis

# Run
uv run uvicorn rn_api.main:app --reload --port 8000                # control plane
uv run uvicorn rn_voice.main:app --port 8080                       # voice gateway
uv run taskiq worker rn_worker.broker:broker                       # workers
npm run dev                                                        # dashboard

# Checks — run the relevant ones before reporting a task complete
uv run ruff format .             # format
uv run ruff check . --fix        # lint
uv run mypy .                    # types
uv run lint-imports              # ARCHITECTURE BOUNDARIES — do not skip
uv run pytest                    # tests — live and load are excluded by default
uv run pytest -m unit            # fast subset
npm run lint && npm run typecheck && npm run build     # frontend
```

`pytest` markers: `unit` · `integration` (needs Postgres/Redis) · `provider` (mocked adapters) · `live` (**real paid APIs, dials real numbers**) · `agent_eval` · `load`.

`addopts` pins `-m 'not live and not load'`, so a bare `pytest` **cannot** spend money — you must opt in explicitly with `-m live`. Do not remove that filter.

```bash
# Migrations (always the DIRECT connection — a pooler cannot hold session state)
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic check                      # fails if a model changed without a revision
uv run alembic revision --autogenerate -m "what changed"
```

Note: nothing in `apps/` has an entrypoint yet. Those `uvicorn`/`taskiq` commands describe the intended shape; they will not work until the relevant phase is implemented.

**Integration tests need Docker but not a free port 5432.** They start an ephemeral PostgreSQL via testcontainers, so they never collide with — or worse, write to — a database you already run. Set `RN_TEST_DATABASE_URL` to reuse an existing one instead (CI does). The local compose stack takes `POSTGRES_HOST_PORT` if 5432 is taken.

---

## How to work here

1. Read `CLAUDE.md`, then the docs relevant to the task, then the existing code.
2. Understand the current architecture **before** proposing a change to it.
3. State a short plan for anything non-trivial.
4. Implement **only the requested scope.** Do not refactor adjacent code you happen to dislike.
5. Add or update tests.
6. Run the relevant checks above — actually run them, and report real output.
7. If you changed architecture or behaviour, **update the docs in the same change.** Stale docs are worse than no docs, because they are trusted.
8. Report: files changed · decisions made · tests run and their result · what is unresolved · the single next recommended task.

### Report honestly

If tests fail, say so and show the output. If you skipped a step, say which. If something is unverified, label it unverified. A confident wrong answer about a provider API costs hours of debugging; "I could not confirm this, here is what I'd check" costs nothing.

### Uncertainty policy

- **Reversible technical choice** (a library, a file layout, a helper's name): pick a sensible default, note it, move on.
- **Expensive or irreversible** (schema shape, tenancy model, provider commitment, anything touching compliance or cost): stop and mark it **`DECISION REQUIRED`**. See PRD §12 for the ones already open.

---

## Conventions

**Python** — 3.12, fully typed, `mypy --strict`. Async by default at I/O boundaries; never block the event loop. Pydantic for all external-boundary data. Errors are typed and structured, never bare strings. Comments explain *why*; the code already says *what*. Small modules, no god classes.

**API** — versioned under `/api/v1`. External schemas are separate from persistence models; never expose an ORM model as an API contract. Consistent pagination, filtering, sorting, error shape. Idempotency keys on anything with an external side effect.

**Database** — Alembic migrations for every schema change, reviewed for lock behaviour before it reaches production. UUID primary keys. Timezone-aware timestamps, always. Foreign keys and constraints, not application-level hope. JSONB only where the shape is genuinely open — core business data is normalised.

**Frontend** — Next.js App Router + TypeScript + Tailwind. Note that `apps/web/AGENTS.md` warns this Next.js major version has breaking changes; read the bundled docs in `node_modules/next/dist/docs/` before writing framework code. The backend is authoritative for authorization — the frontend never decides what a user may see.

**Naming** — an *agent definition* is configuration; an *agent session* is one live call. Do not blur them. See [docs/GLOSSARY.md](docs/GLOSSARY.md).

---

## Traps specific to this codebase

Each of these has already cost someone somewhere a day:

- **A tool's `permission` must already be in the frozen catalog.** `roles.permissions` is constrained by a CHECK built from a literal snapshot in migration `0001`, so a new value cannot be stored at all. `ToolRegistry` refuses the declaration at import for that reason. **Adding a tool permission is a migration** — the V1 tool set needs several (meetings, callbacks, messaging), so Phases 3, 9 and 10 each own one.
- **The Realtime tool schema is FLAT, and getting it wrong fails silently.** `{"type","name","description","parameters"}`, properties at the top level — *not* nested under a `function` key. `convert_to_openai_tool()` returns the nested shape; the session accepts it and the model then never calls the tool, which presents as "the agent won't use its tools". `rn_agent.tools.schema` builds it from Pydantic directly and a test asserts `"function"` is not a top-level key.
- **`ToolRuntime` is a parameter, not a field.** That is why `organization_id` cannot appear in a generated tool schema — it was never in the model whose schema is generated. Do not "improve" this by filtering fields out of the schema; a filter is something a future contributor can forget.
- **`` is the wrong boundary for Indic text.** Hindi and Telugu attach particles inside the word (`cheyyakandi`, `करना`), so `kandi` matches nothing and a pattern that reads correctly never fires — while *omitting* the boundary makes `ना` match inside `करना` and turns a genuine opt-out into a negated one. Use `(?!\w)` on the right and `(?:^|\s)` on the left. Devanagari combining marks are also outside `\w`: the virama in `असिस्टेंट` and the candrabindu in `हूँ` both are, so a `[\w]+` class fails on exactly the words it was written for.
- **A span attribute key containing `name` is dropped.** `rn_core.telemetry` filters it so customer names cannot reach a span, which also silently drops `tool_name`. Use `tool=` on a span; `tool_name=` is fine in a log line.
- **The freeze trigger must list every behaviour column.** A new column on `agent_versions` that `agent_versions_freeze` does not name is silently mutable after publication — a weakened guarantee introduced by adding a feature. Migration `0002` replaces the function for exactly this reason, and its `downgrade` restores the `0001` body from a frozen literal.
- **A `BEFORE UPDATE` trigger fires ahead of CHECK evaluation.** On a published `agent_versions` row the freeze trigger raises before any constraint is evaluated, so a test asserting a CHECK must use a draft row or it passes for the wrong reason.
- **Barge-in is three operations, not one.** Clear the telephony buffer, flush our own ring buffer, *and* truncate the model's belief about what was played — with an accurate played-milliseconds figure. Implement it as one function with one call site.
- **Outbound audio must be byte-aligned** to the telephony provider's chunk rules. Model deltas arrive at arbitrary sizes. Unaligned writes produce choppy audio that looks like a network problem.
- **Filtered vector search silently under-returns.** With an approximate index, the tenant filter is applied *after* the index scan, so a scoped query can return far fewer rows than `LIMIT` — no error, the agent just seems to have forgotten its knowledge base. Always go through the shared retrieval helper.
- **Telephony webhooks are unsigned and may never arrive.** Idempotent handlers plus a reconciliation job. Never let a webhook alone authorize something with a financial effect.
- **The pooled database connection cannot hold session state.** Transaction-mode pooling means `SET LOCAL` inside a transaction, and a separate direct connection for migrations, index builds and advisory locks.
- **Two schedulers means duplicate real phone calls.** The scheduler holds a leader lease. Never run more than one.
- **Auth org claims are nested and prefixed differently across token versions**, and the vendor's own SDK helper reads the wrong shape. Use our claim extractor; getting this wrong is an authorization bypass.
- **`Mapped[datetime]` defaults to a NAIVE column.** SQLAlchemy maps a bare `datetime` annotation to `TIMESTAMP WITHOUT TIME ZONE`. `Base.type_annotation_map` fixes this globally and a schema test enforces it — do not override the column type per-field and reintroduce a naive one.
- **Postgres truncates identifiers at 63 characters, silently.** A truncated constraint name cannot be dropped by the name a migration expects, which breaks `downgrade`. The naming convention also *prepends* `ck_<table>_`, so an explicit CHECK name must be the bare suffix or you get `ck_roles_ck_roles_...`.
- **`session.get()` bypasses tenant scoping.** It looks up by primary key alone and will happily return another tenant's row — from the identity map, without touching the database. Repositories use a filtered `SELECT` for exactly this reason.
- **A migration must never import a live application catalog.** Permission and enum values are frozen literal snapshots in the migration. An old migration whose meaning changes because today's code changed is not a migration.

---

## Never do these

- Hardcode RiseNext anywhere in platform code.
- Share conversation state between calls, or hold it in a module-level global.
- Let the model produce a price, an availability slot, an ID, or a permission.
- Put an orchestration framework, an ORM call, or a vector search in `rn_voice.media`.
- Reach for LangGraph inside a live turn without a measured latency figure and an ADR ([ADR-009](docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md) gate). Off the critical path it needs no permission.
- Freeze an embedding dimension or a vector partitioning scheme before the Phase 3 bake-off ([ADR-010](docs/DECISIONS/ADR-010-defer-vector-storage-layout.md), open decision D-8).
- Write per-frame audio events to Postgres or Redis.
- Trust an `organization_id` that came from a client or from model output.
- Launch campaign calls in a loop without the queue, the concurrency budget and the compliance gate.
- Commit a secret, log a full phone number, or export PII without a tenant authorization check.
- Claim a concurrency number that has not been load-tested.
- Delete or rewrite working architecture to make a task easier — ask instead.
