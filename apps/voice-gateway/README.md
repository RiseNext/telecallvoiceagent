# voice-gateway — the media plane

The bridge between a telephony media socket and a realtime AI session. **The only component in this system with a hard latency budget.**

## Owns

- The inbound WebSocket endpoint the telephony provider connects to.
- Per-call session runtime: context resolution, provider session lifecycle, audio transcoding, the outbound pacing ring buffer, played-milliseconds accounting, barge-in, turn events.
- Realtime tool dispatch into `rn_services`.
- Call finalization through `rn_services` (state + outbox row, one transaction).

## Three layers, three different rules

```
rn_voice.media      MEDIA TRANSPORT    bytes, buffers, timings.  framework-free. PERMANENT.
      ▲
rn_voice.session    REALTIME SESSION   provider session, turn lifecycle, tool dispatch
      ▲
rn_voice.runtime    AGENT RUNTIME      agent definition, guardrails, turn decisions
                                       MAY consult orchestration — gated, not forbidden
```

**`rn_voice.media` is permanently walled off** from `langchain*`, `langgraph*`, `langsmith`, `rn_orchestration`, `rn_agent` and `rn_services`. This does not relax for any benchmark: a framework in the byte loop ends the transport's independent testability and replaceability, and that is a bigger loss than any latency number. A second contract enforces `runtime → session → media`, so the transport cannot be reached around.

**Above the transport, orchestration is permitted with evidence.** `rn_voice.runtime` may consult `rn_orchestration` for genuinely stateful work. Anything synchronous inside a live turn must first clear the [ADR-009](../../docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md) gate — measured latency, budget fit, a reason it cannot run off the critical path, a fallback, a per-agent flag, an ADR. Today `rn_voice` declares no dependency on `rn-orchestration`, because nothing needs one.

## What may not enter this process at all

No database session. No broker client. No vector search. No synchronous log or file I/O while a call is live. Each is enforced by an import-linter contract, not by good intentions.

If a feature seems to need one, the answer is a pre-loaded cache, a value resolved at dial time, or a background task — not an exception to the rule.

## Scaling shape

Stateless as a process, **stateful as a connection holder**. A live call is pinned to the instance holding its two sockets. Therefore:

- Scale by adding **containers**, never by adding uvicorn workers — forking would fragment connection ownership and capacity accounting.
- Autoscale on **active calls**, not CPU or RPS.
- Draining means "stop accepting new calls and let running ones finish". Calls can run for tens of minutes, so the graceful-shutdown window must exceed the maximum call duration.

## The three things that break first

1. **Barge-in treated as three separate operations.** It is one: clear the telephony buffer, flush our ring buffer and freeze `played_ms`, truncate the model with a truthful `audio_end_ms`. A wrong `audio_end_ms` corrupts the model's belief about what the caller heard, and it fails silently.
2. **Unaligned outbound audio.** The telephony provider requires byte-aligned chunks within a size range; model deltas arrive at arbitrary sizes. Writing them straight through produces choppy audio that looks like a network fault.
3. **Blocking work in the frame loop.** One await on something slow and every call on the instance stutters at once.

Full detail: [REALTIME_VOICE.md](../../docs/REALTIME_VOICE.md).
