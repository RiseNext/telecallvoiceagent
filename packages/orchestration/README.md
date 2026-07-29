# rn-orchestration — LangGraph workflows

**The only package permitted to import LangChain or LangGraph.** Enforced by an import-linter contract listing every other package as forbidden.

## Owns

- **Post-call analysis** — the graph that turns a finished call into schema-constrained structured output: summary, interest, qualification, intent, requested services, languages, sentiment, objections, next action.
- **Evaluation** — the harness that replays scenario transcripts against an agent version and scores them.
- **Human-in-the-loop and multi-agent workflows** — future, but this is where they will live.

## The one rule

**`rn_voice.media` may never depend on this package.** The audio transport layer — transport, codecs, ring buffer, played-ms accounting, VAD plumbing, barge-in mechanics — is permanently walled off from it, and from LangChain, LangGraph, `rn_agent` and `rn_services` too. That contract does not relax: a graph framework in the byte loop ends the transport's independent testability and replaceability, which no latency measurement can give back.

**Everything above the transport is allowed to call this package.** `apps/worker` is the only caller today, and `rn_services`/`rn_agent` never will be (they sit below it). But `rn_voice.runtime` — the decision layer of a live call — **may**, for genuinely stateful work: workflow decisions, multi-step reasoning, tool orchestration, recovery, complex conversation state.

Anything *synchronous inside a live turn* has to earn it first: a measured latency figure at realistic concurrency, a fit inside the measured turn budget, a reason the work cannot run off the critical path, a working fallback, a per-agent flag, and an ADR. Off-turn and background use needs none of that. The gate is [ADR-009](../../docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md); the reasoning for keeping the framework in one package is [ADR-004](../../docs/DECISIONS/ADR-004-langgraph-off-the-hot-path.md).

LangGraph earns its place here without argument for post-call work: stateful, multi-step, recoverable and retryable, where a superstep costing tens of milliseconds is irrelevant against a minute of budget. Inside a turn it is not irrelevant, and no official latency benchmark exists — which is what the gate is for.

## Operational notes

- **Checkpointer choice matters.** The async Postgres saver has a known throughput defect under concurrency (an instance-level lock held during async execution). Use it only for post-call and HITL graphs, never anywhere concurrent with live calls.
- **`interrupt()` re-executes its node from the beginning on resume.** Any side effect placed before an interrupt runs twice. For this platform that would mean placing a duplicate real phone call. Side effects go after the interrupt, or behind an idempotency key.
- **Checkpoint payloads are a code-execution surface** on a shared multi-tenant database. Strict deserialization stays on.
- The LangChain and LangGraph versions move as a **single train** — `langchain` pins a narrow `langgraph` range. Bump them together or not at all.
