# RiseNext Voice AI Platform — Product Requirements

> **This document is the primary product source of truth.** When code and this document disagree, one of them is a bug — decide which, then fix it in the same change.
>
> **Status:** Phases 0–2 complete and merged; **Phase 3 Stage 1 complete** — the schema-independent foundations, the D-8 bake-off harness, and the Rise Next evaluation corpus (143 passages, 804 queries, human review closed 2026-08-11). Stage 2 is blocked on D-8, with **one recorded exception** built ahead of it in a schema-free form (`search_knowledge` and an in-memory retriever — [ADR-012](docs/DECISIONS/ADR-012-offline-in-memory-retriever.md)). **Phase 4** (provider seams, fakes and the audio transcoder) is implemented except its Exotel wire-capture deliverable, which is blocked on external input. See [docs/ROADMAP.md](docs/ROADMAP.md), which is the authority on what exists.
> **Last updated:** 2026-07-30
> **Companions:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/ROADMAP.md](docs/ROADMAP.md) · [docs/DECISIONS/](docs/DECISIONS/)

---

## 1. Vision

A multi-tenant platform that lets a business run **AI voice agents that hold real conversations** on real phone calls — inbound and outbound, in Indian languages, at production scale.

RiseNext is the first tenant, not the product. The first agent — *Aira*, RiseNext's own sales and customer-engagement assistant — is **one agent configuration running on the platform**. Everything Aira needs (knowledge, tools, languages, voice, campaign rules) is data, not code. A second customer in a completely different industry should require configuration and possibly a new tool, never a fork.

The one-sentence test for every design decision:

> *Would this still be right if there were fifty organizations, three hundred agents, and a thousand concurrent calls?*

---

## 2. The problem

Businesses in India lose revenue in the gap between a lead arriving and a human calling it back. Existing options are all bad in a specific way:

| Option | Why it fails |
|---|---|
| Human calling teams | expensive, hard to staff, inconsistent, does not scale in bursts, high attrition |
| IVR ("press 1 for sales") | callers hang up; captures no requirements; cannot answer a question |
| Recorded-audio robocalls | actively damages brand; regulated; zero information gathered |
| Generic global voice-AI vendors | weak on Hindi/Telugu and code-mixed speech, no India telephony/compliance story, per-seat pricing, no multi-tenant reselling |

What is missing is an agent that **converses** — understands what the caller actually said, adapts when they change their mind, answers with authoritative data rather than invention, and completes real actions (book, send, schedule, record) — in the languages Indian customers actually speak, including switching mid-sentence.

---

## 3. Users

| Role | Who | What they need |
|---|---|---|
| **SUPER_ADMIN** | RiseNext platform team | onboard and configure organizations, see platform-wide usage/health, inspect calls where authorized, manage platform integrations and plans |
| **CLIENT_ADMIN** | Customer's business owner or ops lead | manage their agents, campaigns, contacts, knowledge; see calls, transcripts and analytics; export; manage their team |
| **CLIENT_USER** | Customer's sales/support staff | work the leads: view assigned calls and outcomes, follow up; limited configuration rights |
| *(indirect)* **The caller** | The customer's prospect or customer | be understood, not be deceived, get a correct answer, be able to opt out and have it respected |

The caller is not a user of the dashboard, but they are the person the product is ultimately judged by. Requirements written on their behalf (AI disclosure, opt-out, data deletion) are **product requirements**, not compliance overhead.

---

## 4. Use cases

**Now (V1, RiseNext as tenant):** outbound lead qualification, inbound enquiry handling, service and pricing questions, requirement capture, meeting booking, callback scheduling, WhatsApp follow-up.

**Designed for (no re-architecture):** real-estate site-visit booking · hospital appointment scheduling and reminders · education admissions counselling · ecommerce order and return queries · automobile test-drive booking · customer support triage · surveys and feedback · service and payment reminders · renewal follow-ups.

The platform-level abstraction that makes all of these the same product: **an agent definition + a knowledge base + a tool set + a campaign or a phone number.**

---

## 5. What the AI must be

### 5.1 It must actually converse

Not a decision tree with a nicer voice. The agent must handle, as a baseline:

- A caller who **contradicts the premise**: *"I already have a website."* — acknowledge and pivot, do not continue the pitch.
- A caller who **changes the topic mid-call**: *"Actually I need social media management."* — follow, and carry earlier context forward.
- A caller who **asks something requiring authoritative data**: *"How much?"* — retrieve it with a tool; never invent a number.
- **Corrections and references**: *"No, not that one — the other one."*
- **Short, noisy, half-finished** phone speech, with the caller talking over the agent.

### 5.2 Languages

English, Hindi, Telugu — and **code-mixed speech within a single utterance**, which is how people actually talk:

- *"Website toh already hai, social media management chahiye."*
- *"Website already undi, marketing kavali."*
- *"App development ki approximately entha avutundi?"*

A call has no single language. Language handling is per-agent configuration, and the agent must not force the caller into one.

> **Highest product risk in the project.** There is no official speech-to-speech language list for the realtime model we plan to use, and no published Hindi/Telugu code-switching benchmark from any provider. Widely-repeated language counts belong to a *different* (translation) model. **Telugu quality is unverified.** This must be settled by our own evaluation on real Indian telephony audio before any language is promised to a customer — see [docs/ROADMAP.md](docs/ROADMAP.md) Phase 6 (language evaluation) and open decision **D-2**.

### 5.3 It must say it is an AI

The agent identifies itself as an AI assistant — *"Hi, I'm Aira, RiseNext's AI assistant."* — and never claims to be human when asked. Sounding natural is a quality goal; passing as human is not a goal and is not permitted. This is enforced in the agent definition and asserted in tests, not left to prompt phrasing.

### 5.4 It must respect "no"

"Remove my number", "don't call me again", "stop" — recognised in any supported language, honoured immediately, recorded as a durable opt-out that blocks future dialling across campaigns.

---

## 6. Functional requirements

### 6.1 Agents
- Agent definitions: identity, role, instructions, languages, voice, turn-taking policy, enabled tools, knowledge-base bindings, guardrails.
- **Versioned.** Every call records the exact agent version that served it. Changing a prompt does not rewrite history.
- Test/preview an agent without placing a real call.

### 6.2 Calls
- Outbound and inbound share one session runtime.
- Live call state; durable call record; transcript; per-turn timing; tool-execution log.
- Graceful handling of: no answer, busy, invalid number, caller hangs up mid-tool, provider disconnect.

### 6.3 Campaigns
- Import contacts from CSV/XLSX with validation, E.164 normalisation, and a **preview of what will be rejected and why** before anything is committed.
- Deduplication by policy; consent and opt-out checks before dialling.
- Queue-based dispatch honouring per-organization concurrency, platform concurrency, provider rate limits, calling windows and retry policy.
- Pause, resume, cancel. Scheduled start. Timezone-aware (IST first).

### 6.4 Tools
Initial set for Aira — extensible, per-agent, per-organization:

`search_knowledge` · `search_services` · `get_service_details` · `get_service_pricing` · `get_company_information` · `search_faq` · `create_lead` · `update_lead` · `save_customer_requirement` · `check_availability` · `book_meeting` · `schedule_callback` · `send_whatsapp` · `send_service_brochure` · `mark_interested` · `mark_not_interested` · `record_opt_out` · `add_call_note`

Every one is typed, schema-validated, tenant-scoped, permission-checked, audited, and idempotent where it has an external effect.

Two of these deserve a note because they are easy to get wrong:

- **`record_opt_out` is separate from `mark_not_interested`.** "Not interested" is a sales signal about this conversation; an opt-out is a durable, cross-campaign suppression with legal weight. Conflating them means a caller who said "remove my number" gets dialled again by the next campaign. A code-side guardrail matcher is a second, independent path that fires even if the model never calls the tool.
- **`book_meeting` accepts only an opaque slot ID that `check_availability` issued during the same call.** The model may echo back an identifier the platform issued; it may never originate one. The same rule covers IDs, prices, discounts and permissions.

### 6.5 Knowledge
- Multiple knowledge bases per organization; assignable per agent.
- Ingestion pipeline: parse → normalise → chunk → enrich metadata → embed → index.
- Versioning, re-indexing, deletion. Retrieval is **always** tenant-scoped.
- Knowledge answers *"what do you do?"*. It does **not** answer *"what does it cost today?"* — that is an authoritative tool. Keeping these separate is a correctness requirement, not a style preference.

### 6.6 Actions
- WhatsApp send with template compliance and delivery-status tracking.
- Meeting booking against **real** availability — the model never invents a slot — with duplicate-booking prevention and timezone handling.
- Callback scheduling with careful relative-date resolution (*"Friday evening"*) and confirmation when ambiguous.

### 6.7 Post-call intelligence
Schema-constrained structured output per call: summary · interest · qualification · intent · requested services · languages used · sentiment · budget · timeline · requirements · objections · questions asked · meeting booked · callback requested · WhatsApp sent · follow-up required · next action · outcome · confidence.

**Analytics never parse free-form model text.**

### 6.8 Dashboard
- **Super admin:** organizations, agents, calls, campaigns, usage, integrations, platform analytics, system health.
- **Client:** dashboard, calls, call detail, campaigns, contacts/leads, agents, knowledge base, analytics, exports, integrations, team, settings — scoped to their organization and nothing else.

### 6.9 Analytics & export
- Metrics: total/answered/failed calls, answer rate, interest breakdown, meetings, callbacks, WhatsApp sent, service interest, language mix, duration, conversion, campaign and agent performance, provider/model usage.
- Filters: date range, campaign, agent, service, language, interest, outcome — and the **same filters drive Excel export**.
- Large exports run asynchronously and are delivered via an expiring link.

---

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| **Conversation latency** | Target < 1.5 s p95 from caller stopping speech to first audio back. Budget and instrumentation defined in [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md). Provider round-trip from India is currently **unmeasured** — the target is provisional until it is. |
| **Barge-in** | Agent audio stops within ~200 ms of detected caller speech. |
| **Concurrency** | Demo: a handful. V1 production target: **100 concurrent calls.** Long term: thousands across organizations. |
| **Scaling** | Horizontal. No component may assume it is the only instance. |
| **Availability** | An AI provider or Redis failure degrades gracefully; it does not lose a call record. |
| **Durability** | Postgres is the source of truth. Redis holds nothing that cannot be lost. |
| **Tenant isolation** | Enforced in the backend and the database. Frontend filtering is not isolation. |
| **Auditability** | Every sensitive action and every tool execution is attributable to an actor, an organization and a call. |
| **Cost** | Metered per call, minute, tenant, agent, campaign and provider from day one — billing can be added later, but the measurements cannot be retrofitted. |
| **Testability** | The full call flow must be exercisable without placing a paid phone call. |

**We do not claim a concurrency figure we have not load-tested end to end with provisioned provider capacity.** The realtime model provider publishes no concurrent-session limit, and telephony channel capacity is a commercial question, not a documented one.

---

## 8. Architecture summary

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In brief:

- **Three planes.** A control plane (API + dashboard), a media plane (the voice gateway — the only latency-critical component), and a processing plane (workers). They share libraries, not processes.
- **Modular monolith + separately scalable realtime service**, in a monorepo. Not microservices.
- **Layer boundaries are enforced by tooling** (`import-linter`), including the permanent rule that the **audio transport layer** is free of any orchestration framework. A live session's *decision* layer may consult orchestration when a measurement justifies it; the media path never may. See [ADR-009](docs/DECISIONS/ADR-009-orchestration-boundary-for-live-sessions.md).
- **Everything external is behind a provider interface**: telephony, realtime voice, STT, TTS, LLM, embeddings, messaging, storage, identity.
- **The model requests; the platform decides.** Tool calls are validated, authorized and executed server-side with tenant context injected, never accepted, from the model.
- **Postgres + pgvector** for truth and retrieval; **Redis** for coordination only; **Taskiq/Redis Streams** for jobs; **transactional outbox** so the voice gateway never dual-writes.

---

## 9. Demo scope (Milestone 1)

The first demo is complete when, end to end:

1. Open the RiseNext dashboard.
2. Upload a CSV/XLSX of **consented internal test numbers**.
3. Create and start a campaign.
4. A real phone rings.
5. Aira introduces herself **as an AI assistant**.
6. The caller speaks English, Hindi, Telugu — and switches mid-sentence.
7. Aira answers correctly about RiseNext's services.
8. The caller interrupts. Aira stops immediately and responds to the interruption.
9. The caller changes their requirement mid-call. Aira follows.
10. The caller asks about price. Aira retrieves authoritative pricing via a tool.
11. The caller asks for details on WhatsApp. The message is sent and its delivery tracked.
12. The caller requests a meeting or a callback. It is recorded against real availability.
13. Call ends; post-call analysis runs asynchronously.
14. The dashboard shows the call, transcript and summary.
15. Interest, service, language and outcome are **structured**, filterable, and exportable to Excel.

**Explicitly out of scope for the demo:** billing, self-service onboarding, human transfer, a no-code agent builder, non-Exotel telephony, calls to numbers that have not consented.

---

## 10. Beyond the demo

Client self-onboarding · agent builder UI · additional telephony and messaging providers · CRM/calendar integrations · recording with consent management · plans and billing · A/B testing of prompts and voices · richer multi-agent workflows · additional Indian languages.

---

## 11. Known constraints

These are verified facts we must build around, not opinions. Sources in [docs/research/PROVIDER_CONSTRAINTS.md](docs/research/PROVIDER_CONSTRAINTS.md).

1. **Audio format mismatch is unavoidable.** Our telephony provider streams raw PCM (not G.711); the realtime model accepts PCM at one specific rate. The convenient "no resampling needed" telephony pattern does not apply, so a resampler and a byte-aligned pacing buffer are required components.
2. **Barge-in is not automatic** on the WebSocket transport. We must track exactly how much agent audio the caller heard and tell the model. Getting this wrong corrupts the conversation silently.
3. **Telephony status callbacks are unsigned and may be dropped.** Webhook handling must be idempotent, and a reconciliation job is mandatory.
4. **Sessions have hard time caps** (~60 minutes) on both the telephony and model legs, on independent clocks. Long calls need session rollover.
5. **Telugu support is unverified** by any provider documentation. See §5.2.
6. **Neither the managed database's regions nor the model provider's realtime media regions include India.** This has legal consequences — see **D-1**.
7. **Provider concurrency is not documented.** Both the realtime model's concurrent-session limit and telephony channel capacity must be established commercially before any capacity promise.
8. **The fallback Indian-language STT provider caps concurrent sockets at ~100**, making it viable as a fallback but not as a primary at scale without negotiation.

---

## 12. Open decisions

Reversible technical choices have been made and documented in [docs/DECISIONS/](docs/DECISIONS/). The items below **need your input** — they are expensive or irreversible to change later.

Phase numbers below refer to the sequence in [docs/ROADMAP.md](docs/ROADMAP.md#the-phases).

| ID | Decision | Why it cannot wait | Blocks |
|---|---|---|---|
| **D-1** | **Data residency.** May call recordings, transcripts and caller PII leave India? | The managed Postgres region is immutable at project creation and has no India option; the realtime model has no Indian media region. If the answer is "no", the database choice changes and the primary AI provider may have to change, inverting the architecture. **Every other infrastructure decision is downstream of this one.** | Provisioning the managed database; Phase 5 onward. Phases 1–4 run on local Postgres and provider fakes. |
| **D-2** | **Language commitment.** Do we promise Telugu at launch, or English+Hindi first with Telugu after evaluation? | No provider documents Telugu speech-to-speech support. Promising it before measuring is a commercial risk. | Any customer-facing language promise. Phase 6 exists to produce the evidence that answers it. |
| **D-3** | **Consent model and liability.** What artifact counts as proof of opt-in, how long is it retained, and who is liable when a tenant uploads a non-consented list — us or the tenant? | The telephony provider contractually requires producing opt-in evidence within 24 hours. This shapes the schema and the contract. | Phase 9 |
| **D-4** | **Calling window and DND responsibility.** Confirm the permitted window with the provider, and whether they scrub DND/NCPR server-side or it is entirely ours. | Determines whether we must integrate a scrubbing service. Must be configuration, never a hardcoded constant. | Phase 9 |
| **D-5** | **Recording.** Do we record calls at all in V1? Per-tenant configurable? | Changes storage, retention, consent flow and the disclosure script. | Phase 8. Cheap to keep open **only if** the media bridge is built with a disabled tap point from Phase 5. |
| **D-6** | **Provisioned capacity.** Confirm telephony channel capacity and realtime-model concurrency limits commercially. | The "100 concurrent calls" target is unverifiable without this. | Phase 16, and therefore the entire V1 concurrency claim. |
| **D-7** | **Auth plan tier.** More than 10 custom roles, or verified-domain auto-join, requires a paid add-on. | Affects the roles/permissions model. | Phase 15. Avoidable entirely by keeping the platform role catalogue at ≤10 and putting per-tenant roles in our own database. |
| **D-8** | **Production embedding model and vector storage layout.** Which embedding model, at what dimension, in which column type, with what index and partitioning — if any. | The embedding dimension is part of the Postgres column type, so changing it later is a full re-embed **plus a table rewrite of every tenant**; partitioning cannot be retrofitted onto a live vector table at all. These are the two least reversible decisions in the system, and the current placeholder (1536) is a vendor default, not a measured choice. No per-language benchmark exists for our providers on Indic text. | Phase 3, **Stage 2**. Resolved by a bake-off on real Indic and code-mixed data. Nothing before it needs the answer — Phase 1 creates no vector column, and Phase 3 Stage 1 creates none either. The harness, metrics, gates and candidate manifest now exist and run offline, and the evaluation corpus — 143 passages, 804 queries — completed native-speaker review on 2026-08-11. What remains is superseded Rise Next content for the `stale` adversarial role, a decision on the `size` gate, and approval for a paid run. See [ADR-010](docs/DECISIONS/ADR-010-defer-vector-storage-layout.md) and [docs/research/D8_BAKEOFF.md](docs/research/D8_BAKEOFF.md) §11. |

---

## 13. Success criteria

**Milestone 1 (demo):** all 15 steps in §9 pass with a real call to a consented number.

**V1 production:**
- 100 concurrent calls sustained in a load test with provisioned provider capacity.
- p95 turn latency within target, measured — not estimated.
- Zero cross-tenant data access in an adversarial test, including prompt-injection attempts.
- Opt-out honoured 100% of the time, verified by test.
- Post-call analysis produces valid structured output for ≥99% of completed calls.
- A second organization can be onboarded with **configuration only** — no code change.

**Product:** a caller cannot tell the difference in conversational competence between Aira and a good human agent on the first turn — and is told she is an AI anyway.

---

## 14. Glossary

See [docs/GLOSSARY.md](docs/GLOSSARY.md). The two terms worth knowing before reading anything else:

- **Agent definition** — persistent, versioned configuration. Data, not a process.
- **Agent session** — one live call's isolated runtime. Never shares mutable state with another call.
