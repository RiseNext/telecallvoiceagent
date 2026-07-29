# ADR-002: Own the media bridge rather than connecting telephony directly to the model provider's SIP endpoint

- Status: Accepted
- Date: 2026-07-28
- Deciders: Platform architecture
- Supersedes / Superseded by: none

> **Scope:** the call topology — who terminates the caller's audio, and where the raw PCM is reachable.
> **Companions:** [../REALTIME_VOICE.md](../REALTIME_VOICE.md) (frame-by-frame detail) · [ADR-003](ADR-003-audio-transport-and-sample-rate.md) (what the tap costs us in transcoding) · [../research/PROVIDER_CONSTRAINTS.md](../research/PROVIDER_CONSTRAINTS.md) §1–§2 · [../COMPLIANCE.md](../COMPLIANCE.md) · [../../PRD.md](../../PRD.md) §12 (**D-1**, **D-5**).

## Context

There are two structurally different ways to put a speech-to-speech model on a phone call.

**(A) Bridge.** Telephony streams media to a socket we own; we forward it to the model and forward the model's audio back. We are in the middle of every frame.
**(B) Direct SIP.** Telephony hands the call to the model provider's SIP endpoint. The model provider terminates the media. We are out of the audio path entirely and see only signalling and post-hoc events.

(B) is genuinely attractive: it deletes the single most operationally demanding component we would otherwise have to build and run. It also deletes four capabilities the product depends on, and it collides with the one legal question the PRD flags as gating.

**The verified facts that decide this:**

- **HC-17 [C]** — OpenAI SIP media originates from `northeurope` / `southcentralus` / `eastus2` / `westus`. **No India, no Asia.** Under direct SIP, every Indian caller's raw voice is carried to Europe or the United States by the telephony provider, and we never hold it in India at any point. That collides head-on with PRD open decision **D-1** (*may recordings, transcripts and caller PII leave India?*), which is explicitly the decision every other infrastructure choice is downstream of.
- **HC-7 [C]** — on the WebSocket transport OpenAI does **not** auto-truncate on barge-in; the client must send `conversation.item.truncate` with a truthful `audio_end_ms`. Under direct SIP the provider owns interruption behaviour and we have neither the playback ledger nor the policy hook.
- **HC-8 / HC-9 [C]** — Exotel's `clear` only discards audio Exotel has buffered but not yet played, and Exotel's echoed `mark` events are the **only** ground truth for playback position. Both are bridge-side primitives. Without the bridge, neither is reachable.
- **HC-20 – HC-23 [C]** — the Sarvam cascaded fallback (STT → LLM → TTS) is a completely different mechanism: no interim transcripts, VAD-gated, 8/16 kHz PCM, its own idle timeouts. Substituting it per call requires that *we* hold the audio. Direct SIP makes the fallback path structurally impossible, not merely inconvenient.
- **HC-1 [C]** — Exotel's Voicebot applet is a WebSocket product carrying base64 PCM in JSON text frames. **UNVERIFIED / DECISION REQUIRED:** whether Exotel can originate a leg to an arbitrary external SIP URI at all is not confirmed anywhere in [PROVIDER_CONSTRAINTS](../research/PROVIDER_CONSTRAINTS.md); §6a-10 records that even *warm transfer to a human* is unconfirmed on this applet. Option (B) may therefore not be available on our telephony provider even if we wanted it.

The product requirements that need the tap: recording (**D-5**, unresolved — but the architecture must not foreclose it), the three-part atomic barge-in the PRD requires within ~200 ms, per-call provider fallback, per-call usage metering for the cost requirement, and the ability to run guardrails *before* committing to a spoken response.

## Options considered

| Option | What we gain | What we lose | Verdict |
|---|---|---|---|
| **(B) Direct SIP to the model provider** | No media fleet to build, run, scale or drain. No transcoder, no ring buffer, no playback ledger, no 320-byte alignment problem. Fewest moving parts by a wide margin, and the fastest path to a working call. | The raw-audio tap, and with it: **no recording** (forecloses D-5 rather than deferring it), **no custom barge-in policy** (HC-7/HC-8/HC-9 primitives are unreachable), **no per-call provider fallback** to the Sarvam cascade, no per-frame metering, no place to run a guardrail before audio is spoken. Media terminates outside India (**HC-17**) with no Indian option, which is the worst possible answer to **D-1**. Availability of SIP origination on Exotel is itself unverified. | **Rejected as the primary topology.** Retained as a documented degraded fast path — see below. |
| **A third-party managed voice-agent platform** | Fastest possible demo. Someone else owns the media plane, the barge-in tuning and the model integration. | We are building a *multi-tenant reselling platform*; PRD §2 names generic global voice-AI vendors as the competitor category we exist to beat on Indian-language quality and India telephony compliance. Reselling one makes our margin, our roadmap and our differentiation a vendor's feature request queue. Tenant isolation, the pre-dial compliance gate (**HC-14**), the "model requests, platform decides" tool-execution model and audit trail would all become vendor behaviour we cannot verify. Their pricing, concurrency and residency posture are **unverified** — we cannot even size the trade. | **Rejected.** |
| **(A) Our own WebSocket media bridge** *(chosen)* | We hold raw PCM in `ap-south-1`. Recording stays a switch, not a re-architecture. Barge-in is our policy, implemented once against the real primitives. Per-call fallback and per-call metering are possible. India-side PII control is at least *achievable* even though the model itself remains remote. | We own the hardest component in the system: a stateful, latency-sensitive, long-lived-connection fleet, plus a transcoder, a pacing ring buffer, a playback ledger, and session rollover across two independent 60-minute clocks (**HC-5**, **HC-6**). Plus per-frame CPU (**HC-1**) and one extra network hop. | **Chosen.** |

## Decision

**Topology A.** `Exotel Voicebot applet ⇄ our WebSocket media bridge (apps/voice-gateway, ap-south-1) ⇄ OpenAI Realtime WebSocket`. The bridge terminates the caller's media, holds the raw PCM, and is the only component that sees both legs.

**Direct SIP is retained as a documented degraded fast path — not implemented in V1.** It exists in this document so that nobody re-derives it from scratch under pressure, and so the telephony seam is not designed in a way that forecloses it. It is what we would reach for to smoke-test a new model version without touching bridge code, or to run a deliberately non-Indian-PII internal experiment. Using it in production requires **all** of the following, and they are non-negotiable:

1. **D-1 has been answered "media may leave India"** for the tenant in question, in writing.
2. The tenant is explicitly flagged; it is never a silent fallback and never a default.
3. It is understood that on that path there is **no recording, no Sarvam fallback, no custom barge-in policy, and no per-frame metering** — the call's capabilities are a strict subset, and the call record must say so.
4. Exotel SIP origination to an external URI has actually been verified to exist (§6a-10 — currently unknown).

Note what direct SIP is *not* good for: it is not a failover for our own bridge being down, because under topology A the telephony provider connects to *us*. Re-pointing an applet is a configuration change with human latency, not an automatic degradation.

## Consequences

**Positive.** The raw-audio tap is the platform's leverage point. Recording, custom barge-in, per-call provider substitution, per-call cost metering, live call state for the dashboard, and pre-speech guardrails all become possible *because* we sit in the middle. Caller audio and derived transcripts reach Indian infrastructure first, which is the strongest DPDP posture achievable while the model itself is remote — it does not answer **D-1**, but it makes a "yes, with conditions" answer implementable instead of impossible.

**Negative, accepted.** We now own, and must staff:

- A **stateful fleet**: long-lived containers on ECS/Fargate, autoscaled on active calls, draining for up to 60 minutes. Serverless is disqualified by cold starts against Exotel's ~10 s connect deadline and single handshake retry (**HC-5**).
- **Per-frame CPU**: JSON parse + base64 decode/encode at ~10–20 messages/s/direction/call (**HC-1**), plus transcoding ([ADR-003](ADR-003-audio-transport-and-sample-rate.md)).
- **Correctness we cannot buy**: the 320-byte alignment and pacing ring buffer (**HC-2**), the playback ledger feeding a truthful `audio_end_ms` (**HC-7**) corrected against echoed marks (**HC-9**), and barge-in as one atomic three-part operation (**HC-8**) with exactly one call site.
- **Session rollover** across two independent 60-minute clocks (**HC-5**, **HC-6**), plus the fact that **reconnection is not a documented feature of either provider** — so we persist conversation items as they stream and, on a drop, open a fresh session and replay condensed context. Do not architect around a resume primitive that does not exist.
- **One extra hop.** RTT from `ap-south-1` to the nearest OpenAI Realtime edge is **unmeasured** (§6a-17) and sits directly in the turn budget. We must not claim the bridge is latency-free; we can only observe that direct SIP would route media to Europe or the US anyway, so its latency advantage is unproven in either direction. Both numbers must be measured before the PRD's < 1.5 s p95 target ([../../PRD.md](../../PRD.md) §7) is treated as anything other than provisional.

**What this forces us to do.** Build the bridge to run in `ap-south-1` and keep it there. Keep the `TelephonyProvider` and `VoiceSession` seams honest so a future SIP path — or Twilio/Plivo — remains addable. Instrument the divergence between our estimated `played_ms` and the mark-derived truth as a first-class health metric; that divergence is the early warning for the highest-risk silent bug in the system. Record on every call which topology and which capability set served it.

## Revisit when

- **OpenAI (or the then-primary realtime provider) publishes a SIP media region in India or Asia** — the constraint recorded in **HC-17** changing is the single strongest reason to reopen this. Re-check the SIP guide's region list, not a blog post.
- **AND D-5 is answered "no recording in V1 or later"**, **AND** per-call provider fallback is formally dropped as a requirement. Direct SIP only becomes a real option when all three of the capabilities it deletes have been independently decided to be unnecessary. Any one of them surviving keeps the bridge.
- **A measured RTT comparison** (§6a-17) shows the bridge hop adding more than ~100 ms to the turn budget in `ap-south-1`. That would not by itself justify direct SIP, but it would justify re-examining bridge placement and provider edge selection as a separate decision.
- **Exotel's Voicebot applet gains a documented external-SIP or warm-transfer capability** (§6a-10) — that resolves the availability unknown and makes the comparison concrete rather than hypothetical.
