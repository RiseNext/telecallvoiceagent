# ADR-005: Taskiq with Redis Streams for background jobs

- Status: Accepted
- Date: 2026-07-28
- Deciders: Platform architecture
- Supersedes / Superseded by: none

> **Scope:** the processing plane's job runtime — broker, ack semantics, retries, dead-letter, scheduling.
> **Companions:** [../ARCHITECTURE.md](../ARCHITECTURE.md) §6 (async processing) · [../SCALABILITY.md](../SCALABILITY.md) (worker autoscaling) · [../research/PROVIDER_CONSTRAINTS.md](../research/PROVIDER_CONSTRAINTS.md) (HC-34, HC-35, §6a-46/47/48, L-10) · [ADR-008-transactional-outbox-for-call-events.md](ADR-008-transactional-outbox-for-call-events.md) (how work *enters* the queue).

---

## Context

The processing plane runs five distinct workloads, and they have almost nothing in common except that none of them may run on the audio path:

| Workload | Shape | What a bug costs |
|---|---|---|
| Campaign dial dispatch | short, bursty, rate-limited | **a duplicate call to a real Indian phone number** |
| Post-call analysis | one LLM call, seconds, retryable | a missing summary |
| Outbox relay | continuous poll, tiny transactions | a lost or duplicated `call.completed` |
| Reconciliation | periodic, polls Exotel for calls stuck without a terminal event (HC-11) | calls stuck in `live` forever |
| Ingestion, embedding, exports | long, IO-bound, paid APIs | wasted spend on a retry storm |

Four facts constrain the choice, and they are verified, not preferences:

- **HC-34** — ARQ, the otherwise-obvious "small async Redis queue for FastAPI" pick, is in **maintenance-only mode by maintainer statement** (arq issue #510, 2025-10-18); its only 2026 releases are Python-compat bumps. It also has no dead-letter story and no OpenTelemetry story.
- **Celery has no native async execution.** Nothing in the 5.6 release notes or the Tasks guide supports `async def` tasks (anti-fact 20). Our services, provider adapters and SQLAlchemy sessions are async top to bottom; a sync worker means either an event loop per task or a rewrite of `rn_services`.
- **HC-35** — in `taskiq-redis`, the **PubSub and ListQueue brokers have no acknowledgement**. Only the Stream brokers do.
- **L-10** — Dramatiq is dual LGPL-3.0/GPL-3.0. This is a closed-source commercial platform, so adopting it is a legal question, not an engineering one.

One further constraint comes from our own rules rather than a provider: **Postgres is truth, Redis is coordination.** Whatever we choose, a job sitting in the broker is *not* a durable business fact. The durable fact is the `calls` row written before the dial and the `outbox` row written inside `finalize_call()` ([../DATA_MODEL.md](../DATA_MODEL.md) §8). The queue is how work moves, not where it lives.

---

## Options considered

| Option | Why it was genuinely on the table | Why it lost |
|---|---|---|
| **ARQ** | Minimal, asyncio-native, Redis-only, the idiomatic FastAPI answer; we would have picked it in 2024 | **HC-34: maintenance-only.** Betting the processing plane on an unmaintained queue is a slow-motion outage. No DLQ, no OTel instrumentation, no broker abstraction to migrate away through. |
| **Celery** | The most battle-tested Python queue by an enormous margin; every operational question already has an answer | No native async execution (anti-fact 20). Every task would need a sync bridge into an async codebase — either a per-task event loop or a duplicate sync service layer. That is a permanent tax on every job, paid to buy maturity we can get elsewhere. |
| **Dramatiq** | Genuinely good design, better defaults than Celery, real broker abstraction | **LGPL-3.0/GPL-3.0 (L-10).** Requires legal review before a commercial closed-source platform can ship it. We are not spending legal cycles to arrive at a queue that is otherwise a lateral move from Taskiq. Recorded here so the question is not reopened without new information. |
| **Taskiq** *(chosen)* | Natively asyncio, actively released (0.12.4, May 2026), OTel instrumentation built in since 0.12.0 and queue metrics since 0.12.3, permissive licence, real broker abstraction | No dead-letter queue — `SmartRetryMiddleware` only logs a warning on exhaustion. Smaller ecosystem. No published throughput benchmark (§6a-48). We accept all three; see Consequences. |
| **Temporal or Hatchet** | Real durable execution: our dial → answer → turn-loop → post-call → retry-tomorrow flow genuinely *is* a long-running workflow spanning minutes and external callbacks, which is exactly what a queue job is not | Real power at real operational cost — another stateful cluster to run, a new programming model, determinism constraints on workflow code, and a second failure domain in the critical path of placing phone calls. At four self-hosted container services (`api`, `voice-gateway`, `worker`, `scheduler` — five deployment units counting the Vercel-hosted dashboard, [ADR-001](ADR-001-modular-monolith-monorepo.md)) and zero implemented product code, this buys durability we can get from **an explicit state machine in Postgres driven by short jobs plus webhook events**. Documented as the escalation, not the start. |

Note what is *not* an argument here: throughput. We have measured nothing. No option was chosen or rejected on performance.

---

## Decision

**Taskiq 0.12.4 with `taskiq-redis` 1.2.3, using `RedisStreamBroker` exclusively, with `--ack-type when_executed`.**

Alongside it:

1. **`RedisAsyncResultBackend`** for results, **`SmartRetryMiddleware`** for backoff.
2. **A custom `TaskiqMiddleware` dead-letter implementation** writing exhausted jobs to a `dead_letter_jobs` table in Postgres (task name, args, kwargs, attempt count, last traceback, first- and last-seen timestamps). Budgeted at roughly a day of work.
3. **`TaskiqScheduler` running as exactly one replica**, holding a Postgres advisory-lock leader lease on a **direct** (non-pooled) connection, with `cron_offset='Asia/Kolkata'`.
4. **The broker client is owned by `rn_api` and `rn_worker` only** — enforced by the import-linter contract *"Job broker is owned by rn_api and rn_worker only"* in the root `pyproject.toml`. Everything else records intent in the outbox (ADR-008).

### Why only the Stream broker

`taskiq-redis` ships three brokers and two of them are disqualified by **HC-35**: PubSub and ListQueue **do not acknowledge**. A worker that pops a dial job and then dies takes the job with it — silently, with no redelivery and no error anywhere. For post-call analysis that is a missing summary. For campaign dispatch it is a contact that is never called, which on a compliance-gated outbound platform is indistinguishable from a bug we cannot reproduce.

`RedisStreamBroker` uses Redis Streams consumer groups, so an unacked message is redeliverable after its idle timeout. `--ack-type when_executed` acknowledges **after** the task body completes rather than on receipt. The trade is explicit: a crash mid-execution means **redelivery**, so every task must be idempotent. That is why dial jobs carry a deterministic `idempotency_key` derived from `(campaign_contact_id, attempt_number)` and why the pre-dial gate re-checks state before touching Exotel. At-least-once plus idempotency is the design; exactly-once is not on offer and pretending otherwise is how you dial someone twice.

Configuration rule: **the broker is constructed in exactly one module** (`rn_worker.broker`), and the ack type is not a per-deployment environment variable. Making it configurable makes it possible to get wrong in production.

### The dead-letter queue we have to build

Taskiq has no DLQ. `SmartRetryMiddleware` exhausts its retries and logs a warning — the job is then gone. For a platform where a failed job can mean "this lead was never called", a warning in a log stream is not an acceptable terminal state.

The middleware hooks `on_error`, and when the attempt count reaches the task's limit, writes a `dead_letter_jobs` row **before** letting the failure propagate. Requirements that make it useful rather than decorative:

- Row content must be sufficient to **replay** the job, not just to read about it.
- Arguments are written through the same redaction path as logs — phone numbers and caller PII are never stored in full in an ops table.
- `dead_letter_jobs` is platform-global (no `organization_id` NOT NULL) because some failures happen before tenant resolution, but the column exists and is populated whenever it is known.
- Depth and age of the table are alerting signals, not a dashboard nobody opens.

### The scheduler is a leader, and this is not a style preference

`TaskiqScheduler` must run as **one active instance**. Taskiq's own documentation is explicit about it, and the consequence here is worse than a duplicated report: the scheduler is what ticks campaign dispatch. **Two schedulers means two dial budgets computed from the same eligible contacts in the same tick, which means real Indian phone numbers ringing twice from the same campaign.** That is a compliance incident (HC-14), not a glitch.

Two mechanisms, both required:

- **Deployment:** the scheduler is its own ECS service with desired count 1 — one of the four container services, and the reason the count is four rather than three. It is the worker image with a different entrypoint, which is why it is a separate service without being a separate build.
- **Runtime:** it acquires a Postgres advisory lock at startup and holds it for its lifetime; without the lock it starts, logs, and idles. This survives the case a deployment count cannot cover — a rolling deploy briefly running two tasks, or a human scaling the service by hand.

The lock must be taken on a **direct, non-pooled connection**: Neon's PgBouncer runs `pool_mode=transaction`, where session-level advisory locks are unsupported (HC-26). A session-scoped lock taken through the pooler is either an error or, worse, a lock on a connection that gets handed to someone else.

---

## Consequences

**Positive**

- One async runtime end to end. A Taskiq task calls the same `rn_services` coroutine the API calls, with the same session lifecycle and the same provider adapters. No sync bridge, no duplicated service layer.
- OTel instrumentation via `TaskiqInstrumentor().instrument()` means a trace can follow `call.completed` from the outbox relay through analysis into the follow-up webhook without us writing correlation plumbing.
- The broker abstraction is a real exit. Task definitions are broker-agnostic; a migration is a change to `rn_worker.broker` plus operational work, not a rewrite of every job.

**Negative — accepted knowingly**

- **We are writing infrastructure the framework should ship.** The DLQ middleware is ours to build, test and maintain.
- **At-least-once delivery is now every consumer's problem.** Idempotency is a code-review checklist item on every task that has an external effect, forever.
- **Redis is in the path of work moving, and Redis is explicitly allowed to be lost.** A Redis failure loses in-flight jobs. The mitigation is not "make Redis durable" — it is that every durable intent already exists in Postgres (`calls` rows, `outbox` rows, `campaign_contacts` status), and the reconciliation job (mandatory anyway because of HC-11) plus outbox replay reconstruct the work. Redis loss is a delay, never a data loss. This must be tested, not assumed.
- **We have no throughput number.** No official `RedisStreamBroker` benchmark exists, and "proven in demanding production environments" is marketing prose (§6a-48). Campaign burst capacity is a **load-test deliverable**, not a known quantity.
- **Smaller ecosystem.** Fewer answers when something goes wrong at 2 a.m.

**What this forces us to do**

1. Every task with an external effect takes an idempotency key and checks it before acting. No exceptions for "this one is obviously safe".
2. The scheduler service is documented as `desiredCount: 1` with an alarm on any observation of two leader-lease holders.
3. Two DSNs in configuration from day one — pooled for tasks, direct for the advisory lock and for migrations (HC-26).
4. Queue depth, oldest-unacked age, redelivery count and `dead_letter_jobs` growth are first-class metrics with alerts ([../OBSERVABILITY.md](../OBSERVABILITY.md)).
5. A load test that establishes actual campaign burst capacity before any concurrency claim is made to a customer.

### Migration path

Taskiq's broker abstraction is the exit, and we chose it partly for that. The honest state of the options:

| Target | When it becomes attractive | What must be verified first |
|---|---|---|
| **SQS** | We want a managed broker with no Redis to operate | §6a-46: `taskiq-sqs` maturity, ack semantics, FIFO behaviour, visibility-timeout mapping — and specifically whether SQS's **15-minute maximum `DelaySeconds`** can express our campaign-retry deferrals, which are measured in hours. If it cannot, deferral moves into the Postgres state machine and SQS carries only immediate work. |
| **NATS JetStream** | We want lower latency and richer stream semantics | §6a-47: ack and redelivery parity is documented only for `taskiq-redis`. Must be established by test. |
| **Kafka** | Sustained multi-thousand msg/s, or we need event replay and log compaction | Same parity question, plus Kafka is a genuine operational commitment. |

None of these is a decision today. The point of recording them is that the migration is a broker swap plus verification, not an architectural change.

---

## Revisit when

Any one of these, individually, reopens this ADR:

1. **Taskiq's release cadence stalls for two consecutive quarters**, or the maintainer states maintenance-only — the exact condition that disqualified ARQ. This is checked at each dependency-bump review, not noticed by accident.
2. **A load test shows `RedisStreamBroker` cannot absorb a campaign burst** at our per-organization dial budget, after the token-bucket limiter for Exotel's 200 req/min `Calls/connect` cap (HC-13) is accounted for. Then the broker changes, not the job layer.
3. **The Postgres call-state machine becomes the dominant source of complexity** — measured concretely as: more than roughly a third of processing-plane bugs being state-transition or retry-orchestration bugs rather than business-logic bugs. That is the signal to escalate to Temporal or Hatchet, and it is the only signal we will accept for it.
4. **Data residency (PRD D-1) forces infrastructure out of the current setup** in a way that changes what "Redis" means for us. The broker decision is downstream of that ruling like everything else.
5. **`dead_letter_jobs` stops being rare.** If jobs are dead-lettering routinely, the problem is retry policy or an unhealthy dependency — but if the DLQ middleware itself becomes a maintenance burden, that is evidence the framework choice is costing more than it saves.
