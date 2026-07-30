# RiseNext Voice AI Platform

A multi-tenant platform for realtime AI voice agents that make and receive real phone calls in Indian languages — English, Hindi, Telugu, and the code-mixed speech people actually use.

The first agent, *Aira*, is RiseNext's own sales assistant. She is **a tenant configuration running on the platform**, not the product.

> **Status: Phase 1 complete.** The foundations are implemented and tested: `rn_core`, `rn_domain`, `rn_persistence` (21 tables, Alembic baseline, tenant-scoped repositories, Unit of Work) and the `rn_services` authorization seam. **Nothing above them exists yet** — no agent runtime, no telephony, no realtime voice, no job broker, no API endpoints, no frontend pages. 221 tests pass, including a 26-test cross-tenant security suite. See [docs/ROADMAP.md](docs/ROADMAP.md).

---

## Start here

| You want to know | Read |
|---|---|
| What we are building and what counts as done | [PRD.md](PRD.md) |
| How the system is structured, and why | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| What phase we are in and what is next | [docs/ROADMAP.md](docs/ROADMAP.md) |
| How to work in this repo (also read by Claude Code) | [CLAUDE.md](CLAUDE.md) |
| What we actually verified about our providers | [docs/research/PROVIDER_CONSTRAINTS.md](docs/research/PROVIDER_CONSTRAINTS.md) |
| Why a decision was made | [docs/DECISIONS/](docs/DECISIONS/) |

Also: [DATA_MODEL](docs/DATA_MODEL.md) · [AGENT_ARCHITECTURE](docs/AGENT_ARCHITECTURE.md) · [REALTIME_VOICE](docs/REALTIME_VOICE.md) · [SCALABILITY](docs/SCALABILITY.md) · [SECURITY](docs/SECURITY.md) · [COMPLIANCE](docs/COMPLIANCE.md) · [OBSERVABILITY](docs/OBSERVABILITY.md) · [TESTING](docs/TESTING.md) · [GLOSSARY](docs/GLOSSARY.md)

---

## Requirements

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | ≥ 0.8 | manages Python and the workspace; it will fetch Python 3.12 itself |
| Node.js | ≥ 20.9 (24 recommended) | frontend workspace |
| Docker | any recent | local Postgres + Redis |

---

## Setup

```bash
# Python workspace — installs all 10 members plus the dev toolchain
uv sync

# Frontend workspace
npm install

# Configuration
cp .env.example .env      # then fill in real values; .env is git-ignored

# Local infrastructure: Postgres 17 + pgvector, Redis 8
docker compose -f infrastructure/local/docker-compose.yml up -d
```

Verify the toolchain is healthy:

```bash
uv run ruff check .        # lint
uv run mypy .              # types
uv run lint-imports        # architecture boundaries
uv run pytest              # tests
```

---

## Running

Nothing has an entrypoint yet — these are the intended commands, added as each phase lands.

```bash
uv run uvicorn rn_api.main:app --reload --port 8000     # control plane
uv run uvicorn rn_voice.main:app --port 8080            # voice gateway
uv run taskiq worker rn_worker.broker:broker            # workers
npm run dev                                             # dashboard (localhost:3000)
```

The voice gateway must be publicly reachable over WSS for the telephony provider to connect to it. For local development use a tunnel and set `VOICE_GATEWAY_PUBLIC_WS_URL`.

---

## Layout

```text
apps/
  api/              rn_api      control plane — REST, webhooks, uploads
  voice-gateway/    rn_voice    media plane — the realtime audio bridge
  worker/           rn_worker   processing plane — jobs, scheduler, outbox relay
  web/              Next.js dashboard
packages/
  core/             rn_core           config, errors, IDs, time, logging, telemetry
  domain/           rn_domain         entities, events, policies — pure, no I/O
  persistence/      rn_persistence    SQLAlchemy, Alembic, repositories
  providers/        rn_providers      every external system, behind an interface
  services/         rn_services       business use cases
  agent/            rn_agent          agent definitions, tool registry, guardrails
  orchestration/    rn_orchestration  LangGraph — non-realtime work only
infrastructure/     Dockerfiles, local compose
docs/               architecture documentation and ADRs
tests/              cross-cutting: e2e, load, agent evaluation
```

Each package has a README stating what it owns and what may not enter it.

**Imports point downward only**, and the rule is executable — `uv run lint-imports` fails the build otherwise. Ten contracts, in the root `pyproject.toml`.

```
apps  →  rn_orchestration  →  rn_agent  →  rn_services  →  rn_persistence | rn_providers  →  rn_domain  →  rn_core
```

Inside the voice gateway there is a second, finer layering — `runtime → session → media` — and it carries the one rule worth memorising:

**`rn_voice.media` is permanently framework-free.** The audio transport layer may never import LangChain, LangGraph, `rn_orchestration`, `rn_agent` or `rn_services`. Layers *above* it may consult orchestration when a measurement justifies it. LangChain/LangGraph is only ever *written* in `rn_orchestration`. See [ADR-009](docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md).

---

## Testing

```bash
uv run pytest                      # everything except live and load
uv run pytest -m unit              # fast, no I/O, ~0.5s
uv run pytest -m integration       # real PostgreSQL, started automatically
```

**Integration tests need Docker but not a free port 5432.** They start an ephemeral PostgreSQL via testcontainers on a port Docker picks, so the suite never collides with — or writes to — a database you already run. Set `RN_TEST_DATABASE_URL` to point at an existing one instead; CI does exactly that with its service container.

If port 5432 is already taken on your machine, the local compose stack takes an override:

```bash
POSTGRES_HOST_PORT=5433 docker compose -f infrastructure/local/docker-compose.yml up -d
```

Markers: `unit` · `integration` · `provider` · `live` · `agent_eval` · `load`.

**`live` tests call real, paid provider APIs and dial real phone numbers.** `addopts` excludes them by default, so a bare `pytest` cannot spend money — opt in explicitly with `-m live`, and only ever against internal consented test numbers. Most development runs against provider fakes; see [docs/TESTING.md](docs/TESTING.md).

---

## Contributing

1. Read [CLAUDE.md](CLAUDE.md) — it applies to humans too.
2. Keep changes scoped to the task.
3. Run lint, types, `lint-imports` and tests before opening a PR.
4. If you changed architecture or behaviour, update the docs **in the same change**.
5. Never commit a secret. New configuration goes into `.env.example` with a placeholder.

Breaking a layering contract is an architecture change: write an ADR, do not edit the contract in passing.
