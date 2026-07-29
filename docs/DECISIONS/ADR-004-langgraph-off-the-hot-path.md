# ADR-004: LangChain and LangGraph are confined to a single package

- Status: **Accepted, amended in part by [ADR-009](ADR-009-orchestration-boundary-for-live-sessions.md)**
- Date: 2026-07-28 (amended 2026-07-29)
- Deciders: Platform architecture
- Supersedes / Superseded by: §"Decision" amended by ADR-009

> **AMENDMENT NOTICE — read before relying on this ADR.**
> This ADR originally decided two things at once: (a) the orchestration *framework* is written in exactly one package, and (b) that package is reachable **only** from `apps/worker`, so no live call can touch it.
>
> **(a) stands and is unchanged.** **(b) was too strong and has been replaced** by [ADR-009](ADR-009-orchestration-boundary-for-live-sessions.md). The invariant that actually matters is that the *media/audio transport* stays independent of the framework — not that a live session may never consult an orchestration layer. A future live session **may** use orchestration for higher-level stateful reasoning if it is benchmarked and justified.
>
> Everything below is still the reasoning that produced (a), and the constraints in Context are all still true. Where this document says orchestration is "unreachable from a live call", read ADR-009 instead.

> **Scope:** which package may import an orchestration framework, and what that forces on the tool registry.
> **Companions:** [../AGENT_ARCHITECTURE.md](../AGENT_ARCHITECTURE.md) §3, §8 · [ADR-001](ADR-001-modular-monolith-monorepo.md) (the layering this contract lives in) · [../research/PROVIDER_CONSTRAINTS.md](../research/PROVIDER_CONSTRAINTS.md) §1, §5, §6a, §7 · [../../pyproject.toml](../../pyproject.toml) (the contract itself).

## Context

`rn_agent` holds the agent definitions, the typed tool registry and the guardrails. `rn_voice` — the media plane — imports it, because a live call needs exactly those three things. So whatever `rn_agent` depends on is inside the media plane's dependency closure. That single sentence is the whole decision.

The verified facts that constrain it:

- **HC-19 [C]** — Realtime declares tools **flat**: `{"type":"function","name":...,"parameters":{...}}`, properties at the top level, **not** nested under a `function` key. `langchain_core.utils.function_calling.convert_to_openai_tool()` returns the **nested Chat-Completions shape** and will not work. This is [anti-fact #15](../research/PROVIDER_CONSTRAINTS.md) and it fails *silently*.
- **HC-37 [C]** — LangGraph issue #7259: `AsyncPostgresSaver` holds an instance-level `threading.Lock()` during async execution. Reported at 500 concurrent users: **199.9 req/s @ 1923 ms** versus **1295 req/s @ 88 ms** for a raw `psycopg_pool`. That is ~85% throughput lost to in-process lock contention, not database capacity. (Status of the linked PR #7269 is §6a-38 — **unverified**; do not size anything on the assumption it landed.)
- **HC-38 [C]** — `interrupt()` **restarts the entire node from the beginning on resume.** It does not resume from the interrupt line. Any side effect placed before an `interrupt()` re-executes.
- **HC-36 [C]** — `langchain` 1.3.x hard-pins `langgraph >=1.2.5,<1.3.0`. They move as one version train; you cannot bump one independently.
- **HC-39 [C]** — `langgraph-checkpoint-postgres` advises `LANGGRAPH_STRICT_MSGPACK=true` to prevent code execution from a compromised checkpoint DB — mandatory on shared multi-tenant Postgres.
- **`langchain-core` hard-depends on `langsmith`** even when tracing is off ([§4](../research/PROVIDER_CONSTRAINTS.md)). Whether LangGraph OSS emits any telemetry independent of `LANGSMITH_TRACING` is **§6a-43, unverified** — it needs a network-egress test in a sealed container before anyone claims otherwise. Putting a tracing-SaaS client into the dependency closure of every live call is exactly the accident that ends with Indian call transcripts leaving the country by default, against PRD **D-1**.
- **[Anti-fact #16]** — the widely-cited "LangGraph adds ~2 ms per node / 50–100 ms for complex workflows" is third-party blog content. **No official LangGraph latency benchmark exists.**
- **§6a-42, unverified** — whether a single compiled graph is safe to share across concurrent asyncio tasks is an open question (issue #4214 suggests problems with long-lived compiled graphs plus async checkpointers). Compile-once versus compile-per-request is unsettled.

Against that: the turn budget is 1.5 s p95 (**a target, unmeasured**), of which the model's own endpointing takes 300–800 ms, chunk accumulation takes 80–200 ms ([ADR-003](ADR-003-audio-transport-and-sample-rate.md)), and the RTT from `ap-south-1` to the nearest OpenAI edge is **unmeasured** (§6a-17).

## Options considered

| Option | Case for | Why it lost |
|---|---|---|
| **LangGraph as the conversation engine** — a graph per turn, a checkpointer per call, the whole conversation as state | One mental model for every LLM interaction. Persistence, resumability and tracing come free. It is the pattern the ecosystem's documentation assumes. | Spends an **unmeasured** amount of an **unmeasured** budget (anti-fact #16). The realtime model already holds conversation state server-side, so the graph state is a *second, divergent* copy of the truth. `AsyncPostgresSaver` caps at ~200 req/s per process on in-process lock contention (**HC-37**) — at 100 concurrent calls with multiple supersteps per turn, that ceiling is reached by the framework, not the database. Pulls `langchain-core` → `langsmith` into the media plane. |
| **LangGraph for turn orchestration, realtime audio outside it** — audio bridging stays raw, but each turn's decision/tool logic runs as supersteps | Seductive, and the option most likely to be re-proposed. It sounds like it preserves the hot path, since no audio frame touches the graph. | It does not preserve the hot path. The tool round trip — model emits function call → we validate, authorize, execute → `function_call_output` — **is** the latency-critical segment; it is the window in which the caller hears silence or a filler phrase. Adding an unmeasured framework cost there is the same bet with a nicer story. Worse, it puts `langchain-core` into `rn_voice`'s import closure, which destroys the one contract that makes the whole layering checkable, and §6a-42 means we do not even know whether one compiled graph is safe across ~100 concurrent asyncio tasks. |
| **No framework at all** — hand-write post-call analysis, evaluation and future HITL too | Zero framework risk. Zero version train. Total control. | Post-call work genuinely benefits from structured-output plumbing, retries, and — for approval and escalation flows we know we want — durable checkpointing and human-in-the-loop resumption at minutes-to-hours scale. Rebuilding that is real work with no product differentiation, in a plane where latency is irrelevant. Rejecting a framework where it is *free* is as unprincipled as adopting it where it is expensive. |
| **LangGraph only for non-realtime work** *(chosen)* | Puts the framework exactly where its costs are irrelevant (seconds-to-minutes) and its benefits are real. The cost boundary is machine-checkable. | Cost: the realtime turn state machine is ours to write and test, and the tool registry gives up LangChain's ergonomics. Accepted. |

## Decision

**Only `rn_orchestration` may import an orchestration framework.** This is contract *"LangChain/LangGraph is written only in rn_orchestration"* in [../../pyproject.toml](../../pyproject.toml).

> **Amended by [ADR-009](ADR-009-orchestration-boundary-for-live-sessions.md).** The original decision continued: "…and `rn_orchestration` is imported **only** by `apps/worker`." That clause is withdrawn. Who may *call* orchestration is now governed by ADR-009; this ADR governs only where the framework may be *written*.

The contract is now literally what its name says. Two things changed since this ADR was drafted, and both closed the gap between the sentence and the enforcement:

- **`langgraph-checkpoint-postgres` moved from `apps/worker` into `packages/orchestration`.** `apps/worker` now declares **no LangGraph dependency at all**; `rn_orchestration` owns its own checkpointer. The worker is a process that runs graphs, not a package that imports them.
- **`rn_worker` is now listed in the contract's `source_modules`,** alongside `rn_core`, `rn_domain`, `rn_persistence`, `rn_providers`, `rn_services`, `rn_agent`, `rn_voice` and `rn_api`. The rule used to read "only `rn_orchestration` — and also `rn_worker` in practice". It now reads *only `rn_orchestration`*, with nothing after the comma.

The forbidden list names the separate top-level distributions explicitly, because they are separate distributions and a partial list is a hole: `langchain`, `langchain_core`, `langchain_openai`, `langchain_protocol`, `langgraph`, `langgraph_sdk`, `langsmith`. Like the other forbidden contracts it runs with `allow_indirect_imports = true` — it is about who may *write* the import, not about what a resolved dependency tree contains.

> **Amended.** This ADR originally added a contract *"Live-call path never imports `rn_orchestration`"* (sources `rn_voice`, `rn_api`). **That contract has been removed.** It enforced the withdrawn clause above — a permanent ban on a live session ever consulting orchestration — which is precisely the restriction [ADR-009](ADR-009-orchestration-boundary-for-live-sessions.md) replaces with a narrower, genuinely permanent one on `rn_voice.media`.

`rn_orchestration` today does three things: **post-call structured analysis** (the PRD §6.7 fields, schema-constrained — dashboard analytics never parse free-form model text), the **evaluation runner**, and **future HITL / multi-agent graphs**. Today `apps/worker` is its only caller; ADR-009 defines what a live-session caller would have to demonstrate first.

### The `rn_agent` / `rn_orchestration` split

| | `rn_agent` (framework-free) | `rn_orchestration` (framework-bound) |
|---|---|---|
| Imported by | `rn_voice`, `rn_api`, `rn_orchestration` | `apps/worker` today; `rn_voice.runtime` / `rn_api` permitted subject to [ADR-009](ADR-009-orchestration-boundary-for-live-sessions.md) |
| Holds | agent definitions and version snapshots, the typed tool registry, guardrails, prompt assembly, the **flat** Realtime tool-spec export | post-call analysis graphs, evaluation runner, HITL graphs, and the LangChain **adapter** `to_langchain_tools(registry, enabled)` |
| Latency class | live call | seconds to minutes |

Note where the LangChain export lives: the adapter walks the registry and wraps it, **on the orchestration side**. The registry never learns that LangChain exists.

### Why the tool registry must be plain Pydantic

Four independent reasons, any one of which is sufficient:

1. **Import-linter forbids it**, and `rn_voice` imports `rn_agent`. A LangChain-based registry is not merely undesirable — it is unbuildable without an ADR reversing this one.
2. **`langchain-core` hard-depends on `langsmith`.** A tracing-SaaS client in every live call's dependency closure is a DPDP exposure (**D-1**) created by accident rather than by decision, and §6a-43 means we cannot currently prove the framework is silent.
3. **HC-19.** The shape Realtime needs is flat, and the obvious LangChain helper produces the wrong one. Building the flat spec from `Args.model_json_schema()` directly means the wrong function is not even importable from the Realtime path.
4. **Three consumers need three shapes** — flat for Realtime (`rn_voice`), nested Chat-Completions for the OpenAI-compatible Sarvam LLM on the cascaded path, and `StructuredTool` for LangGraph. The single source of truth must be the format none of them own. That is Pydantic.

> **Supersedes the research brief.** [PROVIDER_CONSTRAINTS §3, Seam 3](../research/PROVIDER_CONSTRAINTS.md) suggests declaring tools with LangChain's `@tool` plus a `args_schema` and exporting both ways. That would place `langchain-core` inside `rn_agent`. **We do not do that.** The registry is plain Pydantic; the LangChain wrapper lives in `rn_orchestration`.

### The `convert_to_openai_tool` trap (HC-19)

Realtime wants `{"type":"function","name":...,"description":...,"parameters":{...}}`. `convert_to_openai_tool()` returns `{"type":"function","function":{...}}`. If it is ever needed on a Chat-Completions path, the correct Realtime-shaped export would be `{"type":"function", **convert_to_openai_function(t, strict=True)}` — the *function* variant, not the *tool* variant. **We do not use either in `rn_agent`;** this paragraph exists so nobody "fixes" the hand-built exporter later by reaching for the convenient helper.

The failure mode is what makes this worth a paragraph in an ADR: the session may accept the payload, and then the model simply never calls the tool. The symptom presents as *"the agent won't use its tools"* or *"the agent invented a price"* — which reads as a prompt problem, and costs a day. Two defences: build the flat spec from the Pydantic schema directly, and **validate the spec at agent-version publish time** with a unit test asserting the top-level key set, so a malformed schema fails in the dashboard rather than on a live call.

### The checkpointer and the `interrupt()` hazard

- **`AsyncPostgresSaver` stays off any concurrent live-call-adjacent path** until **HC-37** is confirmed fixed (§6a-38). Live-adjacent graphs use `InMemorySaver`, flushed asynchronously to our own Postgres schema. `AsyncPostgresSaver` (3.1.0) is for post-call and HITL only, with `durability='async'` or `'exit'` — `'sync'` adds a database round trip per superstep. The `langgraph-checkpoint-postgres` distribution it comes from is declared by `packages/orchestration`, which owns the checkpointer; `apps/worker` does not declare it and does not import it.
- **`interrupt()` re-executes its whole node on resume (HC-38).** In a HITL graph that dials, that means **a duplicate outbound call to a real Indian phone number** — real cost, real compliance exposure under **HC-14**, and a caller annoyed twice. Three rules for anyone writing a graph here: side effects go **after** the `interrupt()`; if one must precede it, it goes behind a durable idempotency key checked in **Postgres** (not Redis, not graph state); and every node body that touches the outside world is reviewed on the assumption it will run more than once.
- `LANGGRAPH_STRICT_MSGPACK=true` in base config (**HC-39**); `thread_id = f"{tenant_id}:{campaign_id}:{call_sid}"`; Store namespaces prefixed by tenant.

### On latency, we refuse to assume

There is **no official LangGraph latency benchmark** (anti-fact #16). We are not claiming LangGraph is slow — we are refusing to spend an unmeasured fraction of an unmeasured budget on a framework whose value here is workflow structure, which a single live turn does not need. The price of admission for moving any graph closer to a call is a measurement in *our* environment at *our* target concurrency, reported in [../OBSERVABILITY.md](../OBSERVABILITY.md) terms — not a blog post.

## Consequences

**Positive.** The media plane's dependency closure is framework-free and auditable, and a LangChain upgrade cannot break a live call. The version train (**HC-36**) is confined to one leaf package — genuinely one, now that `apps/worker` declares no LangGraph dependency and the checkpointer lives in `packages/orchestration`. The decision is fully enforced rather than mostly enforced: `rn_worker` sits in the contract's source modules, the forbidden list names every top-level distribution in the train (`langchain`, `langchain_core`, `langchain_openai`, `langchain_protocol`, `langgraph`, `langgraph_sdk`, `langsmith`), and the ninth contract *"Live-call path never imports `rn_orchestration`"* blocks the boundary from the call side as well. There is no longer a gap between what this ADR asserts and what CI checks. The tool registry stays neutral, so the same tool definition serves Realtime, the Sarvam cascade and the evaluation harness — which is what makes the PRD's "exercise the full call flow without placing a paid phone call" requirement achievable.

**Negative, accepted.** We hand-write the realtime turn state machine, its retries and its structured-output handling; that is our code to test and maintain. We give up LangChain's tool ergonomics in `rn_agent`. And we maintain **two export paths from one registry**, which can drift — mitigated by a contract test asserting name, description and JSON-Schema parity across the flat and `StructuredTool` exports. There is also a standing temptation, roughly once per quarter, to "just use a small graph here"; this ADR is the answer, and the answer includes what evidence would change it.

**What this forces us to do.** Keep **both** import contracts in CI — the framework contract and *"Live-call path never imports `rn_orchestration`"* — and treat editing either as an architecture change requiring an ADR. Keep the LangGraph dependency declared in `packages/orchestration` alone; a `langgraph` line reappearing in `apps/worker`'s `pyproject.toml` is the regression to watch for, because it would not fail the contract but it would undo the reason the contract is clean. Validate tool specs at publish time. Set `LANGGRAPH_STRICT_MSGPACK` in base config, not per-instantiation. Run the sealed-container egress test (§6a-43) before claiming no data leaves the process. Review every `rn_orchestration` node for re-entrancy as a matter of course.

## Revisit when

Moving a graph nearer to a live call may be **proposed** only when **all three** of the following hold — not one, not two:

1. **HC-37 is confirmed fixed** in a released version (verify §6a-38 against the changelog and the issue, not a summary), and
2. **§6a-42 is settled** — a load test establishes whether one compiled graph is safe across concurrent asyncio tasks, and at what cost, and
3. **We have our own measured p50/p95 superstep latency** from `ap-south-1` at the V1 target concurrency, published in [../OBSERVABILITY.md](../OBSERVABILITY.md).

Even then, the only segment eligible for reconsideration is **tool dispatch** — never raw audio, which is permanently out of scope for any graph framework.

Separately: if `langchain` ever drops the hard `langsmith` dependency, reason (2) for the plain-Pydantic registry disappears and that sub-decision alone is worth re-examining. A new major LangGraph release is **not**, by itself, a trigger.
