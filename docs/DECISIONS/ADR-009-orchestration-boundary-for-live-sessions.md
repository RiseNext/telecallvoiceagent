# ADR-009: The orchestration boundary is the media transport, not the call

- Status: Accepted
- Date: 2026-07-29
- Deciders: Platform architecture
- Supersedes / Superseded by: **amends [ADR-004](ADR-004-langgraph-off-the-hot-path.md)** (withdraws its "reachable only from `apps/worker`" clause)

> **Scope:** where the orchestration framework may and may not run, and what a live agent session must demonstrate before it is allowed to consult one.
> **Companions:** [ADR-004](ADR-004-langgraph-off-the-hot-path.md) (where the framework is *written*) · [../REALTIME_VOICE.md](../REALTIME_VOICE.md) (the latency budget it must fit) · [../AGENT_ARCHITECTURE.md](../AGENT_ARCHITECTURE.md) · [../../pyproject.toml](../../pyproject.toml) (the contracts).

## Context

[ADR-004](ADR-004-langgraph-off-the-hot-path.md) decided two things at once and bound them together:

- **(a)** LangChain and LangGraph are *written* in exactly one package, `rn_orchestration`.
- **(b)** That package is *reachable* only from `apps/worker`, enforced by a contract forbidding `rn_voice` and `rn_api` from importing it at all.

(a) is well-founded and unchanged. (b) conflated two different things — a framework in the **audio transport** and a framework in the **call's decision-making** — and froze the second on the strength of arguments that only justify the first.

That over-reach has a real cost. The product roadmap explicitly anticipates stateful agent orchestration, controlled state transitions, recovery, and multi-agent workflows. A permanent prohibition on a live session reaching an orchestration layer forecloses all of that at the architecture level, before a single measurement exists. It would mean that the day a benchmark showed LangGraph was the right tool for, say, a multi-step objection-handling flow, the answer would be "the architecture forbids it" rather than "here is what it costs".

The genuine, non-negotiable requirement is narrower and sharper:

> **Raw realtime audio transport must remain independent of LangChain and LangGraph.** The framework must never process audio frames, WebSocket transport, buffering, VAD mechanics, playback mechanics, or interruption/barge-in transport.

Note that this invariant is **not primarily about latency.** Latency is the reason a *synchronous in-turn* graph call needs justifying. The reason the transport itself must stay framework-free is different and stronger: audio transport has to be independently testable with golden files, replaceable per telephony provider, and reasonable about at the byte level. A graph framework in that loop destroys all three, and no benchmark can buy them back.

The constraints from ADR-004's Context all still hold and still argue for caution *inside a turn*: no official LangGraph latency benchmark exists ([anti-fact #16](../research/PROVIDER_CONSTRAINTS.md)); `AsyncPostgresSaver` collapses under concurrency (**HC-37**); `interrupt()` re-executes its node from the start (**HC-38**); whether one compiled graph is safe across concurrent asyncio tasks is unverified (**§6a-42**); and the turn budget itself is a target, not a measurement.

## Options considered

| Option | Case for | Why it lost / won |
|---|---|---|
| **Keep ADR-004(b) — orchestration permanently unreachable from a live call** | Maximum safety. One bright line, trivially checkable. No risk of a framework creeping into a turn. | Forecloses a capability the product roadmap explicitly wants, on the basis of arguments that only justify protecting the *transport*. Bright lines drawn in the wrong place get worked around rather than respected: the first team that needs a stateful flow reimplements a worse graph inside `rn_voice` to stay compliant. **Rejected.** |
| **Drop the restriction entirely — any layer may use orchestration** | Simple. Maximum flexibility. Trust engineers to make latency calls. | Loses the invariant that actually matters. Nothing then stops a graph step appearing inside the frame loop, where the damage is structural rather than merely slow. **Rejected.** |
| **Move the boundary down to the media transport, gate everything above it** *(chosen)* | Puts the permanent, machine-checked line exactly where the permanent requirement is — `rn_voice.media`. Everything above it becomes an engineering decision with an evidence bar rather than an architectural prohibition. | Cost: the rule is now two rules (one structural, one procedural), and the procedural half depends on people honouring a gate. Mitigated by making the layering itself executable, so the gate is visible in the dependency graph rather than only in a document. |
| **Run orchestration as a separate service the gateway calls over the network** | Total process isolation; the gateway image stays lean. | A network hop inside a turn budget that already has an unmeasured intercontinental RTT in it. Reasonable *later*, if orchestration grows its own scaling profile — recorded as a revisit trigger, not chosen now. |

## Decision

**The permanent boundary is `rn_voice.media`, not the call.**

### 1. The layering, made executable

```
MEDIA TRANSPORT      rn_voice.media        bytes, buffers, timings. Framework-free. PERMANENT.
      ↓
REALTIME SESSION     rn_voice.session      provider session, turn lifecycle, tool dispatch
      ↓
AGENT RUNTIME        rn_voice.runtime      agent definition, instructions, guardrails, turn decisions
      ↓
OPTIONAL ORCHESTR.   rn_orchestration      invoked from runtime only, subject to the gate below
      ↓
TOOLS / SERVICES     rn_agent, rn_services business capability
```

Three contracts in [../../pyproject.toml](../../pyproject.toml) hold this together:

1. **"Media transport layer is framework-free and orchestration-free"** — `rn_voice.media` may not import `langchain*`, `langgraph*`, `langsmith`, `rn_orchestration`, `rn_agent` or `rn_services`. **This one does not relax.**
2. **"Voice gateway internal layering (runtime → session → media)"** — a layers contract. `media` knows nothing above it; `session` drives `media` through its interfaces; only `runtime` may reach an orchestration layer.
3. **"LangChain/LangGraph is written only in `rn_orchestration`"** — unchanged from ADR-004(a). Callers use `rn_orchestration`'s own interfaces; they do not grow graph code of their own.

The contract *"Live-call path never imports `rn_orchestration`"* introduced by ADR-004 is **removed**. `rn_voice.runtime` and `rn_api` may depend on `rn_orchestration`.

Both new contracts were negative-tested: adding `import langgraph`, `import rn_orchestration` and `import rn_voice.session` to `rn_voice.media` breaks three contracts, while `rn_voice.runtime` importing `rn_orchestration` passes.

### 2. What is permanently forbidden

LangChain/LangGraph must **never** be in the path of:

- audio frame decode/encode, resampling, or any per-frame work
- WebSocket media transport or its lifecycle
- the outbound pacing/alignment ring buffer
- played-millisecond accounting
- VAD event handling and endpointing mechanics
- interruption/barge-in transport — the clear/flush/truncate sequence

### 3. What is permitted, and what it costs

A live agent session **may** consult an orchestration layer for higher-level stateful work: workflow decisions, multi-step reasoning, tool orchestration, recovery, and complex conversation state. `rn_voice` does **not** declare a dependency on `rn-orchestration` today — nothing needs it yet, and the gateway image stays lean until something does. Adding that dependency is a normal engineering change, not an architecture violation.

**The gate.** Introducing orchestration *synchronously inside a live turn* requires all of:

| # | Requirement |
|---|---|
| 1 | A **measured** latency figure for the orchestration step under realistic concurrency — not an estimate, not a third-party blog figure ([anti-fact #16](../research/PROVIDER_CONSTRAINTS.md)). |
| 2 | That figure fits the documented turn budget ([REALTIME_VOICE.md](../REALTIME_VOICE.md)) **with the rest of the budget measured too**, including the India→provider RTT that is still unmeasured (§6a-17). |
| 3 | A statement of why the work cannot be done **off the critical path** — before the call, between turns, speculatively, or asynchronously with a filler utterance. Off-turn use needs no gate at all. |
| 4 | A **fallback path** that keeps the call working when orchestration is disabled, unavailable, or over its deadline. The graph is an enhancement, never the only way a turn can complete. |
| 5 | A **per-agent feature flag**, so it can be enabled for one tenant and rolled back without a deploy. |
| 6 | A checkpointer choice justified against **HC-37** — `AsyncPostgresSaver` is disqualified from any concurrent live path until that defect is confirmed fixed. |
| 7 | An **ADR** recording the measurement and the decision. |

Requirements 1–2 are the substance; the rest is what makes it reversible.

**Not gated:** orchestration in workers, post-call analysis, evaluation, HITL flows, and anything a call does *not* wait on.

## Consequences

**Positive.** The permanent rule is now stated where it is actually permanent, and is enforced on the module that actually needs protecting. The roadmap's stateful-orchestration ambitions are open rather than foreclosed. The five-layer separation is visible in the import graph instead of living only in prose. `rn_voice.media` gets a stronger guarantee than it had before — it is now walled off from the business layers too, not just from the framework.

**Negative.** Two rules where there was one, and the second depends on a gate being honoured rather than a contract failing the build. The `rn_voice` package now has internal structure that must be respected before there is any code in it — a small tax paid in advance for a boundary that is expensive to introduce later. And there is a real risk this ADR is read as "orchestration in live calls is fine now"; it is not — it is *permitted subject to evidence*, and requirement 3 means most candidate uses should end up off the critical path instead.

**What this forces us to do.** Phase 5 (realtime prototype) must land `rn_voice.media` as a genuinely standalone, golden-file-testable unit — if the transport cannot be tested without a session above it, the boundary exists only on paper. The latency instrumentation in [OBSERVABILITY.md](../OBSERVABILITY.md) becomes load-bearing rather than nice-to-have, because gate requirements 1–2 cannot be satisfied without it.

## Revisit when

- **A concrete flow wants in-turn orchestration.** Then this ADR is not revisited — it is *used*. Walk the gate and write the ADR it asks for.
- **The measured cost of a graph step turns out to be negligible** (say, consistently under ~20 ms at target concurrency, with a checkpointer that does not serialise). Then requirement 3 could be relaxed, though 4 and 5 should stay.
- **`rn_orchestration` grows its own scaling profile** — heavy memory, a distinct dependency train, or a different deploy cadence. Then reconsider the rejected "separate service" option: the boundary would already be in the right place to make that a deployment change rather than a refactor.
- **The media layer starts needing things from above it.** That is a signal the split is drawn in the wrong place. Move the responsibility down into an interface `media` is *handed*, rather than relaxing contract 1.
