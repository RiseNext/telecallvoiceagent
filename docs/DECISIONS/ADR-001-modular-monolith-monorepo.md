# ADR-001: Modular monolith in a monorepo, with a separately scalable realtime service

- Status: Accepted
- Date: 2026-07-28
- Deciders: Platform architecture
- Supersedes / Superseded by: none

> **Scope:** repository shape, deployment-unit boundaries, and how those boundaries are enforced.
> **Companions:** [../ARCHITECTURE.md](../ARCHITECTURE.md) §2–§3 (structure) · [../../PRD.md](../../PRD.md) §7 (non-functional targets) · [../research/PROVIDER_CONSTRAINTS.md](../research/PROVIDER_CONSTRAINTS.md) (verified provider facts) · [../../pyproject.toml](../../pyproject.toml) (the executable contracts) · [ADR-004](ADR-004-langgraph-off-the-hot-path.md) (the contract that matters most).

## Context

The platform has to satisfy the PRD's design test — *fifty organizations, three hundred agents, a thousand concurrent calls* — while being built by a small team that has written **zero product code** so far. Two things force the repository shape:

**1. Exactly one component has a hard latency budget, and it is small.** The control plane targets ~200 ms p95 HTTP; the processing plane can take minutes; the media plane has ~20 ms of our own work per audio frame. Those are different engineering disciplines that must not share an event loop.

**2. The media plane is stateful in a way nothing else is.** A live call is pinned to the process holding its two WebSockets. Exotel caps a streaming session at 60 minutes and requires the bot to respond within ~10 s of connect with exactly one automatic handshake retry (**HC-5**); OpenAI caps a realtime session at 60 minutes on an independent clock (**HC-6**). So a voice-gateway deploy must drain for up to an hour, cold-start serverless is disqualified outright, and per-call CPU is real work — Exotel carries audio as base64 inside JSON *text* frames at roughly 10–20 messages/s/direction (**HC-1**), which is a JSON parse plus a base64 transcode per frame, per call.

Two further pressures argue for one repository rather than several. Provider reality moves underneath us — OpenAI removed the Realtime **beta** interface outright on 2026-05-12 (**HC-16**), invalidating most published examples — so cross-cutting changes that touch a provider adapter, a service, and an app in one commit are the normal case, not the exception. And a tool such as `get_service_pricing` must behave *identically* when invoked mid-call by the media plane and when replayed by an evaluation harness in the processing plane, which means the planes share **libraries**, not processes.

## Options considered

| Option | What it buys | Why it lost |
|---|---|---|
| **Microservices from day one** (campaigns, knowledge, contacts, analytics, calls each a service) | independent scaling and release per domain; hard blast-radius isolation | We do not yet know where the seams belong. Every boundary drawn now becomes a network call, a serialization format, a retry policy, a distributed trace and a partial-failure mode — paid immediately, for scaling we have not needed and cannot yet size (**HC-18**: no documented realtime concurrency limit; §6a-6: Exotel concurrency is a commercial unknown). Four container services with policed module seams is strictly cheaper to run and no harder to split later. |
| **Single deployable including the media path** (one FastAPI process serving dashboard API, webhooks, workers and call audio) | simplest possible ops; one image, one deploy, no inter-process anything | Fatal. A 50k-row CSV export, a slow analytics query, or a GC pause in an HTTP handler becomes audible jitter in every call on that process. Deploys become impossible to schedule: the API wants to ship several times a day, the gateway must drain for up to 60 minutes (**HC-5/HC-6**). And the autoscaler would have to serve two incompatible signals — RPS and concurrent calls — from one metric. |
| **Polyrepo** (separate repos per app and per shared library) | clean ownership boundaries; independent CI | Wrong problem for this team size. A single provider-shape change (see **HC-16**) becomes a chain of version bumps across four repos before it can be tested end to end. Shared libraries would need publishing and pinning, so the layering rules would stop being checkable in one pass — and the layering rules are the thing that keeps LangChain out of the media plane ([ADR-004](ADR-004-langgraph-off-the-hot-path.md)). |
| **Modular monolith + separate voice gateway** *(chosen)* | one atomic change set; one enforced layer graph; exactly the splits that a real operational signal demands | Cost: monorepo CI must be path-filtered or every push runs everything; a shared lock means a transitive security bump redeploys more than it strictly must. Accepted. |

## Decision

**One monorepo. Five deployment units — four self-hosted container services (`api`, `voice-gateway`, `worker`, `scheduler`) plus the Vercel-hosted dashboard. Seven layered Python packages whose boundaries are enforced by `import-linter` in CI.**

That phrasing is the house convention and it is used precisely throughout the docs: *five deployment units* counts the dashboard, *four container services* does not. Never mix the two without saying which you mean.

- **`uv` workspace for Python, `npm` workspaces for the frontend.** They coexist because they are good at different things and never need to interoperate — the only contract between them is the versioned HTTP API. The root `pyproject.toml` is `package = false`: it is not a distributable, it exists to declare workspace members (`apps/api`, `apps/voice-gateway`, `apps/worker`, `packages/*`), pin the shared dev toolchain, and own the repo-wide tool configuration. One `uv.lock` covers every Python member, which is how the whole tree resolves to one consistent set — including trains that must move together, such as `langchain` 1.3.14 pinning `langgraph >=1.2.5,<1.3` (**HC-36**). Deployment images install only what they need (`uv sync --package rn-voice`), so a single lock does not put LangChain inside the gateway image. `npm` workspaces own `apps/web` and nothing else.
- **No meta-build tool** (Nx, Bazel, Turborepo). The build graph is ~10 Python nodes and 1 JS node. A third build system would be a thing to learn, not a thing to gain. Revisit when CI time forces it.
- **The four container services** are `apps/api`, `apps/voice-gateway`, `apps/worker` and `scheduler` (the worker image with a different entrypoint, **exactly one active replica** holding a Postgres advisory-lock leader lease on a direct, non-pooled connection). **The fifth deployment unit** is `apps/web`, hosted on Vercel and therefore not a container we operate.

### Why `api` / `voice-gateway` is the one mandatory split

Every other boundary in this system is a module boundary we can turn into a process boundary later. This one cannot wait, for five independent reasons:

1. **Different scaling signal.** API autoscales on RPS; the gateway autoscales on *active calls*. One deployment cannot serve two metrics.
2. **Different resource profile.** The gateway is CPU-bound on per-frame base64/JSON (**HC-1**) and sensitive to event-loop latency. The API is I/O-bound on Postgres.
3. **Interference is not theoretical.** In a shared event loop, a 30 ms synchronous serialization in a request handler is a 30 ms gap in every concurrent call's audio.
4. **Different drain semantics.** The API drains in seconds. The gateway cannot move a live call between instances and must drain for up to an hour (**HC-5**, **HC-6**). Sharing a process means never shipping the API quickly.
5. **Different blast radius.** An OOM caused by a large export must not disconnect live calls.

The `worker` split is mandatory for a weaker but sufficient reason: post-call analysis is a multi-second LLM call running `rn_orchestration`'s LangGraph graphs (`apps/worker` runs them; it does not itself depend on LangGraph — [ADR-004](ADR-004-langgraph-off-the-hot-path.md)), and it must not share a process with either of the above.

Everything else — campaigns, knowledge, contacts, analytics — stays as **modules inside `apps/api`**, with their own service classes and no cross-module imports except through published interfaces.

### How `import-linter` makes the boundaries real

The layer graph in [../ARCHITECTURE.md](../ARCHITECTURE.md) §3 is not a diagram; it is **ten** executable contracts in [../../pyproject.toml](../../pyproject.toml), run by `uv run lint-imports` in CI:

| Contract | What it protects |
|---|---|
| Layered architecture (`rn_api\|rn_voice\|rn_worker` > `rn_orchestration` > `rn_agent` > `rn_services` > `rn_persistence\|rn_providers` > `rn_domain` > `rn_core`) | no upward imports, ever |
| Domain is pure | `rn_domain` cannot see SQLAlchemy, FastAPI, Redis, httpx, any vendor SDK |
| Media transport layer is framework-free and orchestration-free (source `rn_voice.media`) | **the permanent invariant** — the audio path cannot reach a framework, an orchestration layer or a business service ([ADR-009](ADR-009-orchestration-boundary-for-live-sessions.md)) |
| Voice gateway internal layering (`rn_voice.runtime` > `rn_voice.session` > `rn_voice.media`) | media knows nothing above it; only `runtime` may consult orchestration |
| LangChain/LangGraph is written only in `rn_orchestration` (sources include `rn_worker`) | framework code lives in one package; callers use its interfaces ([ADR-004](ADR-004-langgraph-off-the-hot-path.md)) |
| Vendor SDKs stay inside `rn_providers` | provider swap stays a one-package change |
| Agent layer reaches the DB only through `rn_services` | one authorization and tenancy chokepoint |
| Voice gateway holds no DB session of its own | a direct query in the gateway is a latency bug |
| HTTP framework stays in the app layer, never in a shared package | keeps FastAPI out of shared libraries (`rn_voice` legitimately uses it for its WebSocket endpoint) |
| Broker owned by `rn_api` and `rn_worker` only | forces the transactional outbox instead of a dual write |

The forbidden contracts run with `allow_indirect_imports = true`, because they are about **direct** imports: `rn_voice` → `rn_services` → `rn_persistence` → SQLAlchemy is the intended path, and forbidding it transitively would forbid the architecture. So these contracts constrain what a package may *write*, not what ends up in its image — see [ADR-008](ADR-008-transactional-outbox-for-call-events.md) for what that does and does not buy the gateway.

The point is **cheap extraction**. If `rn_services.campaigns` provably imports only downward and its callers reach it through a published interface, promoting it to its own deployable is packaging work with a known cost. Without the contracts, the same move starts with archaeology — and in every codebase that skipped this step, the answer to "can we extract this?" is no.

Changing a contract is an architecture change: write an ADR. Do not edit `pyproject.toml` in passing to make a task compile.

## Consequences

**Positive.** One commit can change a provider adapter, a service and an app together, and CI verifies the whole tree against it. New engineers get one `uv sync` and can run all three Python planes locally. The layering is checkable in seconds and fails the build, so it does not rot. The extraction path to services is pre-paid.

**Negative, accepted.** CI must be path-filtered or every push runs the full matrix. Two package managers means two lockfiles, two caches, two dependency-update configs. A shared lock means a transitive security bump touches more images than strictly necessary. Most importantly: **`import-linter` sees static imports only.** Runtime string imports, plugin loading and entry points escape it, and it checks *package* layers — it does **not** check coupling *between modules inside* `rn_services` or `rn_api`. That intra-package discipline is currently a code-review responsibility with no tooling behind it, and it is the most likely way this design degrades.

**What this forces us to do.** Path-filtered CI from the first pipeline. A container image per deployable that installs only its own workspace member. Graceful-shutdown handling in the gateway that waits for calls rather than a fixed grace period. A single scheduler replica with a leader lease, forever — two schedulers means duplicate real phone calls. And a review habit: every new cross-module import inside `apps/api` gets challenged, because nothing else will challenge it.

## Revisit when

- **A module inside `apps/api` acquires its own scaling signal or release cadence.** The concrete first candidate: knowledge ingestion (parse → chunk → embed) becoming CPU-heavy enough that it competes with HTTP request handling even after being moved to the worker. Extract that module; do not extract everything.
- **CI wall-clock for a one-line change exceeds ~15 minutes with path filtering already in place.** That is the trigger to evaluate a build-graph tool, not before.
- **We reach roughly 15 container services** (we run four today). That is also the trigger to re-evaluate ECS/Fargate versus Kubernetes ([../ARCHITECTURE.md](../ARCHITECTURE.md) §10) — the two questions should be answered together.
- **A second engineering team takes independent ownership of a domain** and is blocked by shared-repo release coupling.
- **PRD open decision [D-1](../../PRD.md#12-open-decisions) (data residency) forces region-partitioned data.** If transcripts and PII must stay in India, the split that matters may become regional rather than functional, and this ADR must be re-argued rather than patched.
