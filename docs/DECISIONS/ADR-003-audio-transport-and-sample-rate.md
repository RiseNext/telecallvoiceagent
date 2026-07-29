# ADR-003: Per-agent sample rate with a transcoder at the telephony boundary

- Status: Accepted
- Date: 2026-07-28
- Deciders: Platform architecture
- Supersedes / Superseded by: none

> **Scope:** the audio wire format between telephony and the model — sample rate, chunking, resampling, and where each of those lives in the code.
> **Companions:** [ADR-002](ADR-002-bridge-topology-not-direct-sip.md) (why we hold the audio at all) · [../REALTIME_VOICE.md](../REALTIME_VOICE.md) (frame-by-frame implementation) · [../research/PROVIDER_CONSTRAINTS.md](../research/PROVIDER_CONSTRAINTS.md) §1–§2 and §7 · [../../PRD.md](../../PRD.md) §7 (latency target), §11 (known constraints).

## Context

This is the decision that most of the media plane's code shape falls out of. Four verified facts collide.

- **HC-1 [C]** — Exotel's AgentStream/Voicebot applet carries audio as **base64 strings inside JSON text frames**, never binary frames, and the codec is **raw/slin: s16le, mono, little-endian PCM — not G.711 mu-law.**
- **HC-3 [C]** — Exotel supports exactly **8000 / 16000 / 24000 Hz**, mono, s16le, selected **per call** via a query parameter on the Voicebot applet URL.
- **HC-4 [C]** — OpenAI Realtime GA accepts **`audio/pcm` at 24 kHz only**; the only other accepted formats are `audio/pcmu` / `audio/pcma` (G.711, inherently 8 kHz). Format is declared as an **object** — `session.audio.input.format = {"type":"audio/pcm","rate":24000}` — not the dead beta string enum (**HC-16**).
- **HC-2 [C]** — audio sent to Exotel must be a **multiple of 320 bytes, ≥ 3200 bytes, ≤ 100000 bytes**, while model output deltas arrive at **arbitrary sizes**.

The consequence is the single most important structural fact about this stack, and it contradicts almost every telephony-plus-realtime tutorial in existence:

> **The "G.711 passes straight through, no resampling needed" pattern is unavailable here.** It requires the telephony leg to speak G.711. Exotel speaks slin. This is [PROVIDER_CONSTRAINTS](../research/PROVIDER_CONSTRAINTS.md) anti-fact #2 — it appeared in our own first-pass research and would have produced a broken bridge. There is no third option: either Exotel runs at 24 kHz, or we resample.

Two related traps must be stated before any arithmetic below is trusted. **Anti-fact #1:** "3.2 KB = 100 ms of 8 kHz mono PCM" is arithmetically false — 8000 Hz × 2 bytes = 16,000 bytes/s, so 3200 bytes is **200 ms**. Treat the *byte* thresholds as authoritative and every millisecond figure as derived. And **§6a-4 is open**: we do not know whether Exotel's byte thresholds are absolute at all rates or scale with sample rate. Every latency number in this ADR assumes they are absolute, and is a **budget, not a measurement**.

Finally, the fallback path pulls the other way. **HC-23 [C]:** Sarvam STT accepts pcm_s16le at 8000 or 16000 Hz and **not** mulaw; Sarvam TTS can emit linear16 at 8000 Hz. At 8 kHz, the entire Sarvam cascade is pure passthrough.

## Options considered

The three legal configurations, with the derived arithmetic (`bytes_per_second = rate × 2`, s16le mono):

| Exotel rate | OpenAI leg | Sarvam leg | Byte rate | 320 B = | Smallest legal aligned emission |
|---|---|---|---|---|---|
| **8000** | resample 8↔24 both directions | **zero conversion** | 16 KB/s | 20 ms | 3200 B = **200 ms** |
| **16000** | resample 16↔24 both directions (2:3) | zero conversion (Sarvam's documented optimal rate) | 32 KB/s | 10 ms | 3200 B = **100 ms** |
| **24000** | **zero conversion** | downsample 24→16 for STT | 48 KB/s | 6.67 ms | 3840 B = **80 ms** (see §*alignment*) |

| Option | Case for | Why it lost |
|---|---|---|
| **Force 8 kHz everywhere** | Matches PSTN's real bandwidth exactly, so nothing is wasted. Cheapest CPU and bandwidth. Sarvam becomes pure passthrough in both directions — the fallback path gets simpler, not harder. | Puts a resampler on the **primary** path in both directions, and costs **200 ms** of minimum-chunk buffering before a single byte of agent audio can legally be sent — over 13% of the PRD's entire 1.5 s p95 turn target, spent on nothing. |
| **Force 24 kHz everywhere** | Zero conversion on the OpenAI path. Minimum-chunk buffering drops to 80 ms. Fewest moving parts *if OpenAI is the only provider we ever use*. | Makes the Sarvam cascade permanently worse: every fallback agent pays a 24→16 downsample — the expensive, quality-sensitive direction — on the inbound leg. Costs 3× bandwidth and 3× per-frame base64/JSON CPU even for agents that gain nothing from it. Bakes a *provider's* constraint into a *platform* constant. |
| **Force 16 kHz everywhere** | The compromise: 100 ms buffering, Sarvam's documented optimal rate, no rate is more than a 2:3 ratio away. | Loses on both fronts rather than winning on either — it resamples on the primary path *and* is not the cheapest. A compromise chosen to avoid making a decision. |
| **Per-agent sample rate resolved at dial time** *(chosen)* | The applet URL query parameter is already **per call** (**HC-3**), and at dial time we know which agent — and therefore which voice provider — is about to run. The information needed to choose correctly is available exactly when the choice must be made. | Cost: two code paths to test instead of one, and a per-agent field that ops can set wrongly. Accepted — the paths differ only in which `AudioTranscoder` implementation is resolved. |

There is deliberately **no audio-quality argument** in this table. The source is an 8 kHz PSTN call; 16 kHz and 24 kHz are Exotel upsampling and carry no additional information. The decision is purely about where conversion cost and buffering latency land.

## Decision

**Sample rate is a per-agent field, pinned to the agent version, resolved at dial time and written into the Voicebot applet URL.**

- **Default 24000 Hz for OpenAI-primary agents** — eliminates resampling on the primary path and cuts minimum-chunk buffering from 200 ms to 80 ms.
- **Default 8000 Hz for Sarvam-primary agents** — both cascade legs become pure passthrough (**HC-23**).
- **16000 Hz is available** as configuration for anyone who needs it; it is nobody's default.
- The negotiated rate is **read back from the `start` event's `media_format` where possible** rather than assumed, because the exact query-parameter name is **unverified** (§6a-2; anti-fact #9 — `?sample-rate=16000` was seen exactly once and is uncorroborated). Nothing works at a non-default rate until this is confirmed empirically.
- Because the rate is pinned to the agent version, every call record can answer "what rate served this call?" without inference.

### The 320-byte alignment rule, and the 24 kHz wrinkle

**HC-2** is absolute: multiples of 320 bytes, ≥ 3200, ≤ 100000. Model deltas arrive at arbitrary sizes, so **a pacing and alignment ring buffer is a required component, not an optimisation** — this is the most common integration failure mode in this class of system, and emitting deltas raw produces choppy audio that the whole team will initially misdiagnose as a network problem.

The internal quantum is **20 ms**, not 320 bytes: **320 B @ 8k, 640 B @ 16k, 960 B @ 24k**. The reason is accounting, not aesthetics. At 24 kHz, 320 bytes is 6.667 ms — not a whole number of milliseconds — and accumulating playback in units of 6.667 ms drifts. The value that drifts is `audio_end_ms`, which **HC-7** requires us to send truthfully in `conversation.item.truncate` on every barge-in. A wrong `audio_end_ms` silently corrupts the model's belief about what the caller heard, and it fails quietly. So at 24 kHz the alignment quantum is **960 bytes** (3 × 320 = exactly 20 ms) and the smallest legal emission is the smallest multiple of 960 that is ≥ 3200 — **3840 bytes = 80 ms**. At 8 kHz and 16 kHz, 320 bytes is already 20 ms and 10 ms, and 3200 bytes is already a whole number of milliseconds; no adjustment is needed.

The ring buffer drains at approximately realtime, keeping a deliberately shallow lead of 1–2 chunks in Exotel's buffer — deep buffering makes barge-in destroy audio we have already counted as played, which is the same correctness bug from the other direction.

### The asymmetric resampling quality requirement

> **Upsampling can be cheap. Downsampling cannot.** *(quality argument is [A] — reasoned, not measured)*

**8k → 24k** (inbound, OpenAI path) adds no information; any reasonable interpolator is acceptable. **24k → 8k / 24k → 16k** (outbound at low rates; inbound to Sarvam STT) requires a **proper anti-aliasing low-pass before decimation**. Naive decimation folds everything above the new Nyquist back into the audible band — at 8 kHz output, a 5 kHz component lands on 3 kHz, directly on top of speech.

This is not audiophile concern-trolling. Energy above 4 kHz in speech is concentrated in fricatives, sibilants and aspirated stops — exactly the contrasts that are **phonemic in Hindi and Telugu** (aspirated vs unaspirated; retroflex vs dental sit in the same band). Aliasing them does not sound like static; it sounds like the agent is mumbling, and it degrades the one thing the product is judged on.

**Use `soxr` (1.1.0, in the lock) with `numpy` (2.5.1) in both directions. Do not hand-roll a resampler and do not use naive decimation anywhere.** Quality preset is configuration; start high. Budget: **≤ 1 ms per 20 ms frame — a TARGET, unmeasured.** If exceeded, the cause is almost certainly Python-level per-frame overhead, not soxr.

### Where the transcoder lives

**One `AudioTranscoder` at the telephony-adapter boundary — `packages/providers/src/rn_providers/audio/`.** Never inside a provider client, never inside business logic, never inline in the audio pump. Two implementations behind one interface: `PassthroughTranscoder` (rate in == rate out; a no-op that still exists so the call site has no branch) and `PolyphaseTranscoder`.

It is resolved **at session open, not at build time**: the telephony adapter declares the negotiated rate, the voice adapter declares `accepted_input_formats` / `emitted_output_format`, and the bridge picks the pair. That is what makes a Sarvam-primary agent and an OpenAI-primary agent the same code path.

The **ring buffer, aligner, pacer and playback ledger stay in `rn_voice`** — they are bridge policy — but are **parameterised by a `ChunkPolicy` declared by the telephony adapter** (min bytes, max bytes, alignment, frame quantum), because those rules are provider-specific and Twilio or Plivo would declare different ones. The split is: *format conversion is a provider fact; pacing is our policy.*

## Consequences

**Positive.** The primary path has zero resampling and 80 ms of chunk buffering instead of 200 ms — a fifth of the whole turn target, recovered for free. The fallback path has zero resampling too. One transcoder interface, resolved at runtime, means adding a telephony or voice provider does not touch the bridge.

**Negative, accepted.** 24 kHz costs **3× bandwidth and 3× per-frame base64/JSON CPU**. Derived from the byte rates: ~48 KB/s per direction before base64's 4/3 inflation, so roughly 1 Mbit/s per call on the telephony leg alone, and on the order of 100 Mbit/s at the V1 target of 100 concurrent calls, plus a comparable amount on the model leg. That is a **computed estimate from the byte arithmetic, not a measurement** — but it is a real NIC and egress-cost consideration, not a rounding error. We also carry two rate paths in tests forever, and a 24 kHz agent that ever needs mid-call Sarvam fallback would pay the expensive downsample precisely when things are already going wrong (mid-call fallback is **not** a V1 flow, and this ADR is a reason to keep it that way).

**What this forces us to do.** A `ChunkPolicy` on the telephony seam from day one. A playback ledger reconciled against echoed `mark` events (**HC-9**), with divergence logged as a health metric. An anti-aliasing regression test in CI: synthesise a 5 kHz sine at 24 kHz, downsample to 8 kHz, assert energy at 3 kHz is below −40 dBFS — naive decimation fails it instantly. And an empirical test call to resolve §6a-2 (parameter name), §6a-3 (exact outbound JSON shape) and §6a-4 (whether byte thresholds scale) before anyone builds a latency estimate on the table above.

## Revisit when

- **§6a-4 resolves that Exotel's byte thresholds scale with sample rate.** Then 24 kHz loses its buffering advantage entirely, the choice collapses to pure CPU and bandwidth, and **8000 Hz becomes the default for every agent.** This is the single most likely trigger and it is a cheap experiment — run it early.
- **Mid-call provider fallback becomes a supported flow.** A 24 kHz primary agent falling back to Sarvam needs a 24→16 downsample at the worst possible moment; if that flow ships, re-argue the OpenAI default toward 16 kHz.
- **OpenAI Realtime accepts `audio/pcm` at a rate other than 24 kHz**, or Exotel gains G.711 or binary-frame support. Either would restore the passthrough pattern that anti-fact #2 currently rules out, and would change this decision outright.
- **Measured transcoder cost exceeds 1 ms per 20 ms frame at target concurrency**, or measured per-call CPU on the 24 kHz path makes the gateway's cost per concurrent call unacceptable. Both are budgets today; neither has been measured.
