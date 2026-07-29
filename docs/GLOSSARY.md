# Glossary

> **Status:** Phase 0 — the platform is not implemented; these are the terms the design uses and the terms the code must use.
> **Source of truth for:** vocabulary. If a name here disagrees with a name in code, one of them is a bug — fix it, don't invent a synonym.
> **Companions:** [../PRD.md](../PRD.md) (what) · [ARCHITECTURE.md](ARCHITECTURE.md) (how) · [research/PROVIDER_CONSTRAINTS.md](research/PROVIDER_CONSTRAINTS.md) (what we actually verified) · [DECISIONS/](DECISIONS/) (why).
> Links below point at the document that owns each concept in depth. Where an entry and its owning document disagree, the owning document wins and this entry is the bug.

**Conventions used in entries:**
`[C]` verified against a primary provider source · `[A]` our assumption or our decision, not a provider fact · **TARGET** a number we chose, not a number we measured. We have measured nothing yet. Where a question is legal or commercial, the PRD open-decision ID (**D-1**..**D-7**) is given.

---

## The five distinctions that cause the most confusion

Get these five wrong and you will write a plausible-looking bug that nobody catches for a month.

### 1. Agent definition vs. agent session

An **agent definition** is *data* — versioned configuration rows in Postgres. An **agent session** is *a process's worth of memory* — one live call's isolated runtime. One definition serves many sessions concurrently; a session never mutates the definition and never shares mutable state with another session.

The failure mode: caching "the agent" in a module-level global that also accumulates conversation history. Two calls, one history, cross-caller data leak. If a variable holds anything that changes during a call, it belongs to the session, never to the definition.

### 2. Knowledge vs. authoritative data

**Knowledge** answers *"what do you do?"* — retrieved from a vector index, approximate, phrased by the model. **Authoritative data** answers *"what does it cost today?"*, *"is 4pm free?"*, *"what is this lead's ID?"* — fetched by a typed [tool](#tool) from a system of record, never phrased by the model.

The failure mode: putting the price list into the knowledge base. Retrieval will happily return a stale price with high confidence and the model will quote it. Prices, availability, IDs and permissions are tool results. This is a correctness requirement ([PRD §6.5](../PRD.md)), not a style preference.

### 3. Contact vs. lead vs. campaign contact

A **contact** is a person and a phone number owned by an organization. A **campaign contact** is *a membership row* — this contact, in that campaign, in a dial state. A **lead** is a *sales-qualified interest* created by an outcome, usually by the `create_lead` tool during a call.

The failure mode: adding `campaign_id` or `interest_level` to the contact. A contact can be in three campaigns and be two leads; collapsing them makes dedup, retry accounting and reporting all subtly wrong.

### 4. Control plane vs. media plane vs. processing plane

Three sets of rules, not three tiers of importance. **Control** = correctness and authorization, ordinary web latency. **Media** = the only hard realtime budget in the system; nothing enters it without a latency argument. **Processing** = anything expensive, retryable or analytical.

The failure mode: "it's just one small query" inside the voice gateway. There is no such thing on the media plane — see [ARCHITECTURE §1](ARCHITECTURE.md).

### 5. Redis as coordination vs. Postgres as truth

**Postgres holds every durable business fact.** **Redis holds only things we can afford to lose** — concurrency counters, rate-limit budgets, locks, idempotency keys, warm call-context handoff.

The failure mode: the only record of a completed call living in Redis for the few seconds before a worker picks it up. Losing Redis must degrade dispatch and slow lookups; it must never lose a call. This is why the [outbox](#outbox) exists.

---

## Terms

### Agent
Informal, ambiguous, and therefore **discouraged in code and schema names**. In prose it means "the AI persona on the call". In code, always say which of [agent definition](#agent-definition), [agent version](#agent-version) or [agent session](#agent-session) you mean.

### Agent Definition
The persistent, tenant-owned configuration of one AI persona: identity, role, [instruction layers](#instruction-layer), languages, voice map, [turn detection](#turn-detection) policy, enabled [tools](#tool), [knowledge base](#knowledge-base) bindings, [guardrails](#guardrail), and telephony parameters such as [sample rate](#sample-rate). Data, not a process; editable, and every edit produces a new [agent version](#agent-version).
*Owner:* [AGENT_ARCHITECTURE.md](AGENT_ARCHITECTURE.md).

### Agent Session
One live call's isolated runtime: conversation history, caller context, both WebSockets, the [realtime session](#realtime-session), [played-milliseconds](#played-milliseconds) accounting, tool-execution state, trace context. Created at call start, destroyed at call end, never shared, never global, never module-level.
*Not to be confused with:* [realtime session](#realtime-session) (the provider-side object it owns, which may be replaced mid-call by [session rollover](#session-rollover)).

### Agent Version
An immutable snapshot of an agent definition. Every call persists the exact `agent_version_id` that served it, so "which configuration handled this call?" is always answerable and changing a prompt never rewrites history. The voice gateway holds a local LRU cache of version *snapshots* — read-only, shared across concurrent sessions.
*Not to be confused with:* a draft edit. Only versions are dialled.

### Aira
RiseNext's own sales and customer-engagement assistant, and the first agent to run on the platform. **Aira is an agent definition belonging to the RiseNext tenant — she is not a component.** `aira.py`, `if org == "risenext"`, or an "Aira" table is an architecture violation ([CLAUDE.md](../CLAUDE.md)).

### Answer Rate
`answered calls ÷ dialled calls`, where *dialled* means we successfully handed the call to the telephony provider and *answered* means we received the provider's `answered` status event `[C]`. **Contacts blocked by the [compliance gate](#compliance-gate) are never dialled and are excluded from both numerator and denominator** — they are reported separately as suppressed. This definition is owned by this document; analytics code that computes it differently is the bug.

### Anti-fact
A claim that sounds true, is widely repeated, and could **not** be confirmed against a primary source. They are catalogued in [PROVIDER_CONSTRAINTS §7](research/PROVIDER_CONSTRAINTS.md). Restating one as fact in code or docs is treated as a defect, not a style nit — several of them (G.711 passthrough, `convert_to_openai_tool`, "3200 bytes = 100 ms") would each produce a silently broken bridge.

### Audio Frame
One unit of PCM audio as it crosses a wire: the payload of a single telephony `media` event, or a single `response.output_audio.delta` from the model. Exotel carries audio as **base64 inside JSON text frames, raw `s16le` mono PCM — not G.711, not binary frames** `[C]`, at roughly **50–100 ms of audio per message — about 10–20 messages per second per direction, per call** `[C]`. Any "50 events/second/direction" or "20 ms frames" figure is wrong; derived totals must be recomputed from 10–20/s.
*Not to be confused with:* [chunk](#chunk-audio) — a chunk is what we are *allowed to send*, a frame is what we *receive*. Model deltas arrive at arbitrary sizes and must be re-cut.

### Barge-in
The caller talking over the agent, and the agent stopping. **It is three operations that must live in one function with one call site** `[C]`: (1) send `clear` to Exotel, which only discards audio Exotel has buffered but not yet played; (2) flush our own [ring buffer](#ring-buffer); (3) tell the model what the caller actually heard via `conversation.item.truncate` with a truthful `audio_end_ms` — the WebSocket transport does **not** auto-truncate.
Budget: agent audio stops within ~200 ms of detected speech (**TARGET**, unmeasured).
*Owner:* [REALTIME_VOICE.md](REALTIME_VOICE.md). See also [played milliseconds](#played-milliseconds).

### Call
One telephone conversation, [inbound](#inbound-call) or [outbound](#outbound-call), identified internally by our own UUID and externally by the provider's call SID. The durable record of it survives the [agent session](#agent-session) that produced it. Inbound and outbound share one session runtime.

### Call Detail
Our per-call durable record and the dashboard view of it: status, timing, participants, [transcript](#transcript), [call outcome](#call-outcome), [tool executions](#tool-execution), [usage records](#usage-record), and the `agent_version_id` that served it.
*Not to be confused with:* Exotel's *Call Details* API resource, which is an external endpoint the [reconciliation](#reconciliation) job polls `[C]`. When both appear in one sentence, qualify them.

### Call Outcome
The single enumerated business result of a call — e.g. answered-and-qualified, not-interested, no-answer, busy, invalid-number, opted-out, failed. It is an enum written by the platform, derived from provider status plus [post-call analysis](#post-call-analysis).
*Not to be confused with:* the provider's call status (a transport fact) or the [structured output](#structured-output) block (much richer, model-produced, schema-constrained).

### Calling Window
The permitted local-time window for outbound dialling, evaluated per contact in IST first. **The exact permitted hours are UNVERIFIED** — two different windows appear in secondary sources and no Exotel page reviewed states one. It must be tenant/campaign **configuration, never a hardcoded constant**. See **D-4**.

### Campaign
A tenant-owned outbound dialling programme: an [agent version](#agent-version), a contact list, a schedule, a retry policy, concurrency limits and compliance settings. Campaigns are dispatched by a budgeted scheduler tick, **never** by a loop over contacts.
*Owner:* [ARCHITECTURE §6.5](ARCHITECTURE.md).

### Campaign Contact
The membership row joining a [contact](#contact) to a [campaign](#campaign), carrying per-campaign dial state: attempt count, next-attempt time, suppression reason, last [call](#call), terminal state. This — not the contact — is what the dispatcher iterates and what retry policy mutates.

### Cascaded Pipeline
The fallback voice path: STT → LLM → TTS as three separate providers (Sarvam STT/TTS), versus the primary **speech-to-speech** path where one model consumes and produces audio. The two are not behaviourally equivalent: the cascade emits no [interim transcripts](#interim-transcript) `[C]`, owns [turn detection](#turn-detection) differently, and has no model-side context to truncate on [barge-in](#barge-in). Callers branch on `SessionCapabilities`; pretending the paths are uniform produces a bridge that fails silently on fallback.

### Chunk (audio)
An outbound audio payload sized to the telephony provider's rules: **a multiple of 320 bytes, ≥ 3200 and ≤ 100000 bytes** `[C]`. The **byte** thresholds are authoritative; the millisecond gloss you will see repeated is unreliable (it is an [anti-fact](#anti-fact)), and whether the thresholds scale with [sample rate](#sample-rate) is an open provider question.
Our alignment quantum is rate-dependent `[A]`: at **24 kHz it is 960 bytes**, not 320, because 320 B at 24 kHz is 6.667 ms and accumulating [played milliseconds](#played-milliseconds) in 6.667 ms units drifts `audio_end_ms` and silently corrupts [barge-in](#barge-in) truncation. The minimum legal chunk follows from the quantum: at 24 kHz the smallest multiple of 960 that is ≥ 3200 is **3840 bytes = 1920 samples = 80 ms**; at 8 kHz it is **3200 bytes = 1600 samples = 200 ms**. "3200 B = 66.7 ms at 24 kHz" is wrong. Producing chunks is the [ring buffer](#ring-buffer)'s job.
*Not to be confused with:* [knowledge chunk](#knowledge-chunk). Never write a bare `chunk` variable in code that touches both.

### Code-switching
Changing language *within a single utterance* — "Website toh already hai, social media management chahiye." It is the normal way our callers speak, not an edge case, and no provider publishes a benchmark for it. **Neither OpenAI nor Sarvam documents Hinglish/Telugu-English code-switching quality; there is no official speech-to-speech language list for the realtime model at all** `[C]`. Language support must be established by our own evaluation on real Indian telephony audio. See **D-2**.
*Not to be confused with:* multilingual support (handling three languages) or translation (a different model entirely).

### Compliance Gate
The mandatory pre-dial check every candidate contact passes before a dial job is enqueued: [consent record](#consent-record) present, call classified transactional vs promotional, [DND/NCPR](#dnd--ncpr) status, [calling window](#calling-window), [opt-out](#opt-out) list, retry policy, duplicate guard. It is code in the dispatch path, not a report run afterwards, and a blocked contact never becomes a [call](#call).
*Owner:* [COMPLIANCE.md](COMPLIANCE.md).

### Consent Record
The first-class persisted artifact proving a contact opted in: source, timestamp, and the evidence itself. Exotel contractually requires producing opt-in evidence **within 24 hours** on any violation `[C]`, so retrievability is a schema requirement — and the reason `consent_records` stores **both** `phone_hash` (deterministic, for lookup) **and** `phone_e164` in plaintext: a hash cannot be shown to a regulator. Protected by storage-level encryption, not application-level column encryption. Lookup must work **without** tenant context, so there is a hash-first index `(phone_hash, captured_at DESC)` alongside any tenant-scoped one. *What artifact counts, how long it is retained, and who is liable when a tenant uploads a non-consented list are unresolved* — see **D-3**.

### Contact
A person and a phone number owned by one organization, E.164-normalised, deduplicated by policy. The stable identity that campaigns, calls, leads and opt-outs all reference.
*Not to be confused with:* [campaign contact](#campaign-contact) or [lead](#lead). See [distinction 3](#3-contact-vs-lead-vs-campaign-contact).

### Control Plane
`apps/api` + `apps/web`. Configuration, authorization, dashboards, uploads, exports, webhook receipt. Ordinary web-application engineering; correctness and authorization first; ~200 ms p95 HTTP (**TARGET**).

### Conversation Turn
One caller utterance and the agent response it produced, with its timings. Turns are the unit of latency measurement, of transcript structure and of evaluation.
*Not to be confused with:* a [transcript segment](#transcript-segment) (may be several per turn) or a model "response" (one turn may involve several model responses if a [tool](#tool) runs mid-turn).

### Dead Letter
A job that exhausted its retries and was written to the `dead_letter_jobs` table in Postgres by our own Taskiq middleware. **Taskiq has no dead-letter queue** — its retry middleware only logs a warning on exhaustion `[C]` — so this table is a component we build, and a non-empty one is an operational alert, not a log line.

### DND / NCPR
India's Do-Not-Disturb registry / National Customer Preference Register. Verified: calls to registered numbers must be **transactional** *and* the number must be whitelisted by an inbound contact within the last six months; enabling DND calling requires filing opt-in evidence `[C]`. **Whether Exotel scrubs DND/NCPR server-side or it is entirely our responsibility is UNVERIFIED** — assume ours until confirmed. See **D-4**.
*Not to be confused with:* [opt-out](#opt-out), which is our own per-tenant suppression list. A number can be clean on NCPR and still opted out of a specific tenant's calls.

### E.164
The international phone-number format we normalise every number into on import, using `phonenumbers`. Import shows a **preview of what will be rejected and why** before anything is committed; unparseable rows are surfaced, never silently dropped.

### Embedding
A vector representation of text produced by the [embedding provider](#provider-seam). **The production model, width and column type are NOT chosen** — they are open decision **D-8**, resolved in Phase 3 by a bake-off on real Indic and code-mixed data ([ADR-010](DECISIONS/ADR-010-defer-vector-storage-layout.md)). Any document quoting `text-embedding-3-small` at 1536 dimensions or `halfvec(1536)` as settled is stale. The width becomes part of the Postgres column type once chosen, so changing it later is a full re-embed plus a table rewrite; every row therefore records its own `embedding_model` and `embedding_dim` from the first migration.

### Endpointing
Deciding that the caller has *finished speaking*, so a response may begin. It is the single largest tunable slice of [turn latency](#turn-latency) and the parameter most likely to break Indian-language conversations — deliberative phrasing and [code-switching](#code-switching) get cut off by aggressive settings.
*Not to be confused with:* [VAD](#vad) (detects speech presence) or [turn detection](#turn-detection) (the whole policy). Endpointing is one decision inside turn detection.

### Guardrail
A deterministic, framework-free check in `rn_agent` that constrains what an agent may say or do, independent of prompt phrasing: AI self-disclosure, never claiming to be human, honouring [opt-out](#opt-out) language, refusing out-of-scope requests, and the origination rule — **the model may echo back an opaque identifier the platform issued during this call, but may never originate an ID, a price, an availability slot, a discount or a permission.** Guardrails are asserted in tests. A rule that exists only in an instruction string is not a guardrail.

### Idempotency Key
A caller-supplied or platform-generated key that makes an operation with an external side effect safe to retry exactly once — dialling, sending a WhatsApp message, booking a meeting. Mandatory on every such operation, because provider webhooks may be redelivered `[C]`, [outbox](#outbox) relays deliver at-least-once, and a resumed workflow node may re-execute its side effects `[C]`. Duplicate protection here means a real phone does not ring twice.

### Inbound Call
A call the caller initiates to one of the tenant's numbers. Shares the [agent session](#agent-session) runtime with outbound, but has no [campaign](#campaign), no [compliance gate](#compliance-gate) (the caller chose to call), and resolves its agent from the dialled number rather than from a dial job.

### Instruction Layer
One slice of the composed system instruction sent to the model at session open. Layers are assembled in fixed precedence — platform (AI disclosure, safety, anti-injection), organization, agent definition, then per-call context (caller name, campaign, language hints) — and the composed result is versioned with the [agent version](#agent-version). Layering exists so a platform-level rule cannot be overwritten by a tenant's prompt text.
*Not to be confused with:* [guardrails](#guardrail), which are enforced in code and do not depend on the model reading them.

### Interim Transcript
A partial, non-final transcription emitted while the caller is still speaking. **A capability, not a guarantee: OpenAI streams them; Sarvam's STT WebSocket emits nothing until VAD end-of-speech** `[C]`. Exposed as `SessionCapabilities.supports_interim`; any turn-taking logic that assumes partials must have a VAD-only code path. Never fabricate partials to make the seam look uniform.

### Knowledge Base
A tenant-owned collection of ingested documents, assignable per agent, retrieved through [RAG](#rag). Answers *"what do you do?"*. It is explicitly **not** the source for prices, availability or identifiers — see [distinction 2](#2-knowledge-vs-authoritative-data).
*Owner:* [DATA_MODEL.md](DATA_MODEL.md).

### Knowledge Chunk
One retrievable unit of a knowledge base: text plus metadata plus its [embedding](#embedding), always carrying `organization_id`. Retrieval goes through a single shared helper because **filtered approximate vector search silently under-returns** — with an ANN index the tenant filter is applied *after* the index scan, so a scoped query can return far fewer rows than `LIMIT` with no error `[C]`. The symptom is "the agent forgot our knowledge base", not an exception.

### Lead
A qualified sales interest created from a call — usually by the `create_lead` tool — carrying interest level, requirements, budget and timeline, and owned by the organization. One [contact](#contact) may become several leads over time.

### Leader Lease
The Postgres advisory lock that makes the scheduler a single active instance. It must be held on a **direct, non-pooled** connection, because transaction-mode pooling does not support advisory locks or session-level state `[C]`. **Two schedulers means duplicate dial jobs and duplicate real phone calls** — this lease is a safety mechanism, not an availability optimisation.

### Mark Event
The telephony provider's echo confirming that a specific outbound audio chunk has *actually finished playing* to the caller: we send a `mark` after a chunk, Exotel echoes it on playout completion `[C]`. **This is the only ground-truth playback position available to us**, and it is what corrects the [played-milliseconds](#played-milliseconds) estimate. Divergence between estimate and mark is a health metric.

### Media Plane
`apps/voice-gateway`. The only latency-critical component in the system: ~20 ms of our own work per [audio frame](#audio-frame) (**BUDGET**), sub-second [turn latency](#turn-latency) (**TARGET**). Forbidden inside it: any database query, vector search, LangGraph step, synchronous log write, or blocking I/O other than the two WebSockets. It is the one component we cannot fix by scaling out later.
*Not to be confused with:* the [processing plane](#processing-plane). "It's only called once per call" is not an argument for putting work here; put it in the outbox and let a worker do it.

### Membership
The row joining a user to an [organization](#organization-tenant) with a role and a permission set. Our membership table is authoritative for backend authorization: **Clerk's built-in system permissions never reach the backend**, so all authorization is built on our custom `org:<feature>:<action>` permissions `[C]`. Note the role catalogue is capped without a paid add-on — see **D-7**.

### Opt-out
A durable, tenant-scoped record that a contact asked not to be called — "remove my number", "don't call me again", "stop" — recognised in any supported language, honoured immediately mid-call, and enforced by the [compliance gate](#compliance-gate) across every future campaign. Opt-out is a product requirement written on the caller's behalf, not a compliance checkbox, and it is verified by test.
It is written two ways, deliberately: the model calls the **`record_opt_out`** [tool](#tool), and a code-side [guardrail](#guardrail) matcher fires as a belt-and-braces second path even when the model does not. The durable write lands in `suppressions`, keyed `(organization_id NULLABLE, phone_hash)` — a peppered deterministic hash, **no plaintext number**, because a blocklist should not also be a phone-number database. A NULL `organization_id` is a platform-wide suppression.
*Not to be confused with:* [DND/NCPR](#dnd--ncpr) (a national registry), campaign suppression (temporary), or `mark_not_interested` — which is a **sales-interest signal**, not a suppression write. Conflating the two is how a "not interested" lead quietly keeps getting dialled, or a genuine opt-out quietly does not.

### Organization (Tenant)
The unit of tenancy, isolation, billing and configuration. **The primary key is our own UUID; `clerk_org_id` is a unique column, never the PK** — telephony entities, call records, retained recordings and billing ledgers must outlive an auth-provider migration or a deleted upstream org. "Organization" is the schema word; "tenant" is the architectural word for the same thing. Use `organization_id` in code.

### Outbound Call
A call the platform initiates, always originating from a dial job produced by [campaign](#campaign) dispatch (or an explicit single-dial action), always after the [compliance gate](#compliance-gate), always with an [idempotency key](#idempotency-key).

### Outbox
The `outbox` table plus the relay that drains it. The voice gateway writes call state **and** the intent-to-publish in **one transaction**; a relay running in the worker publishes to the broker afterwards. This removes the dual-write between Postgres and Redis — a crash between two writes would otherwise lose or duplicate a call-completion event. It is why `rn_voice` has no broker client at all, enforced by an import contract.
*Delivery is at-least-once*, so every consumer must be idempotent.

### Played Milliseconds
Our running count of how much assistant audio the **caller actually heard** — not how much we generated, and not how much we handed to the telephony provider, which buffers. It is the value sent as `audio_end_ms` on [barge-in](#barge-in), and it is reconciled against [mark events](#mark-event). A wrong figure silently corrupts the model's belief about the conversation: the highest-risk correctness bug in the system, and it fails quietly.

### Post-call Analysis
The asynchronous pipeline that runs after a call ends: transcript assembly, schema-constrained [structured output](#structured-output), usage and cost metering, lead qualification, follow-up actions, campaign metrics. It runs in the [processing plane](#processing-plane) via `rn_orchestration` and is the **only** place LangGraph is permitted.

### Processing Plane
`apps/worker` plus the scheduler. Everything expensive, retryable and analytical: post-call analysis, ingestion, exports, [reconciliation](#reconciliation), campaign dispatch ticks, the [outbox](#outbox) relay. Budget in seconds to minutes.

### Provider
An external system we depend on: telephony, realtime voice, STT, TTS, LLM, embeddings, messaging, storage, identity. Named providers are Exotel, OpenAI, Sarvam, Clerk, and the object store.
*Not to be confused with:* [provider seam](#provider-seam), which is the interface, not the vendor.

### Provider Seam
The interface in `rn_providers` behind which a provider sits — `TelephonyProvider`, `VoiceSession`, `STTProvider`, `LLMProvider`, `EmbeddingProvider`, `MessagingProvider`, `StorageProvider`, `IdentityProvider`. Vendor SDKs (`openai`, `aioboto3`, `clerk_backend_api`) may not be imported anywhere else, enforced by import-linter. Seams are written when a second implementation is imminent or when the seam protects the hot path — not speculatively for all of them.
**What does not abstract cleanly is exposed, not hidden:** interim transcripts, barge-in mechanics, turn-detection ownership, voice catalogues, audio formats and session lifetimes differ per provider and are surfaced through an explicit `SessionCapabilities` object that callers branch on.

### RAG
Retrieval-augmented generation: [knowledge chunks](#knowledge-chunk) retrieved by vector similarity and given to the model as *quotable data*. **Retrieved text and caller speech are untrusted input** — data to be quoted, never instructions to be followed. Retrieval is always tenant-scoped, and the scoping lives inside the single retrieval helper so no caller can forget it.

### Realtime Session
The provider-side session object on the model leg — for OpenAI Realtime, the WebSocket connection plus its `session.update` configuration (instructions, voice, tools, turn policy). It has a **hard 60-minute cap** `[C]` on a clock independent of the telephony leg's own 60-minute cap `[C]`.
*Not to be confused with:* [agent session](#agent-session), which owns it. One agent session may own several realtime sessions in succession via [session rollover](#session-rollover).

### Reconciliation
The scheduled job that polls the telephony provider's Call Details for calls stuck without a terminal event, and repairs our state. **It is a required component, not a safety net:** status callbacks are explicitly documented as possibly delayed or dropped with no retry, and only two callback event types exist (`terminal` and `answered`) `[C]`. Any call-state UI finer than those two must be driven from the media socket lifecycle.

### Resampling
Converting PCM between [sample rates](#sample-rate). Required unless the telephony leg runs at 24 kHz, because the model accepts `audio/pcm` at 24 kHz only and the telephony provider emits `s16le` rather than G.711 — **the "G.711 passes through, no resampling needed" pattern does not apply to this stack** `[C]`. It lives in one `AudioTranscoder` at the telephony-adapter boundary, never inside provider clients or business logic.
Asymmetric quality requirement `[A]`: upsampling adds no information and can be cheap; **downsampling needs a proper anti-aliasing low-pass**, or aliasing degrades exactly the consonants Indian-language intelligibility depends on. No naive decimation.

### Ring Buffer
The outbound pacing buffer between the model's arbitrary-sized audio deltas and the telephony socket's [chunk](#chunk-audio) alignment rules. It emits only conforming chunks, maintains [played-milliseconds](#played-milliseconds) accounting, and is flushed as part of [barge-in](#barge-in). **A required component, not an optimisation** — unaligned writes produce choppy audio that looks convincingly like a network problem.

### Sample Rate
The PCM rate on the telephony leg. Exotel supports exactly **8000 / 16000 / 24000 Hz, mono, s16le**, selected per call `[C]`; OpenAI Realtime accepts `audio/pcm` at **24 kHz only** `[C]`. Our decision `[A]`: sample rate is a **per-agent field resolved at dial time** — default 24000 for OpenAI-primary agents (zero [resampling](#resampling), smaller minimum-chunk buffering) and 8000 for Sarvam-primary agents (zero conversion on that path). The exact query-parameter name is an [anti-fact](#anti-fact): seen once, uncorroborated — read it back from the `start` event or confirm with the provider.

### Session Rollover
Ending a [realtime session](#realtime-session) approaching its hard cap and continuing the same [agent session](#agent-session) on a fresh one: summarise, open new, replay condensed context. Needed because the telephony and model legs each cap at ~60 minutes on **independent clocks started at different moments** `[C]`. Neither provider documents a resume primitive, so do not architect around one — persist conversation items as they stream.

### Structured Output
The schema-constrained JSON produced by [post-call analysis](#post-call-analysis): summary, interest, qualification, intent, requested services, languages used, sentiment, budget, timeline, requirements, objections, questions asked, meeting booked, callback requested, WhatsApp sent, follow-up required, next action, outcome, confidence. **Analytics never parse free-form model text** — if a metric cannot be read from a typed field, the schema is wrong, not the query.

### Super Admin
The `SUPER_ADMIN` platform role held by the RiseNext team: onboarding and configuring organizations, platform-wide usage and health, inspecting calls where authorized, managing platform integrations and plans. It is a platform-level role, evaluated outside any single [organization](#organization-tenant), and every cross-tenant action it performs is audited.
*Not to be confused with:* `CLIENT_ADMIN`, which is the top role *inside* one organization.

### Tenant Isolation
The security boundary that prevents one organization seeing another's data. Enforced in the backend and in the database; the acting `organization_id` is derived from the verified token and **never** from a request body, a frontend value, or model output. Row-Level Security is defence in depth *on top of* application authorization, not instead of it. **Frontend filtering is not isolation.**
*Owner:* [SECURITY.md](SECURITY.md).

### Tool
A typed, schema-validated, tenant-scoped capability the model may **request** and the platform **performs** — `get_service_pricing`, `check_availability`, `book_meeting`, `send_whatsapp`, `record_opt_out`, and so on. The V1 registry is **18 tools**. Every tool with an external effect is idempotent, permission-checked and audited. No tool executes arbitrary SQL, arbitrary HTTP or arbitrary code, ever.
**The model requests; the platform decides.** `organization_id`, `call_id` and `agent_version_id` are injected from server-side session context; a model that emits one is ignored and the attempt is logged as a security event. Where a tool result must be handed back later — `check_availability` returns **opaque slot ids issued by the platform**, and `book_meeting` accepts only an id issued during **this same call** — the model may echo the identifier, never mint one.

### Tool Execution
One persisted invocation record: tool name, validated arguments, result, latency, outcome, call, agent version, actor. Written for every attempt including refusals, because it is simultaneously the audit trail, the debugging trail and the evaluation dataset.

### Tool Registry
The single declaration of every tool, in `rn_agent`, in plain Pydantic — **framework-free by contract**. It is exported two ways: flat function specs for the realtime session and LangChain `StructuredTool`s for `rn_orchestration`. The flat shape is generated from the Pydantic schema directly because `langchain-core`'s `convert_to_openai_tool()` emits the *nested* Chat-Completions shape, which the Realtime API rejects `[C]` — a silent-failure trap. This dual export is exactly why the registry must not depend on LangChain.

### Transcript
The assembled, durable text record of a call, built after the call from streamed [transcript segments](#transcript-segment). Per-frame audio events are never written to Postgres or Redis; they live in process memory for the call's duration.

### Transcript Segment
One speaker-attributed piece of a transcript with its timing and language. Segments are what the providers emit (final-per-utterance on the cascaded path, streamed on the realtime path); the transcript is what we assemble.

### Turn Detection
The full policy governing who speaks when: [VAD](#vad) mode and thresholds, [endpointing](#endpointing) sensitivity, whether the provider or we decide to start a response, and [barge-in](#barge-in) behaviour. Expressed as one `TurnPolicy` config object that adapters translate, and exposed as **per-agent, per-campaign configuration so ops can tune it without a deploy**.
Our default `[A]`: semantic VAD with low eagerness. **Do not hardcode any provider default you remember** — the beta-era values are dead and the GA defaults are unverified `[C]` on the removal, unverified on the values.

### Turn Latency
Time from the caller stopping speaking to the first agent audio reaching them. **TARGET: < 1.5 s p95** ([PRD §7](../PRD.md)). It is a budget composed of telephony hop, our transcode work, provider RTT, endpointing, model time-to-first-audio and minimum-chunk accumulation — and **the provider RTT from India is currently unmeasured**, so the target is provisional. We measure at our own egress toward the telephony provider; the PSTN leg beyond that is not observable to us.
*Owner:* [OBSERVABILITY.md](OBSERVABILITY.md).

### Usage Record
A normalised metering row emitted per call, minute, tenant, agent, campaign and provider from day one. Billing can be added later; the measurements cannot be retrofitted. Providers meter differently — token-based on the realtime model, per-minute and per-character on the fallback `[C]` — so the record is normalised at the seam and the billing engine never assumes a per-minute model.

### VAD
Voice Activity Detection: deciding whether audio contains speech. A primitive, owned by the provider on both paths but with different mechanisms and different thresholds.
*Not to be confused with:* [endpointing](#endpointing) (deciding the caller is *done*) or [turn detection](#turn-detection) (the policy built on both).

### Voice Gateway
`apps/voice-gateway` (`rn_voice`), the sole component of the [media plane](#media-plane): a WebSocket bridge between the telephony provider and the realtime voice provider, holding one [agent session](#agent-session) per live call. **It opens no database session of its own and holds no broker client** — it reads and writes through `rn_services` and records publish intent in the [outbox](#outbox). Both constraints are **excluded by import contract, not by packaging**: `rn_voice` depends on `rn_services`, which depends on `rn_persistence`, so SQLAlchemy, asyncpg and the Redis client do ship inside the gateway image. "No ORM in the media plane" is a comfortable phrasing and a false one; what is prevented is the gateway *using* them directly, and `lint-imports` is what prevents it.
It is *stateless as a process but stateful as a connection holder*: a live call is pinned to the instance holding its two sockets, so draining means refusing new calls and waiting — for up to an hour.

### Webhook Event
An inbound HTTP callback from a provider. **The two we receive are not comparable and must not share a trust model:**

| Source | Signature | Delivery | Consequence |
|---|---|---|---|
| Telephony status callbacks | **Unsigned — no HMAC, no signature header** `[C]` | may be delayed or dropped, no documented retry `[C]` | weak auth (HTTPS + secret path + IP allowlist + strict schema); idempotent on call SID; [reconciliation](#reconciliation) mandatory; **never let one authorize a state change with financial effect** |
| Identity provider (Svix) | HMAC-SHA256 over `{svix-id}.{svix-timestamp}.{raw_body}` `[C]` | eventually consistent, "deliveries are not guaranteed" `[C]` | verify against the **raw body** before any JSON parsing; never the only path that creates a tenant — provision lazily and treat the webhook as a reconciler |

---

## Adding a term

Add it here in the same session you introduce it in code, alphabetically, in the same shape: one or two sentences, a "not to be confused with" note if the term is routinely blurred, and a link to the document that owns it. Tag provider claims `[C]` only if you can point at a primary source in [PROVIDER_CONSTRAINTS](research/PROVIDER_CONSTRAINTS.md); anything else is `[A]`, **TARGET**, or omitted. A glossary that quietly accumulates unverified provider facts is worse than no glossary, because it is trusted.
