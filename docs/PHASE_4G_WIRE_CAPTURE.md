# Phase 4G — the Exotel wire capture

> **Status: BLOCKED, on external input.** (The D-8 gate renderer draws the same
> distinction between `BLOCK` and `FAIL`: no code change closes this, so it is a queue
> waiting on a person rather than a defect.)
>
> Not started, and **not startable by engineering alone.** It needs an Exotel sandbox
> account, credentials, and a consented internal test number. Nothing in this repository
> can substitute for it, and nothing in this repository pretends to.

The rest of Phase 4 is implemented and green. This is the one remaining Phase-4
deliverable — ROADMAP's sixth, *"one `live`-marked wire-capture spike"* — and the only
thing that can turn the assumptions below into facts.

> **On naming.** `4B`—`4G` were working labels from an implementation session and are
> **not repository phase names**. The roadmap has Phase 4 with six deliverables; this
> document covers the sixth. The file name is kept only because links point at it.

---

## Why this exists

Four things about Exotel's media protocol are **documented ambiguously or not at all**,
and every one of them is load-bearing:

| # | Question | What it blocks | Source |
|---|---|---|---|
| 1 | The exact JSON shape Exotel expects for **outbound** media. Docs say "same structure as incoming" but do not confirm whether `sequence_number`, `media.chunk` and `media.timestamp` are required or ignored, or whether `stream_sid` must be echoed. | The outbound encoder. It cannot be *known* correct without this. | [§6a-3](research/PROVIDER_CONSTRAINTS.md) |
| 2 | The exact **sample-rate query parameter name** on the Voicebot applet URL. Seen once as `?sample-rate=16000`, uncorroborated; could be `sample_rate` or `samplerate`. | Every call at a non-default rate. | [§6a-2](research/PROVIDER_CONSTRAINTS.md), anti-fact #9 |
| 3 | Whether the **320 / 3200 / 100000-byte chunk rules scale with sample rate**, or are absolute byte thresholds at every rate. | The minimum-chunk latency figures — and therefore **the 24 kHz default itself** ([ADR-003](DECISIONS/ADR-003-audio-transport-and-sample-rate.md)). | [§6a-4](research/PROVIDER_CONSTRAINTS.md) |
| 4 | **Endpoint casing.** Canonical v1 docs show `/v1/Accounts/{sid}/Calls/connect` (PascalCase); the AgentStream developer guide renders it lowercase. These cannot both be right. | The REST client, Phase 8. | [§6a-1](research/PROVIDER_CONSTRAINTS.md) |

A fifth thing the capture settles for free: the **real message cadence**. HC-1 documents
10–20 messages/second/direction, implying 50–100 ms of audio per message, and separately
records a broader 20–100 ms envelope. The trace shows what actually arrives.

## Where the assumptions live right now

**All four are fields on one frozen dataclass**, `ExotelDialect`, in
[`rn_providers/telephony/exotel.py`](../packages/providers/src/rn_providers/telephony/exotel.py),
and the instance in force is named `ASSUMED_DIALECT` so it cannot be mistaken for a
verified one. Nothing else in the codebase branches on an Exotel quirk.

That is deliberate: **settling all four is editing one dataclass and re-running the
tests.** It is not a refactor. `grep -rn ASSUMED_DIALECT` finds everything that currently
rests on guesswork.

`exotel_chunk_policy()` additionally *refuses* a dialect claiming the thresholds scale
with rate (question 3), because no scaling rule is documented and none has been measured
— a flag that says "settled" has to arrive with the measured rule beside it.

## What is needed from the business

1. An **Exotel sandbox account** with AgentStream / Voicebot applet access.
2. **Credentials** — `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`. Placeholders
   already exist in `.env.example`; they go in a local `.env`, never in the repository.
3. **One consented internal test number.** CLAUDE.md rule 8: no real customer data, and the
   number must be on the consented list.
4. Approval to place **one short call** — a minute or two, negligible cost, but it is a real
   call to a real network and that is a decision, not an engineering step.

## What engineering does once those exist

1. Write a `live`-marked spike that connects the Voicebot applet to a local WebSocket, logs
   **every frame on both legs verbatim**, and plays a few seconds of known audio back.
   Three guards apply and none may be removed: `addopts` already deselects `live`, CI runs
   without provider credentials, and `live` additionally requires `RN_LIVE_TESTS=1` plus a
   number on the consented list.
2. Run it at **8000 and at 24000** — question 3 cannot be answered at one rate.
3. Redact phone numbers at capture time. Commit the trace to
   `tests/fixtures/telephony/` with a README stating the date, the account, the rate and
   the Exotel API version it was taken against.
4. Update `ExotelDialect`'s defaults to the observed shape. Expect this to be a handful of
   booleans and one string.
5. Point `FakeTelephonyProvider` at the captured trace instead of its hand-authored tape,
   and re-run the Phase-4 suite. **Any test that fails at this point is a genuine finding** — it is the
   moment the assumptions are tested, and it is the entire reason the fake audits rather
   than accepts.
6. Update [PROVIDER_CONSTRAINTS §6a](research/PROVIDER_CONSTRAINTS.md) items 1–4 with the
   answers and upgrade their confidence tags from **[A]** to **[C]**, with the capture date.
7. Update [REALTIME_VOICE §13](REALTIME_VOICE.md) and, **if question 3 comes back "they
   scale"**, revisit ADR-003's latency table and the 24 kHz default with it.

## What must not happen

- **Do not fabricate a capture.** A hand-authored fixture placed in `tests/fixtures/telephony/`
  and described as captured would make every downstream confidence tag a lie, and the tags
  are the thing that keeps [PROVIDER_CONSTRAINTS](research/PROVIDER_CONSTRAINTS.md) worth
  reading.
- **Do not mark §6a items 1–4 confirmed** on the strength of a passing offline test. The
  offline suite proves the bridge obeys the rules we *believe* apply. It cannot prove the
  rules.
- **Do not remove any of the three `live` guards** to make the spike easier to run.

## What Phase 4 can honestly claim without it

The byte pipeline is built, complete and tested: transcoding at all three rates in both
directions with byte-exact goldens and an anti-aliasing floor, chunk alignment over
arbitrary delta sizes, playback accounting reconciled against mark echoes, barge-in as one
function with one call site, and a full offline call simulation with zero network access.

What it cannot claim is that **the frames it writes are the frames Exotel wants**. Phase 5
does not depend on that — it is a realtime-voice prototype with no telephony — so this
blocker does not sit on Phase 5's critical path. It sits on **Phase 8**, the first real
phone call, and it should be closed well before then.
