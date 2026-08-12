<!--
  Provenance: generated 2026-07-28 by a 7-agent research pass that fetched official
  provider documentation. Confidence tags are the researchers' own:
    [C] confirmed against a primary source (URL given)
    [L] likely / single source
    [A] assumed or inferred - NOT verified
  Sections 6 and 7 matter most: 6 = open questions, 7 = plausible-sounding claims that
  could NOT be confirmed. Never promote anything from section 7 into a design doc
  without re-verifying it. Re-verify any API shape before implementing against it.
-->
# ARCHITECTURE CONSTRAINTS BRIEF
**India-first multi-tenant AI voice-calling platform** — FastAPI + Next.js · Exotel telephony · OpenAI Realtime primary · Sarvam cascaded fallback · Postgres+pgvector · Redis · Clerk
*Compiled 2026-07-28 from 7 research areas. Every claim below is tagged **[C]** confirmed-from-primary-source, **[L]** likely/single-source, or **[A]** assumed/inferred by me. Nothing untagged is fact.*

---

## 0. Errata and later confirmations

> **Everything below section 0 is a point-in-time snapshot of 2026-07-28 and is deliberately NOT updated — its value is the record of what was and was not verifiable that day. This errata section is the only part of this document kept current.**

| # | Where | What the body says | Status now | How confirmed |
|---|---|---|---|---|
| E-1 | §4 | `clerk-backend-api` **UNKNOWN**; `taskiq-redis` "exact version unpinned"; `svix` "latest **[A]**" | **Resolved.** `clerk-backend-api` **6.0.1**, `taskiq-redis` **1.2.3**, `svix` **1.99.1** | An actual `uv lock` in this repository — `uv.lock`, 145 packages, Python 3.12.11 |
| E-2 | §3 Seam 3 | Declare tools with LangChain's `@tool` + pydantic `args_schema` | **Superseded** by ADR-004 | [`../DECISIONS/ADR-004-langgraph-off-the-hot-path.md`](../DECISIONS/ADR-004-langgraph-off-the-hot-path.md) |
| E-3 | §5 Vector index / Checkpoint hardening | Column called `tenant_id` | **Renamed.** The platform-wide column name is `organization_id` | DATA_MODEL.md |
| E-4 | §5 Vector index | `PARTITION BY LIST(tenant_id)` **with hash sub-partitioning** to bound partition count | **Diverges** from the accepted Phase 1 design: LIST partitioning + a DEFAULT partition, per-tenant promotion above a threshold; hash sub-partitioning kept only as a future mitigation | ADR-006, DATA_MODEL.md |
| E-5 | §2 sample-rate table & latency budget | 24000 Hz row: "Min-chunk latency (3200 B) … **66.7 ms**" | **Superseded** by ADR-003: smallest legal emission at 24 kHz is **3840 B = 80 ms** | ADR-003, REALTIME_VOICE.md |
| E-6 | §5 Chosen Defaults — **Embedding model**, **Vector index**, **Tenant isolation** rows | `text-embedding-3-small` @ 1536 stored as `halfvec(1536)`; tiered index with `PARTITION BY LIST(tenant_id)` + hash sub-partitioning; "LIST partitioning **for performance**" | **WITHDRAWN / SUPERSEDED. These are not chosen and must not be implemented.** [ADR-010](../DECISIONS/ADR-010-defer-vector-storage-layout.md) withdrew both the `halfvec(1536)` column and the LIST partitioning, and records them as open decision **D-8**. The model, width, column type, ANN index, partitioning **and `document_chunks`' physical primary key** are all pending measurement. | [ADR-010](../DECISIONS/ADR-010-defer-vector-storage-layout.md) supersedes the Decision of [ADR-006](../DECISIONS/ADR-006-pgvector-tenant-isolation-and-embeddings.md); status and blockers in [D8_BAKEOFF.md](D8_BAKEOFF.md) |

**E-1 notes.** The following §4 pins also resolved *exactly* as predicted, so §4's confidence tags there held up: `langchain` 1.3.14, `langchain-core` 1.5.1, `langgraph` 1.2.9, `langgraph-checkpoint-postgres` 3.1.0, `taskiq` 0.12.4, `openai` 2.49.0, `soxr` 1.1.0.

**E-2 notes.** The tool registry lives in `rn_agent` and is declared with plain Pydantic models, because `rn_agent` must not depend on LangChain — otherwise the voice gateway would be forced to import an orchestration framework onto the live-call path. Only the `@tool`/`args_schema` *declaration mechanism* is superseded. The flat-vs-nested tool-schema warning in the same section (**HC-19**, and anti-fact #15) remains valid and remains one of the highest-value traps in this brief.

**E-4 notes.** The divergence is deliberate: hash sub-partitioning multiplies the partition count for *every* tenant to solve a problem only an outsized tenant has. A DEFAULT partition absorbs the long tail of small tenants at one partition, and a tenant is promoted to its own partition once it crosses a size threshold. Sub-partitioning a single promoted partition remains available later without a redesign.

**E-5 notes.** At 24 kHz the alignment quantum must be **960 bytes**, not 320: 320 B at 24 kHz is 6.667 ms, and accumulating playback in 6.667 ms units makes `audio_end_ms` drift, which silently corrupts barge-in truncation (**HC-7**). The minimum emission is therefore the smallest multiple of 960 that is ≥ 3200 B → **3840 B = 80 ms** (3840 ÷ 48 000 B/s = 0.080 s). The 8 kHz figure is unaffected: 3200 B ÷ 16 000 B/s = **200 ms**. Note that §7 anti-fact #1 already warned that the byte thresholds are authoritative and the millisecond glosses unreliable — this is exactly that failure, and §2's own "Where resampling lives" paragraph already stated the 960-byte rule, so the table contradicted the prose.

**E-6 notes — read this before implementing anything in §5's data-tier rows.**

The withdrawn rows are the highest-risk stale content in this document, because they read
as a settled design and they are three lines of SQL away from being irreversible. A reader
who follows §5 would build `halfvec(1536)` with LIST partitioning; the dimension would
become a Postgres typmod, and changing it later costs a **full paid re-embed of every
tenant plus a table rewrite**, while partitioning cannot be usefully retrofitted at all.

**What was withdrawn, and why** ([ADR-010](../DECISIONS/ADR-010-defer-vector-storage-layout.md) has the full argument):

- **1536 was never evaluated on merit.** It is the native output width of
  `text-embedding-3-small` — a vendor default adopted before measuring anything, on a
  corpus that is English/Hindi/Telugu and code-mixed, against **L-8** in this very
  document recording that no per-language Indic benchmark exists.
- **`halfvec` was chosen to dodge a cap we are not near.** HC-24 binds only above 2000
  dims. At 1536 *both* types are indexable, so the argument reduces to storage and build
  time against an **unmeasured recall cost**.
- **LIST partitioning is unjustified at single-digit tenants**, and it was the only reason
  the previously-sketched composite primary key on `document_chunks` existed.

**What survives from §5 and the surrounding sections, and is still authoritative:**
**HC-24** (HNSW dimension caps), **HC-25** (filtered approximate search post-filters and
silently under-returns — the single most important fact in the data tier), **HC-26**
(transaction-mode pooling forbids session-level `SET`), **HC-27/HC-28** (Neon regions and
scale-to-zero), the **two-DSN** split, the argument against partial-index-per-tenant
(anti-fact 24), "vectors live in the same Postgres as everything else", and "RLS is
defence in depth, not the isolation mechanism". None of that depends on the withdrawn
choices, and all of it constrains whatever layout D-8 selects.

Per this document's own rule, §5 itself is **left unedited**: everything below section 0
is a deliberately-frozen 2026-07-28 snapshot whose value is the record of what was and
was not verifiable that day. This errata row is the correction.

---

## 1. Hard Constraints

Facts that eliminate design options. Format: **fact → source → consequence.**

### Telephony wire

**HC-1 [C] Exotel AgentStream carries audio as base64 strings inside JSON *text* frames — never binary WebSocket frames — and the codec is raw/slin (s16le, mono, little-endian PCM), NOT G.711 mu-law.**
`https://support.exotel.com/support/solutions/articles/3000108630-working-with-the-stream-and-voicebot-applet`
→ Every 20–100 ms of audio in both directions costs a JSON parse + base64 transcode. Budget CPU per concurrent call accordingly (~10–20 msg/s/direction). Rules out any zero-copy binary bridge.

**HC-2 [C] Outbound chunks to Exotel must be a multiple of 320 bytes, ≥ 3200 bytes, ≤ 100000 bytes.** (same source)
→ A pacing/alignment ring buffer between the model and the socket is **mandatory**, not an optimization. TTS/model deltas arrive at arbitrary sizes; emitting them raw produces choppy audio. This is the most common integration failure mode.

**HC-3 [C] Exotel supports exactly 8000 / 16000 / 24000 Hz, mono, s16le, selected per-call via a query param on the Voicebot applet URL.**
`https://docs.exotel.com/exotel-agentstream/voicebot-applet`
→ Sample rate is a **per-call, per-agent config resolved at dial time**, not a global constant. See §2.

**HC-4 [C] OpenAI Realtime GA accepts `audio/pcm` at 24 kHz ONLY; the only other formats are `audio/pcmu` and `audio/pcma` (G.711, inherently 8 kHz).**
`https://developers.openai.com/api/reference/ruby/resources/realtime`
→ **Combined with HC-1 this is the single most consequential constraint in the system.** Exotel emits slin, not G.711, so the celebrated "G.711 passthrough, no resampling" telephony pattern **does not apply to Exotel**. Either run Exotel at 24 kHz (zero resampling, 3× bandwidth) or build a resampler. There is no third option. Full analysis in §2.

**HC-5 [C] Exotel: max streaming session 60 min; max call TimeLimit 14400 s; bot must respond within 10 s of connect or the session fails; exactly ONE automatic handshake retry.**
`https://docs.exotel.com/exotel-agentstream/advanced` [L on the 10s/retry specifics]
→ (a) No blocking initialization inside the WS accept path — pre-warm the model connection or emit silence while it establishes. (b) Cold-start serverless is disqualified for the media endpoint; use warm long-running containers behind an LB with health checks.

**HC-6 [C] OpenAI Realtime hard session cap: 60 minutes.**
`https://developers.openai.com/api/docs/guides/realtime-conversations`
→ Coincides with Exotel's 60 min but is an *independent* clock started at a different moment. Build session-rollover (summarize → new session → replay condensed context) from day one for any flow that can approach it.

**HC-7 [C] On the WebSocket transport, OpenAI does NOT auto-truncate on barge-in — only WebRTC does. The client must send `conversation.item.truncate` with a truthful `audio_end_ms`.** (same source)
→ The bridge must maintain exact playback accounting of assistant audio actually delivered to the caller. A wrong `audio_end_ms` silently corrupts the model's belief about what the caller heard — highest-risk correctness bug in the system, and it fails *quietly*.

**HC-8 [C] Barge-in on Exotel requires `{"event":"clear","stream_sid":...}`, which only discards audio Exotel has buffered but not yet played — it does not stop our generator.** (Exotel support article)
→ Barge-in is a **three-part atomic operation**: (1) `clear` to Exotel, (2) flush our own ring buffer, (3) truncate/cancel upstream. Implement as one function; never as three call sites.

**HC-9 [C] Exotel `mark` events: we send a mark after a media chunk, Exotel echoes it when that audio has actually finished playing.** (Exotel support article)
→ This is the **only** ground-truth playback position available. Use marks to correct the ring-buffer estimate that feeds `audio_end_ms`.

### Telephony control plane & compliance

**HC-10 [C] Exotel does NOT sign StatusCallback webhooks. No HMAC, no signature header, anywhere in the docs.**
`https://developer.exotel.com/docs/references/authentication`
→ Layered mitigation only: HTTPS + high-entropy secret path segment + IP allowlist (list is unpublished, must be obtained from support) + strict schema validation. Treat webhook auth as *weak* and never let a webhook alone authorize a state change with financial effect.

**HC-11 [C] Exotel explicitly states StatusCallback delivery may be delayed or fail, with NO documented retry, and advises polling Call Details as fallback.**
`https://developer.exotel.com/api/statuscallback`
→ A reconciliation job is a **required component**, not a nice-to-have. All callback handling idempotent on `CallSid`.

**HC-12 [C] Voicebot applet custom parameters: max 3 key/value pairs, total query string after `?` ≤ 256 characters.**
`https://docs.exotel.com/exotel-agentstream/voicebot-applet`
→ Pass one opaque `session_id` only. All business context is looked up server-side, joined on `call_sid` from the `start` event.

**HC-13 [C] Rate limits: `/v1/Accounts/{sid}/Calls/connect` = 200 req/min (429 on breach); campaigns default throttle 60 calls/min, max 5000 contacts/campaign.**
`https://developer.exotel.com/api/make-a-call-api`, `https://developer.exotel.com/api/campaigns-lists` [L]
→ Token-bucket limiter + durable queue in front of every Exotel dial path. No burst-dialing from a worker pool.

**HC-14 [C] India NCPR: calls to NCPR-registered numbers must be transactional AND the number must be whitelisted (inbound contact within the last 6 months). Enabling DND calling requires filing opt-in evidence, and Exotel requires producing that evidence within 24 hours on any violation.**
`https://support.exotel.com/support/solutions/articles/35421-...`, `https://docs.exotel.com/business-phone-system/enable-dnd-calling`
→ A **pre-dial compliance gate in code** is mandatory: consent record exists → call classified transactional/promotional → IST calling window → 6-month whitelist recency. Opt-in evidence (source, timestamp, artifact) must be a first-class persisted entity with 24h retrievability.

**HC-15 [C] Only two StatusCallback event types exist: `terminal` and `answered`.** (statuscallback doc)
→ No ringing/progress events. Any finer-grained call-state UI must be driven from the media socket lifecycle, not webhooks.

### Realtime provider

**HC-16 [C] The Realtime **Beta** interface was removed from the API on 2026-05-12. `OpenAI-Beta: realtime=v1`, `session.input_audio_format`, and the `g711_ulaw` string enums are dead.**
`https://developers.openai.com/api/docs/changelog`
→ Every tutorial, blog post, and third-party realtime helper library predating 2026-05 must be validated against the GA `session.audio.input.format` **object** shape before adoption. Assume most OSS examples are broken.

**HC-17 [C] OpenAI SIP media originates from northeurope / southcentralus / eastus2 / westus. No India, no Asia.**
`https://developers.openai.com/api/docs/guides/realtime-sip`
→ Direct-SIP topology routes every Indian call's media to Europe or the US. Strengthens the case for our own bridge (topology A, §2), which at least keeps recordings/PII in India even though the model remains remote. Must be raised in DPDP review.

**HC-18 [C] Realtime rate limits are published as RPM/TPM per tier only. There is no documented concurrent-session limit.** `https://developers.openai.com/api/docs/models/gpt-realtime-2.1`
→ **Do not promise a concurrency SLA.** TPM is the binding constraint for audio; derive max concurrent calls empirically from measured tokens/min/call, then load-test. See §6a.

**HC-19 [C] Realtime tools are declared FLAT: `{"type":"function","name",...,"parameters"}` — properties at top level, not nested under `function`.**
`https://developers.openai.com/api/docs/guides/realtime-conversations#function-calling`
→ `convert_to_openai_tool()` from langchain-core returns the **nested Chat-Completions shape and will not work**. Correct export is `{"type":"function", **convert_to_openai_function(t, strict=True)}`. This is a silent-failure trap.

### Fallback provider (Sarvam)

**HC-20 [C] The Sarvam STT WebSocket emits NO interim/partial transcripts. Results are final-per-utterance, gated on VAD `speech_end` or explicit `flush`.**
`https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/which-api-to-use.md`
→ Any turn-taking design assuming Deepgram-style partials (speculative prefill, early barge-in from text) does not port. The turn-taking layer needs a VAD-only code path. Expose `supports_interim: bool` on the STT seam.

**HC-21 [C] Sarvam STT WebSocket concurrency: 20 (Starter) / 100 (Pro) / 100 (Business). It does not scale past 100.**
`https://docs.sarvam.ai/api/getting-started/ratelimits.md`
→ Sarvam STT caps us at ~100 concurrent calls regardless of tier. Viable as fallback; **not viable as primary at scale** without a negotiated cap increase. TTS WebSocket scales to 1000 — the legs are asymmetric, so model provider capacity per-leg, not per-call.

**HC-22 [C] Both Sarvam STT and TTS WebSockets close after ~60 s idle.**
`https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/best-practices.md`
→ Two independent keepalives required: near-zero-amplitude PCM into STT, `ping()` into TTS. Without these, calls drop mid-conversation and look like random provider failures.

**HC-23 [C] Sarvam STT accepts pcm_s16le/pcm_l16/pcm_raw/wav at 8000 or 16000 Hz — but NOT mulaw. Sarvam TTS *can* emit mulaw/alaw/linear16 at 8000 Hz.**
`https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/ws.md`, `.../text-to-speech/convert.md`
→ Asymmetric. Convenient with Exotel slin: at 8 kHz both legs are pure passthrough. See §2.

### Data tier

**HC-24 [C] pgvector HNSW index caps: `vector` ≤ 2000 dims, `halfvec` ≤ 4000 dims.**
`https://github.com/pgvector/pgvector`
→ `text-embedding-3-large` at native 3072 **cannot be indexed as `vector`**. Either reduce dims ≤2000 via the `dimensions` param, or store/cast as `halfvec(3072)`.

**HC-25 [C] "With approximate indexes, filtering is applied after the index is scanned… if a condition matches 10% of rows, with default `hnsw.ef_search` of 40, only 4 rows will match on average."** (same source)
→ A naive `WHERE tenant_id = $1 ORDER BY embedding <=> $2 LIMIT k` on a shared HNSW index is a **silent correctness bug** — it succeeds and returns too little context, surfacing as "the agent forgot our knowledge base." Forces the tiered strategy in §5.

**HC-26 [C] Neon's PgBouncer runs `pool_mode=transaction`: session-level `SET`/`RESET`, SQL PREPARE, LISTEN/NOTIFY, temp tables, and advisory locks are unsupported.**
`https://neon.com/docs/connect/connection-pooling`
→ All pgvector tuning must be `SET LOCAL` inside `BEGIN/COMMIT`. **Two connection strings in config**: pooled for app traffic, direct for migrations, index builds, advisory locks and session-level `SET`.

**HC-27 [C] Neon has no India/Mumbai region, and a project's region cannot be changed after creation.**
`https://neon.com/docs/introduction/regions`
→ Irreversible decision at project creation. Nearest is `aws-ap-southeast-1` (Singapore). If DPDP requires Indian residency for transcripts/PII, **Neon is disqualified** and the OLTP+vector tier moves to RDS/Aurora `ap-south-1` or self-hosted Postgres in Mumbai. §6b.

**HC-28 [C] Neon computes suspend after 5 min idle by default; reactivation "typically takes a few hundred milliseconds." Disabling requires a paid plan.**
`https://neon.com/docs/introduction/scale-to-zero`
→ Scale-to-zero **must be disabled on the production branch**; keep it on for dev/preview branches.

### Auth

**HC-29 [C] Clerk session token v2 nests org data under a single `o` claim (`o.id`, `o.slg`, `o.rol` *without* the `org:` prefix, `o.per`). v1 used flat `org_id`/`org_role` *with* the prefix. The Python SDK's `RequestState.to_auth()` v2 branch reads the FLAT names and will return `None`.**
`https://clerk.com/docs/backend-requests/resources/session-tokens` + `https://raw.githubusercontent.com/clerk/clerk-sdk-python/main/src/clerk_backend_api/security/types.py`
→ **Do not use `to_auth()` for org context.** Write a claim extractor handling both shapes and normalizing the `org:` prefix. Getting the prefix normalization wrong is an authorization-bypass class bug.

**HC-30 [C] "System Permissions aren't included in session claims. If you need to check Permissions on the server-side, you must create Custom Permissions."**
`https://clerk.com/docs/guides/organizations/roles-and-permissions`
→ All backend authz must be built on custom `org:<feature>:<action>` permissions. Clerk's 9 built-in system permissions never reach FastAPI.

**HC-31 [C] Max 10 custom Organization Roles per instance without the $100/mo Enhanced B2B add-on. Custom claims must stay under ~1.2 KB (cookie limit).** (same source + `https://clerk.com/docs/guides/sessions/session-tokens`)
→ Fixed role catalog ≤10; per-tenant custom roles must live in our own DB. No tenant config, phone numbers, or agent lists in claims.

**HC-32 [C] Svix webhook signature is HMAC-SHA256 over `{svix-id}.{svix-timestamp}.{raw_body}`.**
`https://docs.svix.com/receiving/verifying-payloads/how-manual`
→ The FastAPI handler must read `await request.body()` **before** any JSON parsing. Re-serializing breaks verification.

**HC-33 [C] Clerk webhooks are eventually consistent and "deliveries are not guaranteed."**
`https://clerk.com/docs/guides/development/webhooks/syncing`
→ A webhook must **never** be the only path that creates a tenant. Lazily provision on first sight of an unknown `clerk_org_id` in the auth dependency; the webhook is a reconciler.

### Orchestration & jobs

**HC-34 [C] ARQ is in maintenance-only mode by maintainer statement (issue #510, 2025-10-18); the only 2026 releases are Python-version compat bumps.**
`https://github.com/python-arq/arq/issues/510`
→ ARQ is disqualified as the job layer despite being the obvious "small async Redis queue" pick.

**HC-35 [C] taskiq-redis PubSub and ListQueue brokers have NO acknowledgement; only the Stream brokers do.**
`https://github.com/taskiq-python/taskiq-redis`
→ `RedisStreamBroker` + `--ack-type when_executed` is the only acceptable configuration for dial jobs.

**HC-36 [C] `langchain` 1.3.x hard-pins `langgraph>=1.2.5,<1.3.0`.**
`https://pypi.org/pypi/langchain/json`
→ These move as a **single version train**. You cannot bump langgraph independently.

**HC-37 [C] LangGraph issue #7259 (open, PR #7269 linked): `AsyncPostgresSaver` holds an instance-level `threading.Lock()` during async execution. Benchmarked at 500 concurrent users: 199.9 req/s @ 1923 ms vs raw psycopg_pool 1295 req/s @ 88 ms.**
`https://github.com/langchain-ai/langgraph/issues/7259`
→ ~85% throughput loss from in-process lock contention, not DB capacity. **Keep `AsyncPostgresSaver` off any concurrent live-call path** until confirmed fixed.

**HC-38 [C] LangGraph `interrupt()` restarts the entire node from the beginning on resume — it does not resume from the interrupt line.**
`https://docs.langchain.com/oss/python/langgraph/interrupts`
→ Any side effect placed before an `interrupt()` re-executes. For this platform that means a **duplicate outbound call to a real Indian phone number**. Side effects go after the interrupt or behind an idempotency key.

**HC-39 [C] langgraph-checkpoint-postgres docs advise `LANGGRAPH_STRICT_MSGPACK=true` (or an explicit `allowed_msgpack_modules` list) to prevent code execution from a compromised checkpoint DB.**
`https://pypi.org/pypi/langgraph-checkpoint-postgres/json`
→ Mandatory on a shared multi-tenant Postgres. Set it in the base config, not per-instantiation.

---

## 2. The Voice Hot Path — Concrete Contract

### Topology decision

**Chosen: (A) Bridge topology.** `Exotel Voicebot applet ⇄ our WS media bridge (ap-south-1) ⇄ OpenAI Realtime WS`.
Rejected: (B) Direct SIP (`sip:proj_xxx@sip.api.openai.com`). B removes our entire media path but simultaneously removes raw-audio tap for recording, our own barge-in policy, per-call Sarvam fallback, and India-side PII control — and its media terminates in Europe/US (HC-17). Keep B documented as a degraded fast-path only. **[A]**

### Inbound leg (caller → model)

```
PSTN 8 kHz
  └─ Exotel Voicebot applet
       JSON text frame: {"event":"media","sequence_number":N,"stream_sid":"...",
                         "media":{"chunk":C,"timestamp":"<ms string>","payload":"<base64>"}}
       payload decodes to: s16le PCM, mono, little-endian, @ {8000|16000|24000} Hz   [C]
  └─ BRIDGE
       1. JSON parse → base64 decode → bytes
       2. [conditional] resample → 24000 Hz s16le mono
       3. base64 re-encode
  └─ OpenAI Realtime WS
       {"type":"input_audio_buffer.append","audio":"<base64 pcm16 @24kHz>"}
       session.audio.input.format = {"type":"audio/pcm","rate":24000}   [C]
```

### Outbound leg (model → caller)

```
OpenAI Realtime WS
  response.output_audio.delta → base64 pcm16 @ 24 kHz, arbitrary delta sizes   [C]
  └─ BRIDGE
       1. base64 decode → bytes → append to PLAYBACK RING BUFFER
       2. [conditional] resample 24000 → target rate
       3. emit ONLY 320-byte-aligned chunks, ≥3200 B, ≤100000 B          (HC-2)
       4. increment played_ms accounting; emit a `mark` after each utterance
       5. base64 encode → wrap in the SAME media event shape
  └─ Exotel  {"event":"media","stream_sid":"...","media":{"payload":"<base64>"}}
  └─ Exotel echoes {"event":"mark","mark":{"name":"<label>"}} on actual playout completion  [C]
```

### Is resampling required? — **Yes, unless we run Exotel at 24 kHz.**

`audio/pcm` on OpenAI is locked to 24 kHz (HC-4). Exotel emits slin, never G.711 (HC-1). Therefore the G.711 passthrough escape hatch **is unavailable on this stack.** Three legal configurations:

| Exotel rate | OpenAI path | Sarvam path | Min-chunk latency (3200 B) | Byte rate |
|---|---|---|---|---|
| **8000** | resample 8↔24 both directions | **zero conversion** (STT accepts pcm_s16le@8000; TTS emits linear16@8000) | **200 ms** | 16 KB/s |
| **16000** | resample 16↔24 both directions (2:3) | zero conversion (Sarvam's documented optimal rate) | 100 ms | 32 KB/s |
| **24000** | **zero conversion** | downsample 24→16 for STT; TTS emits 24000 natively | **66.7 ms** | 48 KB/s |

**Decision [A]: make sample rate a per-agent field resolved at dial time** (the applet URL query param is per-call, and we know the agent's provider then). Default **24000 for OpenAI-primary agents**, **8000 for Sarvam-primary agents**. Rationale: 24 kHz eliminates resampling on the primary path *and* cuts minimum-chunk buffering latency from 200 ms to 67 ms — a meaningful slice of the turn budget. There is no audio-quality argument either way: the source is 8 kHz PSTN, so 16/24 kHz is Exotel upsampling with no added information. The cost is 3× base64/JSON CPU and bandwidth per call.

### Where resampling lives

A single `AudioTranscoder` at the **telephony-adapter boundary**, never inside provider clients, never inside business logic. Two implementations behind one interface: `PassthroughTranscoder` and `PolyphaseTranscoder`. Operate on 20 ms-aligned frames (at 24 kHz use **960-byte** alignment — a multiple of 320 that is also a whole number of ms; 320 B at 24 kHz is 6.67 ms and will drift your accounting).

**Asymmetric quality requirement [A]:** 8k→24k upsampling adds no information; linear or cheap polyphase is fine. **24k→8k downsampling requires a proper anti-aliasing low-pass** or you get audible aliasing on sibilants and consonants — degrading exactly the phonemes Indian-language intelligibility depends on. Do not use naive decimation.

### Barge-in sequence (atomic, one function)

1. `input_audio_buffer.speech_started` arrives from OpenAI **[C]**
2. → send `{"event":"clear","stream_sid":...}` to Exotel (note: no `sequence_number` in the documented example) **[C]**
3. → flush our outbound ring buffer, freeze `played_ms`
4. → send `conversation.item.truncate` with item id, content index, and `audio_end_ms = played_ms` **[C, HC-7]**
5. → reconcile `played_ms` against the last echoed `mark` when it arrives; log divergence as a health metric

### Latency budget (targets, not measurements) **[A]**

| Segment | Budget |
|---|---|
| Exotel → bridge (ap-south-1) | ≤ 15 ms |
| Bridge decode + resample + encode | ≤ 5 ms |
| Bridge (India) → OpenAI (nearest edge) RTT | **unmeasured — see §6a** |
| Model endpointing (`semantic_vad`, `eagerness: low`) | 300–800 ms |
| Min-chunk accumulation outbound | 67 ms @24k / 200 ms @8k |
| **Nothing else is permitted in this path.** No Postgres read, no LangGraph superstep, no vector search. | — |

---

## 3. Provider Abstraction Seams

### Seam 1 — `TelephonyProvider`
```
start_outbound_call(to, from_, agent_id, idempotency_key) -> call_sid
on_media_frame(cb)        # yields (pcm_bytes, sample_rate, seq)
send_media_frame(pcm_bytes)
clear_playback()
mark(label) / on_mark(cb)
on_dtmf(cb)
on_call_ended(cb)
```
Exotel's vocabulary (`connected`/`start`/`media`/`dtmf`/`mark`/`stop`/`clear`) maps near-1:1 onto Twilio Media Streams and Plivo AudioStream. **Adapter-local differences:** codec (slin vs mulaw), field naming (`stream_sid` vs `streamSid`), chunk alignment rules. **[C on the Exotel side, L on the mapping]**

### Seam 2 — `VoiceSession` (the provider-swap seam)
This is where OpenAI speech-to-speech and cascaded Sarvam STT→LLM→TTS must both plug in:
```
async open(agent_config) -> None
async push_audio(pcm, rate)
async stream_output() -> AsyncIterator[AudioChunk | ToolCall | TranscriptEvent | TurnEvent]
async truncate(played_ms)
async cancel_generation()
async submit_tool_result(call_id, output_json)
capabilities: SessionCapabilities
```

### Seam 3 — Tool registry (single source of truth)
Define every business tool once with `@tool` + a pydantic `args_schema`. Export two ways:
- `to_langchain_tools()` → for the LangGraph/offline path
- `to_realtime_tools()` → `[{"type":"function", **convert_to_openai_function(t, strict=True)}]` (**HC-19** — flat, not `convert_to_openai_tool`)
- Sarvam's LLM is OpenAI-compatible (`base_url=https://api.sarvam.ai/v1`, Bearer auth, tool calling, JSON Schema) **[C]** so the cascaded path reuses the nested Chat-Completions shape unchanged.

Tools requiring runtime context use `ToolRuntime` (excluded from the JSON schema) **[C]**, never closures over globals — this is what makes the same function object safely exportable to Realtime.

### What does NOT abstract cleanly — the leaky parts

| Leak | Why | Handling |
|---|---|---|
| **Interim transcripts** | OpenAI streams; Sarvam WS emits nothing until VAD end-of-speech (HC-20) | `SessionCapabilities.supports_interim: bool`. Turn-taking layer must have a VAD-only path. Do not fake partials. |
| **Barge-in / truncation** | OpenAI needs `conversation.item.truncate` with `audio_end_ms` (HC-7); Sarvam cascaded needs us to cancel TTS synthesis and drop buffered text — there is no context to truncate | Unify at the *effect* level (`cancel_generation()`), not the *mechanism*. The OpenAI adapter owns `audio_end_ms`; the Sarvam adapter owns the TTS-socket flush. |
| **Turn detection ownership** | OpenAI: `server_vad`/`semantic_vad` server-side. Sarvam: `vad_signals=true` with 512-sample (32 ms @16k) frames and its own thresholds | Expose one `TurnPolicy` config object; adapters translate. Or set OpenAI `create_response:false, interrupt_response:false` **[C]** and own turn policy ourselves in the orchestrator — this is the seam that lets guardrails/compliance run before we commit to an expensive spoken response. |
| **Voice/prosody** | OpenAI: 10 named voices, immutable once audio is emitted **[C]**. Sarvam: 37-ish bulbul:v3 speakers, per-language recommendations, `pace`/`temperature` **[C]** | Agent config carries a `language → (provider, voice_id)` map, not a global voice string. `priya`/`ishita` are the documented cross-language-safe Sarvam defaults. |
| **Audio format capability** | OpenAI pcm@24k only; Sarvam STT 8k/16k s16le only; Sarvam TTS 8k–48k + mulaw/alaw | Each adapter declares `accepted_input_formats` / `emitted_output_format`; the bridge resolves the transcoder at session open, not at build time. |
| **Language & script conventions** | Sarvam's `mode` (transcribe / translit / codemix / translate / verbatim) **changes the script the LLM sees** **[C]** | Mode is coupled to the prompt. Store mode+prompt as one versioned artifact per agent; changing mode silently changes the token distribution. |
| **Session lifetime** | OpenAI 60 min hard (HC-6); Sarvam 60 s *idle* per socket (HC-22); Exotel 60 min stream (HC-5) | Three different clocks. A `SessionLifecycleManager` owns all of them and initiates rollover; adapters expose `time_to_forced_close()`. |
| **Cost accounting** | OpenAI: token-based, 80× spread between cached ($0.40/1M) and fresh ($32/1M) audio input **[C]**. Sarvam: ₹30/hr STT + ₹30/10K chars TTS + per-token LLM **[C]** | Billing engine consumes a normalized `UsageEvent`, never a per-minute assumption. Prompt caching of the long system instruction is a first-order cost lever, not an optimization. |
| **Reconnection** | Not a documented feature on either provider | **Do not architect around a resume primitive.** Persist conversation items as they stream; on drop, open a fresh session and replay condensed context. |

---

## 4. Verified Version/Model Pins

| Component | Pin | Confidence | Note |
|---|---|---|---|
| Python | `>=3.11,<3.14` | **[A]** | langgraph & taskiq need ≥3.10; 3.14 support exists but is new |
| `langchain` | `==1.3.14` | **[C]** PyPI | pins langgraph <1.3 (HC-36) |
| `langchain-core` | `==1.5.1` | **[C]** PyPI | hard-depends on `langsmith` even when tracing is off |
| `langgraph` | `>=1.2.5,<1.3` — target **1.2.9** | **[C]** PyPI | **avoid 1.2.3 (reported yanked — [L], verify)** |
| `langgraph-checkpoint-postgres` | `==3.1.0` (2026-05-12) | **[C]** PyPI | requires `autocommit=True`, `row_factory=dict_row` on manual conns |
| `taskiq` | `==0.12.4` (2026-05-08) | **[C]** PyPI | OTel instrumentation since 0.12.0; queue metrics since 0.12.3 |
| `taskiq-redis` | latest (repo updated 2026-06-23) | **[C]** repo, **[?]** version | exact version unpinned — resolve at lock time |
| `clerk-backend-api` | **UNKNOWN** | **[?]** | PyPI page did not render; a search snippet claimed "6.0.1 as of June 2026" — **unverified**. Run `pip index versions clerk-backend-api` before pinning. |
| `svix` | latest | **[A]** | needed directly; unverified whether it is a transitive dep of clerk-backend-api |
| PostgreSQL | 17 (pgvector 0.8.0) or 18 (pgvector 0.8.1) on Neon | **[C]** | upstream pgvector is 0.8.5 — Neon lags. Iterative scans landed in 0.8.0, so the feature we need **is** present. |
| pgvector | `0.8.0` (PG14–17) / `0.8.1` (PG18) | **[C]** | test against 0.8.0 semantics; do not assume post-0.8.1 fixes |
| OpenAI realtime model | `gpt-realtime-2.1` (premium) / `gpt-realtime-2.1-mini` (default) | **[C]** | 128K ctx, 32K max out, `v1/realtime` only, knowledge cutoff 2024-09-30 |
| OpenAI realtime WS | `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1` + `Authorization: Bearer` (+ optional `OpenAI-Safety-Identifier`) | **[C]** | **no** `OpenAI-Beta` header (HC-16) |
| OpenAI transcription | `gpt-realtime-whisper` | **[L]** | for input transcription; $0.017/min |
| OpenAI embedding | `text-embedding-3-small`, `dimensions=1536` | **[C]** | $0.02/1M tok, 8192 max input |
| Sarvam STT | `saaras:v3`, mode `codemix` default | **[C]** | `saarika:v2.5` is legacy; 23 languages incl. hi-IN, te-IN |
| Sarvam TTS | `bulbul:v3` | **[C]** | 11 languages incl. te-IN; ₹30/10K chars is **beta pricing, can move** |
| Sarvam LLM | `sarvam-30b` (default) / `sarvam-105b` | **[C]** | `sarvam-m` is **removed**; OpenAI-compatible at `/v1/chat/completions` |
| Exotel API host | `api.in.exotel.com` (Mumbai) | **[C]** | keep as env var; SG is `api.exotel.com` |
| Exotel outbound | `POST /v1/Accounts/{sid}/Calls/connect`, form-encoded, PascalCase params | **[C] docs / [!] see §6a casing conflict** | |
| Exotel WhatsApp | `POST /v2/accounts/{sid}/messages` | **[C]** | same Basic auth as voice |

---

## 5. Chosen Defaults

| Decision | Choice | One-line justification |
|---|---|---|
| **Job queue** | **Taskiq** + `taskiq-redis` `RedisStreamBroker` + `RedisAsyncResultBackend` + `SmartRetryMiddleware` + `TaskiqScheduler` | Only natively-asyncio option that is actively released in 2026 (0.12.4, May 2026) with OTel built in and a real broker-abstraction exit to SQS/NATS/Kafka; ARQ is maintenance-only (HC-34) and Celery has no native async execution. **[C on facts, A on choice]** |
| **Queue ack mode** | `--ack-type when_executed` on Stream brokers only | PubSub/ListQueue brokers silently drop in-flight messages on worker crash (HC-35). |
| **Dead-letter** | Custom `TaskiqMiddleware` writing to a `dead_letter_jobs` table in Postgres | Taskiq has no DLQ — `SmartRetryMiddleware` only logs a warning on exhaustion **[C]**. ~1 day of work, budgeted. |
| **Scheduler HA** | Single replica + Postgres advisory-lock leader lease; `cron_offset='Asia/Kolkata'` | Taskiq docs are explicit: "Always run only one instance of the scheduler" **[C]** — two schedulers means a duplicate dial storm. IST at the schedule layer, not in job bodies. |
| **Checkpointer** | `InMemorySaver` for anything touching a live call, async-flushed to our own Postgres schema; `AsyncPostgresSaver` (v3.1.0) **only** for post-call and HITL graphs, with `durability='async'` or `'exit'` | HC-37: the `threading.Lock()` defect caps AsyncPostgresSaver at ~200 req/s per process. `durability='sync'` adds a Neon round-trip per superstep — unacceptable in-turn. |
| **Checkpoint hardening** | `LANGGRAPH_STRICT_MSGPACK=true` everywhere; `thread_id = f"{tenant_id}:{campaign_id}:{call_sid}"` (<255 chars); Store namespaces `(tenant_id, ...)` | HC-39 RCE surface on shared Postgres; thread_id column is length-limited **[C]**. |
| **Embedding model** | `text-embedding-3-small` @ **1536 dims**, stored as **`halfvec(1536)`** | Indexable within the 2000-dim HNSW cap (HC-24); 6.5× cheaper than 3-large; Neon benchmarks halfvec at 50% storage, 23% faster builds, equivalent recall/latency **[C]**. Store `model_id` + `dims` per row so a future re-embed can proceed tenant-by-tenant. |
| **Vector index** | **Tiered:** tenants <~10k chunks → no ANN index, exact seq scan + B-tree on tenant_id (~36 ms @10k **[C]**, 100% recall). Larger tenants → HNSW (`m=16`, `ef_construction=64`) with `SET LOCAL hnsw.iterative_scan='relaxed_order'` and raised `ef_search`. Table `PARTITION BY LIST(tenant_id)` with hash sub-partitioning to bound partition count. | HC-25 post-filter recall. pgvector's own guidance names partitioning (not partial indexes) as the answer for "many distinct filter values" **[C/L]**. Partitioning is near-impossible to retrofit onto a live vector table. |
| **Tenant isolation** | Postgres RLS (Neon RLS + Clerk JWKS) **for correctness** + LIST partitioning **for performance**. RLS does not rescue ANN recall — iterative scans still required. | Defense in depth; RLS predicates post-filter exactly like any other filter. **[C on Neon-Clerk RLS integration]** |
| **Tenant PK** | Internal UUID in an `organizations` table, with a unique `clerk_org_id` column — **never** Clerk's `org_id` as PK | Clerk's own guidance for users is "store the Clerk ID as a column" **[C]**; the org-specific version is my inference **[A]**. Telephony entities, Exotel subaccounts, billing ledgers, recordings and vector namespaces all need a key that survives an auth migration and outlives a deleted org (CDR retention). |
| **Connection pooling** | Two DSNs: `-pooler` (PgBouncer, transaction mode) for app traffic; **direct** for migrations, index builds, advisory locks, session `SET`. All vector search goes through one `vector_search()` helper that always opens a transaction and issues `SET LOCAL`. | HC-26. Without the single helper, raw ORM queries ship the default `ef_search=40` into production. |
| **Read routing** | Vector search → Neon read replica; OLTP writes → primary. Signal "indexing" in UI rather than promising instant KB availability. | Replicas share storage, spin up in seconds, are async/eventually-consistent **[C]**. |
| **Realtime model routing** | `gpt-realtime-2.1-mini` default; `gpt-realtime-2.1` per-agent opt-in. `reasoning_effort` constrained to `minimal`/`low`. | ~3× cost swing ($10 vs $32 per 1M audio-in) **[C]**; anything above `low` measurably delays first audio **[L]**. |
| **Turn detection default** | `semantic_vad`, `eagerness: low`; all VAD params exposed as per-agent config | Indian English/Hindi code-switching and deliberative phrasing get cut off by aggressive endpointing **[A]** — ops must tune per campaign without a deploy. |
| **Backend auth** | One FastAPI dependency → `authenticate_request(..., jwt_key=CLERK_JWT_KEY, authorized_parties=[...], accepts_token=['session_token'])` returning an internal `AuthContext` dataclass; custom claim extractor handling both `o` and flat shapes | `jwt_key` makes verification networkless — without it every request costs a JWKS round-trip from India to `api.clerk.com` **[C]**. HC-29 forces the custom extractor. |
| **Internal service auth** | Shared-secret/mTLS inside the VPC for east-west traffic; Clerk M2M (JWT format, short TTL) only at boundaries needing centralized machine identity | Clerk M2M is $0.001/creation **[C]**; a busy campaign minting tokens per call makes that non-trivial. JWT M2M tokens are **not revocable** — keep expiry in minutes. |
| **Observability** | OTel everywhere. `LANGSMITH_TRACING` unset; `LANGSMITH_OTEL_ENABLED=true` + `LANGSMITH_OTEL_ONLY=true` → own collector. Taskiq via `TaskiqInstrumentor().instrument()`. | Keeps Indian call transcripts and PII out of a US SaaS by default — material for DPDP posture **[C on the env-var mechanism]**. |
| **Durable call orchestration** | Explicit state machine in Postgres driven by short Taskiq jobs + webhook events. **Not** Temporal, **not** Hatchet, **not** a long-lived LangGraph run. | A dial→answer→turn-loop→post-call→retry-tomorrow flow spans minutes and external callbacks; that is not a queue job's unit of work. Escalate to Temporal only if this state machine becomes the dominant complexity. **[A]** |

---

## 6. UNKNOWNS / DECISION REQUIRED

> **Everything in this section is UNVERIFIED.** None of it may be treated as fact, quoted in a design doc, or used for capacity/cost/compliance planning without the confirmation named.

### 6a. Must confirm with provider / account team

**Exotel**
1. **Endpoint casing conflict.** Canonical v1 docs show `/v1/Accounts/{sid}/Calls/connect` with PascalCase params; the AgentStream developer guide renders `/v1/accounts/{sid}/calls/connect` lowercase. **These cannot both be right.** Verify against a live sandbox call before writing the client. Assume PascalCase.
2. **Exact sample-rate query param name.** Seen once as `?sample-rate=16000`, uncorroborated. Could be `sample_rate` / `samplerate`. Confirm, or read `media_format` back from the `start` event.
3. **Exact JSON shape Exotel expects for OUTBOUND media.** Docs say "same structure as incoming" but do not confirm whether `sequence_number`, `media.chunk`, `media.timestamp` are required or ignored, or whether `stream_sid` must be echoed. **Needs an empirical test call.**
4. **Whether the 320-byte / 3200-byte / 100000-byte chunk rules scale with sample rate**, or are absolute byte thresholds at all rates. This directly determines min-chunk latency at 24 kHz (§2).
5. **Codec conflict on the Legs API.** `start_stream` example shows `content_type: "audio/x-mulaw;rate=8000"`, contradicting slin everywhere else. Confirm accepted `content_type` values before building on the Legs path.
6. **Concurrency model.** "Unlimited concurrent calls per ExoPhone" appears **only in an Exotel marketing blog**, never in developer docs or a limits page. Actual provisioned concurrency, how it is purchased, per-account caps and burst behaviour must be confirmed **contractually**. Blocks capacity planning.
7. **Webhook source IP ranges** (unpublished; support-only) and the notification mechanism when they change. This is the *only* transport-level auth available (HC-10) — a hard dependency with no documented change process.
8. **Keepalive/ping behaviour and idle timeout on the media WebSocket.** Undocumented.
9. **Active Stream Monitoring API** — exact path, method, response schema unverified.
10. **Mid-call warm transfer to a human agent** — whether the Voicebot applet supports it natively, or it requires the Legs API / next-applet handoff.
11. **Pricing**: per-minute voice, AgentStream/streaming surcharge, ExoPhone rental, WhatsApp conversations. Nothing public.

**OpenAI**
12. **Concurrent-session limit for `gpt-realtime-2.1`.** None documented (HC-18). Confirm with sales whether concurrency is separately gated or purely TPM-derived. **Blocks any concurrency SLA.**
13. **Whether `temperature` still exists in the GA session object**, or is fully replaced by `reasoning_effort`.
14. **GA defaults for `server_vad`** (`threshold`, `prefix_padding_ms`, `silence_duration_ms`) and the semantics/default of `idle_timeout_ms`. **Do not hardcode the beta-era 0.5 / 300 / 200.**
15. **Whether `response.function_call_arguments.done` still exists** in the GA event set. Implement against `response.done`.
16. **Whether the sideband `?call_id=` WebSocket works for plain-WebSocket-originated sessions** or only WebRTC/SIP.
17. **Measured RTT from ap-south-1 to the nearest OpenAI Realtime edge.** Unmeasured; sits directly in the turn budget (§2).
18. **ZDR availability for the Realtime API specifically**, and any data-residency/processing-region guarantee.

**Sarvam**
19. **Recommended STT WebSocket chunk size and cadence** for 8 kHz telephony. Not documented anywhere. Must benchmark or ask.
20. **Any latency numbers at all.** No p50/p95 time-to-first-transcript or time-to-first-audio-byte is published; docs say only "milliseconds not seconds." **Must be measured in-house on Indian telephony audio before Sarvam goes anywhere near the critical path.**
21. **Max WebSocket session duration / total audio per connection** (only the ~60 s *idle* timeout is documented). Determines whether a 30-min call needs mid-call socket rotation.
22. **Whether the STT WS accepts binary frames** instead of base64-in-JSON (~33% bandwidth saving).
23. **Whether the 100-concurrent STT socket cap can be raised** by enterprise agreement (HC-21). This is the hard scaling ceiling.
24. **Per-speaker × per-language validity matrix for bulbul:v3** — not published; only `shubh` and `ishita` are demonstrated across all 11 languages. Validate every speaker/language pair empirically before exposing it in agent config.
25. **Written DPA / data-residency / retention / training-use statement.** `sarvam.ai/privacy-policy` returned **HTTP 403** and could not be fetched. SOC 2, ISO 27001, DPDP compliance and India-based infrastructure are **third-party/marketing claims only**. **Blocker for regulated verticals.**

**Clerk**
26. **Whether v2 tokens also emit flat `org_id`/`org_role` aliases.** Docs show only `o`; the Python SDK's own v2 path reads flat names. These are in direct tension (HC-29). **Resolve by decoding a real token from our instance and printing the claim set** before writing the extractor.
27. **The authoritative webhook event catalog** — Clerk deliberately does not publish it; read the Dashboard Event Catalog tab and pin the exact subscription list.
28. **Svix timestamp tolerance window and Clerk's retry schedule.** Needed for replay/dead-letter design.
29. **Default session token lifetime.** Docs say "determined by dashboard setting" with no default. The widely-cited 60 s is unconfirmed.
30. **M2M GA vs beta status** as of today, and whether machines can be scoped per-tenant.
31. **Exact latest `clerk-backend-api` version** (§4).
32. **India/APAC data residency for user PII.**

**Neon / Postgres**
33. **Measured RTT from Mumbai/Bengaluru/Delhi to `aws-ap-southeast-1`.** No Neon-published figures. Sets the retrieval budget.
34. **Whether Neon plans an India region.**
35. **Whether Neon's PgBouncer supports `SET LOCAL` for ALL pgvector GUCs** — confirmed only for `hnsw.ef_search`; unverified for `hnsw.iterative_scan`, `max_scan_tuples`, `scan_mem_multiplier`. **Hands-on test against `-pooler` required.**
36. **Read-replica limits on paid plans** (only Free = 3 is documented).
37. **Compute-hour pricing for an always-on instance** sized to hold the HNSW graph in RAM. TCO currently unquantified.

**LangGraph**
38. **Current status of issue #7259** — was open with PR #7269 linked; verify whether it landed in 1.2.7/1.2.8/1.2.9 **before sizing concurrency** (HC-37).
39. **The default value of `durability`** on `invoke`/`ainvoke`. The reference lists the three literals but states no default. Verify in source.
40. **Whether 1.2.3 was yanked** (reported via search summary only).
41. **`DeltaChannel` exact import path and API** — beta, changelog-only.
42. **Whether a single compiled graph is safe to share across concurrent asyncio tasks** (issue #4214 suggests problems with long-lived compiled graphs + async checkpointers). Compile-per-request vs compile-once must be settled by load test.
43. **Whether langgraph OSS emits any telemetry independent of `LANGSMITH_TRACING`.** Verify by network-egress test in a sealed container before claiming "no data leaves the process."
44. **Whether any official `langchain-sarvam` chat-model integration exists.** If not, write a custom `BaseChatModel` or call Sarvam directly.
45. **TTL/retention support on Store and checkpointer tables.** If none, we write the pruning job ourselves.

**Taskiq**
46. **`taskiq-sqs` production maturity** (7 stars) — ack semantics, FIFO, visibility-timeout mapping, and especially whether SQS's **15-minute max DelaySeconds** covers our campaign-retry deferrals. **Load-test before treating it as the migration target.**
47. **Ack/redelivery parity for the Kafka and NATS brokers.** Documented only for taskiq-redis.
48. **Throughput/latency of `RedisStreamBroker` at our campaign burst volume.** No official benchmark exists; "proven in demanding production environments" is marketing prose.

### 6b. Needs a human / business / legal decision

**L-1 — Data residency under the DPDP Act. THE gating decision.** Neon has no India region and the region is immutable (HC-27). OpenAI Realtime has no Indian processing region (HC-17). Sarvam's residency posture is unverifiable (§6a-25). **Legal must rule on whether call recordings, transcripts and caller PII may leave India for this use case.** If the answer is no: Neon is out (→ RDS/Aurora `ap-south-1`), and OpenAI Realtime as primary may be out entirely, inverting the whole architecture toward Sarvam-primary. **Every other decision in this brief is downstream of this one.**

**L-2 — Telugu, and code-switching quality.** Telugu is absent from the only documented OpenAI realtime language list (which belongs to `gpt-realtime-translate`, a *different* model). There is **no official speech-to-speech language list for `gpt-realtime-2.1` at all**, and **zero official documentation on Hinglish/Telugu-English code-switching** from either provider. Sarvam has a `codemix` mode but publishes no WER for code-switched audio. **Run our own eval set on real Indian telephony audio before promising any language.** This is the highest product risk in the brief.

**L-3 — Permitted calling window.** Could not be verified from any official Exotel page. A search summary suggested 8 AM–9 PM; some Indian regulations cite 9 AM–9 PM. Confirm with Exotel compliance and **treat it as a configurable rule, never a hardcoded constant.**

**L-4 — Whether DLT registration applies to VOICE**, not just SMS. All confirmed Exotel DLT documentation concerns SMS sender IDs and template scrubbing.

**L-5 — Whether Exotel performs server-side NCPR/DND scrubbing**, or whether it is entirely our responsibility. Materially changes whether we must integrate a DND scrubbing service ourselves. Not stated in any doc reviewed.

**L-6 — Consent/opt-in evidence retention policy.** Exotel contractually requires producing opt-in proof within 24 hours (HC-14). Legal must define what artifact counts, how long we retain it, and who is liable — us or the tenant — when a tenant uploads a list without consent.

**L-7 — Clerk plan tier.** $100/mo Enhanced B2B add-on if we need >10 custom roles, unlimited org members, or verified-domain auto-invitation (likely wanted by enterprise Indian customers with `@company.in` domains). Plus MRO overage at $1/mo each above 100.

**L-8 — Multilingual retrieval quality of OpenAI embeddings on Indic languages.** No official per-language benchmark exists. For an India-first product this is first-class risk. Bake off `text-embedding-3-small/large` against Indic-specialized alternatives on real transcript data **before locking the dimension into the schema** — dimension is baked into the column type, so changing it later is a full re-embed plus table rewrite of every tenant.

**L-9 — Whether Sarvam is fallback-only or a co-primary.** HC-21's 100-socket STT ceiling makes Sarvam unviable as primary at scale unless negotiated. If L-1 forces India-resident processing, this becomes urgent and commercial.

**L-10 — Dramatiq is dual LGPL-3.0/GPL-3.0.** If the job-queue decision is ever revisited, this needs legal review for a commercial closed-source platform. (Taskiq, ARQ, Celery, Temporal, Hatchet are permissive.)

---

## 7. Anti-facts

**Things that sound true, are widely repeated, or appear in our own research notes — but that could NOT be confirmed. Do not write these into any document as fact.**

1. **"3.2 KB = 100 ms of 8 kHz 16-bit mono PCM."** ✗ **Arithmetically false.** 8000 Hz × 16-bit × mono = 16,000 bytes/s, so 3200 bytes = **200 ms**, not 100 ms. The equivalence holds only at 16 kHz mono (or 8 kHz stereo). **Treat the BYTE thresholds (320-multiple / ≥3200 / ≤100000) as authoritative and the millisecond gloss as unreliable.** Confirm §6a-4 before building latency estimates on it.

2. **"Telephony needs no resampling because G.711 passes through OpenAI Realtime untouched."** ✗ True in general, **false on this stack.** Exotel's Voicebot applet emits raw/slin PCM, not G.711 (HC-1). The passthrough is unavailable. This claim appears in our own OpenAI research findings and would have produced a broken bridge.

3. **"Exotel supports unlimited concurrent calls per ExoPhone."** ✗ Marketing blog only. Absent from developer docs and any published limits page.

4. **"gpt-realtime-2.1 supports 70+ input and 13 output languages."** ✗ Those figures belong to **`gpt-realtime-translate`, a different model.** There is **no official speech-to-speech language list for `gpt-realtime-2.1`.**

5. **"OpenAI Realtime supports Telugu."** ✗ Unverified. Telugu is absent from the translate model's 13-language output set. The only Telugu evidence found was a third-party vendor (BolnaAI) press claim about the *translate* model's WER — not official, not speech-to-speech.

6. **"gpt-realtime-2.1 allows N concurrent sessions."** ✗ **No concurrency limit is documented at any tier.** Only RPM/TPM. Any number you have seen is invented.

7. **"Sarvam is SOC 2 Type II, ISO 27001, DPDP-compliant, and India-resident."** ✗ Marketing/third-party summaries only. The privacy policy returned **HTTP 403** and could not be fetched. Nothing about training-data use or retention is confirmed.

8. **"Sarvam's STT WebSocket gives partial transcripts."** ✗ Explicitly the opposite (HC-20).

9. **"The Exotel sample-rate parameter is `?sample-rate=16000`."** ✗ Seen exactly once, uncorroborated in the applet reference.

10. **"Exotel's Legs API streams mu-law."** ✗ One doc example shows `audio/x-mulaw;rate=8000`, contradicting slin everywhere else. Likely a doc error; unresolved.

11. **"Indian calling hours are 9 AM–9 PM."** ✗ Not stated on any Exotel page reviewed. Two different windows appear in secondary sources.

12. **"Clerk v2 session tokens include flat `org_id`/`org_role` for backward compatibility."** ✗ Docs show only the nested `o` claim; the Python SDK reads flat names. **Direct tension, unresolved.** Assume `o` is authoritative.

13. **"Clerk session tokens default to a 60-second lifetime."** ✗ Widely cited, not confirmed. Docs say it is a dashboard setting with no stated default.

14. **"The Clerk Python SDK has a `has()` helper like the JS SDK."** ✗ Not found in the source. Assume you implement permission checking yourself.

15. **"`convert_to_openai_tool()` produces a Realtime-compatible tool schema."** ✗ It returns the **nested** Chat-Completions shape; Realtime requires flat (HC-19). Silent failure.

16. **"LangGraph adds ~2 ms per node / 50–100 ms for complex workflows."** ✗ Third-party blogs, not LangChain docs. **No official latency benchmark exists.** Measure your own before allowing any graph step near a live turn.

17. **"Shortened `text-embedding-3-large` (256/1024 dims) outperforms `ada-002` at 1536."** ✗ The OpenAI announcement blog returned **HTTP 403**; the MTEB comparison is unverified here.

18. **"Neon supports ap-south-1 / Mumbai."** ✗ It does not (HC-27).

19. **"ARQ is the standard async job queue for FastAPI."** ✗ It is in **maintenance-only mode** by maintainer statement (HC-34), with no DLQ and no OTel story.

20. **"Celery can run `async def` tasks natively."** ✗ Nothing in the 5.6 release notes or Tasks guide supports this. A third-party `celery-asyncio` exists but is unaffiliated and pre-release.

21. **"DLT registration is required for outbound voice."** ✗ All confirmed Exotel DLT documentation concerns **SMS** sender IDs and template scrubbing. Unverified for voice.

22. **"Exotel scrubs NCPR/DND server-side for us."** ✗ Not stated anywhere. Assume it is our responsibility until confirmed.

23. **"A Neon read replica gives read-after-write consistency."** ✗ Replicas are explicitly **asynchronous / eventually consistent.**

24. **"pgvector's partial-index-per-tenant pattern scales to a multi-tenant platform."** ✗ pgvector names partial indexes as the answer for **few** distinct filter values and partitioning for **many**. Thousands of HNSW indexes means unbounded catalog bloat.

25. **"LangSmith self-hosting is available to us."** ✗ Enterprise-plan add-on requiring a license key from a LangChain rep; Docker self-hosting is deprecated in favour of Kubernetes/Helm. Assume unavailable and design around OTLP-to-own-collector.