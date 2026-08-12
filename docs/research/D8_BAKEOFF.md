# D-8 — the embedding bake-off

> **Status:** Stage 1 complete, plus the corpus workflow, the **official Rise Next source material** (supplied 2026-07-30) and its full decomposition. The corpus builds: **143 passages, 804 queries** across all 8 subsets, every passage source-grounded or deliberately adversarial. **Human review is complete** — all 8 subsets, all 101 templates, all 76 spot checks, approved by the Rise Next team on 2026-08-11. **No paid call has been made. No model has been chosen. D-8 is OPEN**, and **two** blocking quality gates remain — `adversarial_present` and `size`, both closed by a human supplying something, not by code. See §11.
> **Scope:** how open decision **D-8** gets answered by measurement: the dataset, the candidates, the metrics, the gates fixed in advance, and what still blocks a decision.
> **Companions:** [../DECISIONS/ADR-010-defer-vector-storage-layout.md](../DECISIONS/ADR-010-defer-vector-storage-layout.md) (why it is deferred) · [PROVIDER_CONSTRAINTS.md](PROVIDER_CONSTRAINTS.md) (L-8, HC-24, HC-25, anti-fact 17) · [../DATA_MODEL.md](../DATA_MODEL.md) §6–7 · [../../PRD.md](../../PRD.md) §12
> **Code:** `tests/d8_bakeoff/`

---

## 1. What D-8 has to answer

From [ADR-010](../DECISIONS/ADR-010-defer-vector-storage-layout.md), six questions:

| # | Question | Answered by |
|---|---|---|
| 1 | Which embedding model? | this bake-off |
| 2 | Which width? | this bake-off, plus a measured recall-vs-cost curve |
| 3 | `vector` or `halfvec`? | Stage 2 — needs a Postgres column to measure storage and recall delta |
| 4 | Exact scan or ANN? | Stage 2 — needs measured p95 latency at realistic corpus size |
| 5 | Partition or not? | only against a measured planner problem at the real tenant count |
| 6 | Coexistence mechanism? | falls out of the width |

Only questions 1 and 2 are answerable by this harness. **Three of the six need a schema
that does not exist yet**, which is why the harness reports those gates as
`NOT EVALUATED` rather than passing them by omission.

## 2. Why it cannot be read from anywhere

- **L-8** — no official per-language benchmark exists for OpenAI embeddings on Indic
  languages. This is an India-first product whose corpus is English/Hindi/Telugu and
  code-mixed.
- **Anti-fact 17** — the comparison usually cited to justify a shortened
  `text-embedding-3-large` is **unverified**; the source returned HTTP 403.
- **HC-24** — HNSW caps `vector` at 2000 dims and `halfvec` at 4000. This binds only
  above 2000, i.e. only for `3-large` at its native 3072.
- Nothing published anywhere covers the case that actually matters here:
  **cross-script retrieval** — a Devanagari query against an English passage, a
  romanised Hindi query against a Devanagari one.

## 3. The dataset

`tests/d8_bakeoff/data/{corpus,queries}.yaml`.

### Reporting subsets

Every metric is reported **per subset**. The pooled average is computed and is the
number least worth looking at: a candidate can pool to 0.92 while scoring 0.55 on
Telugu, and shipping that is how an India-first product gets a language wrong.

| Subset | What it tests |
|---|---|
| `en` | baseline |
| `hi-deva` | Hindi in Devanagari |
| `hi-latn` | romanised Hindi — real, because Sarvam's `mode` changes the script the LLM sees |
| `te-telu` | Telugu script |
| `te-latn` | romanised Telugu |
| `codemix-en-hi` | switch inside one utterance |
| `codemix-en-te` | the least documented case |
| `cross-script` | **query in one script, gold passages in another** |

### Graded relevance

`2` = fully answers on its own. `1` = partially answers, or answers in a different
language than asked. Omission = `0`. The gradation is what makes nDCG mean anything —
binary judgements would score "returned the right topic in the wrong language"
identically to "returned nothing".

### Current size vs target

| | seed only | + Rise Next material | + full decomposition | target |
|---|---|---|---|---|
| passages | 18 | 67 | **143** | 400–600 |
| queries | 26 | 423 | **804** | 200–250 |

Queries are 3× past target. Passages are at 24% of it — and the honest reading of that
number is narrower than it looks.

**A corrected claim.** An earlier version of this document argued that a 67-passage corpus
would saturate at k=8 and fail to discriminate between candidates. The free offline run
disproves it. Measured `answerability@8` on the pre-decomposition corpus:

| | `en` | `hi-deva` | `te-telu` | `cross-script` |
|---|---|---|---|---|
| deterministic fake | 0.622 | 0.290 | 0.267 | 0.258 |
| lexical trigram | 0.523 | 0.161 | 0.133 | 0.065 |

Nothing near ceiling, every subset far below its G1 gate, and the two baselines separate
cleanly — by 4× on cross-script. Retrieval difficulty here is driven by the query↔gold
semantic gap (a Devanagari query against English gold), not by corpus size. **`size` is
failing against a threshold that was estimated, never measured**; it is not evidence the
benchmark cannot work. The `TARGET_PASSAGES = 600` constant is left in place deliberately —
lowering a pre-registered gate after seeing results is rationalisation — but what it should
be replaced with is a *measured* criterion, and that decision is open (§11).

Note both baselines are lexical in nature (the "fake" embedder is character-trigram
hashing), so the low Indic scores show the corpus contains real difficulty; they do **not**
predict a real model's score. That is what the paid run measures.

### Decomposition: 67 → 143 passages, no new facts

Every passage below traces to `risenext.yaml`. The transform is splitting and joining, never
elaboration — where the supplied material is silent, the corpus is silent.

| Category | Before | After | What changed |
|---|---|---|---|
| **capability atoms** | 0 | **69** | the 69 named sub-services, one passage each |
| services (description + capabilities) | 14 | 14 | unchanged |
| policies | 2 | 11 | the never-promise list split into its 9 clauses + overview |
| company | 2 | 5 | the profile paragraph's 3 distinct claims + mission + full text |
| business process | 1 | 4 | the 11 stages grouped into pre-sales / delivery / launch |
| financing disclaimer | 1 | 3 | not-a-lender / what it does do / never promise approval |
| pricing policy | 2 | 3 | customised / the 5 quotation factors / approximate-budget |
| faqs · industries · technology | 14 | 14 | unchanged — cannot expand without new answers |
| adversarial | 13 | 20 | + 7 capability-misattribution distractors |
| seed (invented) | 18 | **0** | retired |

**The capability atoms are the substance of the expansion**, and they change the retrieval
task rather than just the count. Before, all 14 of Technology Solutions' sub-services lived
in one passage, so *"do you build e-commerce platforms?"* and *"do you do cloud deployment?"*
had identical gold and were the same retrieval problem. They also bring hard negatives the
source supplies for free, because it puts confusable names in different services:

```
Documentation [admin]                ~  Documentation Support [loans]
Customer Support Automation [AI]     ~  Customer Support Operations [admin]
HR Management Systems [technology]   ~  Plot Management Systems [real estate]
Corporate Websites [technology]      ~  Real Estate Websites [real estate]
```

**What could not be decomposed, and why.** The 33 named technologies were left as 6 grouped
passages: `"Rise Next builds software using React."` and `"…using Next.js."` differ by one
noun in an otherwise identical sentence, and 33 of them would fail `no_passage_duplication`
— correctly, because they would report as 33 passages while measuring 6. The same argument
retired the 12 industries as 1 passage rather than 12.

**What the decomposition cost, and how it was caught.** The first version carried each
service's full description into every capability passage, so that a four-word chunk had
something to embed. The shared text then dominated the trigrams and `no_passage_duplication`
failed with 63 near-identical pairs — every capability of a service looking like every other
and like the service itself. Padding a short fact with shared text does not enrich it; the
passages are now a name and its owner, which is what the source actually supplies.

### Where the corpus comes from

`source/risenext.yaml` holds the official Rise Next business material supplied by the Rise
Next team on 2026-07-30, structured into ten categories. Everything in it is
`source_grounded` with a `source_reference` naming the section it came from, and every entry
records `reviewed_by: Rise Next team`.

**The classification that does the most work is `authority`, and it has four values because
the material needed four.** The instinct to sort content into "price / not price" produces
two wrong answers:

| Authority | Example from the material | Retrievable? |
|---|---|---|
| `descriptive` | services, industries, technologies, process, FAQs | **yes** — this is what a knowledge base is for |
| `policy` | the pricing policy, the never-promise list, the not-a-lender disclaimer, the uncertainty fallback | **yes, deliberately** |
| `authoritative` | a price *figure*, an availability slot, an id | **never** — a typed tool reads a system of record |
| `structural` | the lead-capture field list, the closing script | **never**, and never even a passage |

The `policy` row is the one worth arguing about. Hiding anything price-related from
retrieval feels safe and is backwards: the danger is not that retrieval returns *"every
project is quoted individually"* — that **is** the correct answer — it is that retrieval
returns a **number**. Withholding the policy leaves a pricing question with no correct
passage at all, which is precisely the state in which a model invents one. The schema
enforces both directions: `authoritative`/`structural` may not be RAG-eligible, and
`descriptive`/`policy` may not be marked `never_rag`.

`structural` exists because CRM field definitions are schema, not content. A caller never
asks what fields the lead form has, so a passage of them adds noise without adding a test —
and a naive "never_rag ⇒ it's a price trap" rule would have labelled the lead-capture field
list as a price.

### Four tiers, kept apart

Growth does not mean "more of the same". `dataset.MaterialTier` distinguishes four kinds of
material, because "synthetic" covers two very different things — a paraphrase mechanically
derived from a reviewed fact, and a passage somebody made up:

| Tier | Origin | May support a decision |
|---|---|---|
| `source_grounded` | supplied business content, with a `source_reference` | yes |
| `controlled_synthetic` | a documented deterministic transform of the above | yes, via `derived_from` |
| `adversarial` | deliberate hard cases | yes |
| `non_decision_synthetic` | invented outright | **never**, whatever its review status |

The tier check runs **before** the review-status check in `Dataset.readiness`, so a review
pass cannot launder invented material into evidence. The default when a tier is unstated is
the safest one, so a missing tier fails safe rather than silently inflating the corpus.

### Hard negatives

`PassageRole` marks passages that must **never** be gold, enforced at load:

| Role | Models the failure | Present? |
|---|---|---|
| `distractor` | a similar service, the right company with a capability it lacks, or a real capability attributed to the wrong service | **17** |
| `stale` | a superseded version of live content — what `documents.status = 'active'` exists to prevent | **0 — gap** |
| `injection` | a tenant's brochure containing text addressed to the model | **3** |
| `price_bearing` | a chunk carrying a money-shaped number | **0 — by design** |

The **capability-misattribution** distractor is the hardest one the source can produce and
it costs nothing to build, because the material supplies both halves: *Plot Management
Systems* is real and belongs to Real Estate Solutions, so a passage claiming it is part of
Technology Solutions is a sentence in which every noun is genuine and only the attribution
is false. A distractor built from invented content gives itself away by being odd; this one
is detectable only by knowing which service owns which capability — which is exactly what
has to be right when a caller asks "who does plot management?" and the answer routes a lead.

**Two of the four cannot be manufactured, and the two zeros mean opposite things.**

`stale` is a **real gap**. It needs genuinely superseded text — an old service description, a
withdrawn offer — and none was supplied. Inventing "old" text would produce a semantic
distractor wearing a stale label, which measures the wrong thing while reporting the right
count. So `adversarial_present` fails, and the failure message says what would close it.

`price_bearing` is **zero on purpose, and that is better than a pass.** A price-bearing
passage can only be built from a price the business actually supplied, and the Rise Next
material contains none — it quotes customised pricing. Requiring the role would therefore
create pressure to invent a figure in order to satisfy a gate whose entire purpose is to keep
invented figures out. `PRICE_BEARING` was removed from `MIN_ADVERSARIAL_ROLES` and the
guarantee moved to a stronger gate instead — see below.

### How the corpus grows

```
source/risenext.yaml        SUPPLIED — Rise Next material, 2026-07-30, reviewed by the team
                            41 facts + 7 services + 69 named capabilities
source/phrasebook.yaml      101 templates + 7 service names in 3 scripts
                            ⛔ every entry review_status: pending — authored by the
                               assistant, NOT reviewed by any native speaker
             │
             ▼
    corpus/build.py          deterministic, offline, no LLM, no network
             │
             ├──► data/generated_corpus.yaml      143 passages
             └──► data/generated_queries.yaml     804 queries
```

**The multiplication is the point**, over three fan-out axes: capability-scoped templates ×
69 sub-services, service-scoped × 7 services, business-wide × 1, plus mechanical typo
variants on Latin-script text. 101 reviewable templates become 804 judged queries — and a
reviewer validates the 101, not the 804.

**The capability fan-out is deliberately capped**, and the reason is review capacity rather
than cost. `en` fans out over all 69 capabilities; every other subset takes a deterministic
*strided* sample (20–24), which walks the whole list so every service is represented — a
truncated `[:20]` would have tested Technology Solutions and nothing else. Fanning all 69
across all 8 subsets would give ~1,600 queries against a 250 target, and the 10%
individual-review floor scales with query count, so it would roughly quadruple the human
work for a metric already far past target. Capabilities without a query in a given language
are not waste: they are realistic distractors, which is what makes the task hard.

**Every generated row carries provenance back to what produced it**, which is what stops a
paraphrase becoming an independent business fact:

| Field | Holds | Why it exists |
|---|---|---|
| `source_id` | the intake entry — a fact id, a service id, a template id | seven phrasings of one template share one id, so a subset cannot look like broad coverage by rephrasing |
| `source_version` | `source_material_version` / `phrasebook_version` | a corpus built against v1 and reviewed against v2 are about different text |
| `source_type` | the intake section: `policies`, `faqs`, `pricing_policy`, … | lets a gate assert "a pricing answer comes from the pricing section" for *any* tenant's file |
| `generated_from` | the transform: `service_capabilities`, `typo:transpose`, `negative:stale` | reproducibility — an engineer re-runs this; `derived_from` is what an *auditor* reads |
| `human_review_required` | whether anyone has vouched | **derived** from the validation status, never stated, so the two cannot drift |

`human_review_required` is `true` on everything Indic today, and the loader refuses a row
that declares `false` while its validation is `synthetic_unreviewed`. That is DECISION 6 as
arithmetic rather than as a rule someone has to remember.

**What is generated mechanically and what is not** — the line is *mechanical where the
transform genuinely is, human where it is a judgement about how people speak*:

| Axis | Generated | Human |
|---|---|---|
| typo / transcription-noise variants | **yes** | — |
| cross-script gold pairing | **yes** | the query text |
| hard negatives | **yes** (field swaps and template fills) | the `near_duplicate_of` and `superseded` declarations |
| English paraphrase / terse / conversational | — | **yes**, as templates |
| Hindi / Telugu, any script | — | **yes**, as templates |
| code-mix | — | **yes** |

Generating Hindi paraphrases by rule would produce textbook Hindi nobody says, and the
benchmark would then measure how well a model handles textbook Hindi.

### LLM-assisted expansion — a proposal, not a plan, and nothing has run

**No LLM has been called and no paid API has been touched.** The Indic templates currently in
`phrasebook.yaml` were authored by the assistant directly, in-session, at no API cost —
which is a *different* thing from LLM-assisted generation as a pipeline, and both are
unreviewed either way. If the volume of Indic phrasings ever needs to grow beyond what a
person will hand-write, here is what would be proposed, so the decision can be made on
numbers rather than in the moment:

**Process.** Templates only, never passages. A passage is a claim about the business and an
LLM must never author one. For each `(subset, intent, style)` cell, ask for *k* natural
caller phrasings, given the English template and the service vocabulary as context. Output
lands in `phrasebook.yaml` as `review_status: pending`, `generated_from:
llm:<model>:<date>`, and enters exactly the same review bundle as everything else. **The
review requirement does not relax** — an LLM-drafted Telugu sentence is precisely the kind of
grammatically-correct-but-nobody-says-that text this whole exercise exists to catch, and the
model that wrote it cannot vouch for it.

**Cost.** ~8 subsets × ~12 intents × ~3 styles ≈ 290 cells; at ~400 input / ~200 output
tokens each ≈ **116k input / 58k output tokens**, one-off. Well under a dollar at current
Claude or GPT pricing. **Cost is not the constraint here — review capacity is.** 290 new
templates is 290 more rows a native speaker has to read, and the binding resource is that
person's time, not tokens.

**Recommendation: don't, yet.** The corpus is short on *passages*, not queries — queries are
already 804 against a 250 target. Generating more phrasings makes the review queue longer
while making the benchmark no more discriminating, and review is now *complete*, so new
templates would reopen a closed gate. There is a second, sharper cost since 2026-08-11: the
`spot_check` floor is met with **zero margin** in every subset, so any added query also
requires added individually-judged queries in the same pass. Revisit only if the per-subset
scores turn out to be phrasing-limited.

### The four business-constraint gates

These encode business rules rather than corpus statistics, and their failure would be
invisible in a score.

| Gate | Asserts | Now |
|---|---|---|
| `no_numeric_prices_in_corpus` | **no passage anywhere carries a money-shaped number** | PASS |
| `pricing_gold_is_policy` | every pricing query's gold includes the pricing policy | PASS — 119 queries |
| `lending_gold_is_disclaimer` | every lending query's gold includes the not-a-lender disclaimer | PASS — 15 queries |
| `adversarial_intents_present` | every adversarial intent appears in at least one **Indic** subset | PASS |
| `no_passage_duplication` | no two passages exceed 0.90 trigram overlap | PASS — 143 compared |

`no_passage_duplication` arrived with the capability decomposition and immediately earned
its place (see above). Splitting one service into fifteen passages is the cheapest way to
make a corpus *look* larger, and the failure is worse than a miscount: two duplicate
passages give retrieval two equally correct answers where the gold set names one, so a
candidate returning the unnamed twin is marked wrong for being right, and which twin it
prefers is arbitrary — a coin flip no amount of averaging removes.

**Topic-scoped gold.** Splitting the never-promise list into nine clauses briefly made every
guarantee and pricing question's gold set 13–14 passages wide, which over a 143-passage
corpus turns `answerability@8` into a formality every candidate passes. Facts and templates
now carry a `topic`, so *"can you guarantee first page ranking on Google"* is gold-matched to
the ranking clause alone, and the eight unrelated clauses become hard negatives — strictly
better than being spurious gold. A template that names nothing specific carries no topic and
falls back to the overview list, which is the honest answer to a generic question. Gold-set
sizes now run 1–6, median 3.

**`no_numeric_prices_in_corpus` is strictly stronger than the older
`no_pricing_as_rag_truth`,** which permits priced passages provided they are labelled traps
and gold for nothing. The difference is worth stating: a labelled trap tests that a
*candidate* ranks it low, but it still leaves a real number in a corpus that someone can
later relabel, promote, or copy into a fixture. Zero numbers means a pricing query cannot be
answered with a figure **by construction** — there is nothing to rank. The gate is therefore
also a guard on the intake file: adding a price to `risenext.yaml` fails the build, correctly,
because a price belongs in `service_prices` behind `get_service_pricing` ([PRD §6.5](../../PRD.md)).

**What these gates can and cannot claim.** D-8 runs no model. It cannot show that an agent
declines to quote a price or refuses to present Rise Next as a lender — it shows that the
*constraint is retrievable* from the question, in every language. That is the precondition,
not the behaviour: a model cannot decline from a policy it was never given. The behavioural
half — "the agent did not invent a number", "the agent did not claim to lend" — is an
`agent_eval` case and is deliberately not this suite's job. Do not read a green
`pricing_gold_is_policy` as evidence that price hallucination has been tested end to end.

### The loan constraint, specifically

Rise Next assists with documentation, application processes and coordination with banks and
financial institutions, and **is not a lender**. It is the highest-priority business
constraint in the supplied material, so it is carried three ways: as its own
`financing_disclaimer` section (grade-2 gold), as the customer-facing FAQ that says "We are
not a lender" in the caller's own words (grade-1 gold), and as a `lending` intent with
templates in every subset including cross-script.

## 4. Human validation status — the DECISION 6 mechanism

Per-item `validation` status, propagated to a per-subset readiness computation. A subset
is *review-complete* only when every passage and every query in it is reviewed; a report
is decision-grade only when every subset it scored is review-complete. **"Do not claim
synthetic Hindi or Telugu is validated" is therefore arithmetic the harness performs,
not an instruction someone has to remember.**

| Subset | Queries | Reviewed | Templates | Spot checks | Status |
|---|---|---|---|---|---|
| `en` | 248 | 248 | 20 | 24 | ✅ review-complete |
| `hi-deva` | 49 | 49 | 12 | 4 | ✅ review-complete |
| `hi-latn` | 104 | 104 | 11 | 10 | ✅ review-complete |
| `te-telu` | 50 | 50 | 12 | 5 | ✅ review-complete |
| `te-latn` | 104 | 104 | 11 | 10 | ✅ review-complete |
| `codemix-en-hi` | 104 | 104 | 11 | 10 | ✅ review-complete |
| `codemix-en-te` | 106 | 106 | 12 | 10 | ✅ review-complete |
| `cross-script` | 39 | 39 | 12 | 3 | ✅ review-complete |

**Review is complete.** All 101 templates, all 7 service-name translation sets and all 76
spot checks were approved by the Rise Next team on 2026-08-11, in two passes: `hi-deva` and
the service names first, the remaining 89 templates and the 76 spot checks in the
consolidated review recorded in `review/HUMAN_REVIEW.md`. Every subset is review-complete
and `dataset.is_review_complete` is true.

**`hi-deva` needed both halves, and that is the lesson.** Approving its 12 templates left 21
of 49 queries unreviewed: three are service-scoped and fill their slot from `service_names`,
which were still pending, and a generated query is only as reviewed as its **weakest** input.
Approving the 7 service-name sets closed it. A subset is not done when its templates are
done.

**Two templates were corrected rather than approved as written**, and both corrections were
then explicitly approved: `hi-deva-industries` in the first pass and `xs-deva-out-of-scope`
in the consolidated review. Both carry `native_reviewed` under the same rule — the reviewer
supplied the replacement wording *as their approved form*, which is a judgement on the final
text. `ReviewDecision.resulting_state` maps a bare `needs_edit` to `pending`, and that is the
right default for the case this is not: a correction nobody has since signed off. Applying it
here would have recorded a review as absent when a named human had given one.

Two judgements a future reviewer may still overturn, recorded because they were the
highest-risk calls in the authoring: writing "Technology Solutions" as टेक्नोलॉजी सॉल्यूशंस
rather than translating it, and **keeping capability names in English inside Devanagari and
Telugu frames** — क्या आप E-Commerce Platforms बनाते हैं. Both are now approved; both remain
the kind of claim only a speaker can settle, so they are named here rather than buried.

**Attribution is enforced, not trusted.** Every approval carries `reviewed_by` and
`reviewed_on`; a claim of review with no named reviewer is refused at load, and
`is_placeholder_reviewer` refuses `claude`, `assistant`, `ai`, `llm`, `gpt`, `gemini`,
`copilot` and versioned forms like `GPT-4` or `Claude 3.5` — **a corpus reviewed by the thing
that generated it is not reviewed.** It matches per word rather than by substring, so `Nair`,
`Rai` and `Vaidya` are accepted.

Reviewing 101 templates validated 804 queries; the 10% spot-check floor in `quality.py` is
what keeps that an efficiency rather than a loophole, and it is met in every subset — **with
zero margin.** Each subset sits exactly at `max(1, int(len(queries) * 0.10))`, so any corpus
growth re-opens the gate. Adding queries means adding spot checks in the same pass.

**Still outstanding, and not a language question:** *decomposition fidelity*. The 143 passages
are splits and joins of the supplied text; "did any claim drift in the splitting?" is a
question only the source's owner can answer, and it has not been asked.

**Also unvalidated, and tracked here rather than buried:** the Hindi and Telugu
**injection-detection patterns** in `rn_domain.sanitisation` are synthetic and
unreviewed. Their recall is unknown and must not be quoted as coverage. They belong in
the same review pass.

## 5. Candidates

`tests/d8_bakeoff/candidates.py`. **Every provider fact verified against primary
documentation on 2026-07-30 and re-verified the same day after the corpus work**; each entry
records the date. Nothing changed on re-verification: same three models, same three prices, no
new embedding model. A test pins the two candidate prices so a silent edit shows up as a test
change rather than as a quietly different cost estimate.

One honest caveat from the re-read: the two fetches of the embeddings guide disagreed about
whether `text-embedding-ada-002`'s native width is stated on the page. `NATIVE_DIMENSIONS`
therefore **does not record it**, and the adapter requires an explicit width for that model.
It is not a candidate, so nothing depends on the answer.

| Candidate | Kind | Width | USD / 1M tok | Decision-grade? |
|---|---|---|---|---|
| `offline-fake-256` | offline deterministic | 256 | free | **never** |
| `lexical-trigram` | lexical baseline | — | free | **never** |
| `openai-3-small-1536` | paid | 1536 (native) | 0.02 | yes |
| `openai-3-small-768` | paid | 768 | 0.02 | yes |
| `openai-3-small-512` | paid | 512 | 0.02 | yes |
| `openai-3-large-3072` | paid | 3072 (native) | 0.13 | yes |
| `openai-3-large-2000` | paid | 2000 | 0.13 | yes |
| `openai-3-large-1024` | paid | 1024 | 0.13 | yes |
| `local-multilingual-e5-large` | local model | ? | free per call | **declared only** |
| `local-bge-m3` | local model | ? | free per call | **declared only** |

Verified capability facts: `text-embedding-3-small` → 1536 native, 8192 max input
tokens, accepts `dimensions`; `text-embedding-3-large` → 3072 native, 8192, accepts
`dimensions`. Shortening is documented as supported, and **manual** shortening requires
L2 re-normalising — which is why the adapter asks the API for a reduced width instead of
truncating client-side.

**`text-embedding-ada-002` is not a candidate.** It does not support `dimensions`, it is
five times the price of `3-small`, and the comparison usually cited to justify it is
anti-fact 17. Including it would spend money confirming something nobody is proposing.

**`openai-3-large-2000` is the interesting configuration**: 2000 is the largest width
`vector` can index under HC-24, so it offers `3-large` quality while leaving both column
types available. If `3-large-3072` wins outright, the column type is **forced** to
`halfvec` — that is a finding, not a preference.

**Local models are declared, not implemented.** DECISION 7 forbids installing heavyweight
local ML infrastructure without showing the cost first, so these entries exist to be
shown. Their availability, licence and width are all **unverified** and must be checked
against primary sources before either becomes a real candidate. The harness raises if
asked to run one. This repository imports no ML framework.

### The lexical baseline is not decoration

Character-trigram Jaccard overlap, computed locally, approximating what `pg_trgm` would
do — and `pg_trgm` is already installed in every environment. **If a paid model cannot
beat trigram overlap on our corpus, that is the most important finding the bake-off can
produce.** Its expected weakness is the point too: it cannot match across scripts, so it
should score near zero on `cross-script`. A paid model that also scores near zero there
has told us something nobody has published.

## 6. Metrics

`tests/d8_bakeoff/metrics.py`, each with unit tests against hand-computed values,
because a scoring bug that flatters one candidate would not look like a bug — it would
look like a result, and it would end up in an ADR.

- **`answerability@k` — primary.** The fraction of queries where at least one gold
  passage is in the top `k`. Primary because it is the only metric that maps onto the
  product: a `search_knowledge` call returns `k` chunks and the agent answers from them,
  so what matters is "could the agent have answered". Recall@8 of 0.5 sounds mediocre and
  is perfectly adequate when the missing half was redundant.
- **`recall@k`** — how much of the gold set was found.
- **`nDCG@k`** — how well it was ordered, with exponential gain over graded judgements.
- **`MRR@k`** — how far down the first hit was. A candidate winning on answerability
  while losing badly on MRR is putting the answer at position 8, which costs prompt
  budget.

Reported at **k ∈ {4, 8, 16}**: 4 is `RETRIEVAL_DEFAULT_K`, 8 the likely production
value, 16 the configured `RETRIEVAL_MAX_K`.

## 7. Chunking is frozen

`FROZEN_CHUNKING_V1` in `rn_domain.chunking`: target 700 graphemes, max 1000, overlap
100, min 80, version `chunking-v1`.

**Frozen before the model comparison, because comparing models under different chunking
measures chunking.** The version string is recorded in every chunk and in every report,
a test pins the exact values, and a chunk-size sweep runs *afterwards* with the winning
model only. If the policy changes, that test fails — and the failure is the notification
that every recorded number has to be re-taken.

Budgets are in **grapheme clusters, not characters**. `len("हूँ")` is 3 and its grapheme
length is 1, so a character-budgeted chunker gives Hindi and Telugu systematically
smaller chunks than English for the same budget — which would make the per-language
comparison measure chunk size rather than model quality.

## 8. Acceptance gates — fixed before any candidate ran

Setting thresholds after seeing numbers is rationalisation, not measurement. These live
in `tests/d8_bakeoff/report.py`; editing one after results exist is a reviewable act in a
diff, which is the intended friction.

| Gate | Criterion | Evaluable now? |
|---|---|---|
| **G0 — servability** | Can be served in V1 with **no new infrastructure**. A model that wins on quality but needs an inference host we do not have is a Phase-17 proposal, not a winner. | human judgement |
| **G1 — quality** | `answerability@8` ≥ **0.90** on every language subset, ≥ **0.85** on `cross-script`. | **yes** |
| **G2 — latency** | p95 (query embed + exact search) ≤ 300 ms at 10k chunks/tenant. *A budget, not a measurement.* | Stage 2 |
| **G3 — `halfvec`** | Adopted only if recall delta vs `vector` at the same width ≤ 0.5 pp on every subset **and** measured storage saving ≥ 40%. | Stage 2 |
| **G4 — ANN** | Adopted only if exact scan breaches G2 at a corpus size a real V1 tenant reaches. | Stage 2 |
| **G5 — partitioning** | Only on a measured planner problem. Never by argument. | Stage 2+ |
| **G6 — coexistence** | Follows from the chosen width; recorded in ADR-011. | with ADR-011 |

**Tie-break, fixed in advance:** within 1 pp on G1 → prefer the lower width, then the
lower cost, then the vendor already behind a seam we ship.

**If no candidate clears G1, that is a finding, not a reason to lower the bar.** The
pre-planned escalation is hybrid retrieval (RRF over vector + trigram) → a reranker →
revisit chunking.

## 9. Cost

Token counts are **estimates with a stated band, not measurements.** There is no
tokeniser in this repository; `tiktoken` would be a dependency added to produce a number
the API returns for free on the first real call. The band comes from per-script
characters-per-token assumptions that are labelled as assumptions.

The direction matters and is counter-intuitive: **Devanagari and Telugu cost *more*
tokens per character than English** under a byte-pair encoding whose vocabulary is
Latin-dominated. The widely repeated "~4 characters per token" is an English figure, and
using it for Hindi would understate the count several-fold.

Estimates as of the current dataset (143 passages, 804 queries) and projected to target
(600 / 250), from `--estimate-only`:

| | all paid candidates (USD) |
|---|---|
| current dataset | **$0.0069 – $0.0108** |
| projected to target | **$0.0119 – $0.0164** |

So a complete run of all six paid configurations, even at full target size, is **under two
cents**. Each reduced-width configuration is a **separate** billed request even for the same
model, and the total already accounts for that. Note the two rows have converged as the
corpus grew, which is the expected direction: the projection is an extrapolation from real
content, and there is now more real content to extrapolate from.

**Cost is not what is blocking D-8, and it never was.** Four cents does not justify a
decision-grade corpus, and reading these numbers as "so let's just run it" gets the
constraint backwards: the blockers in §11 are native-speaker review and 500 more passages of
real business content, neither of which money buys.

The first paid run replaces the band with `usage.prompt_tokens` reported by the provider,
recorded in the report as `reported_prompt_tokens`. Quote that afterwards, not the
estimate.

## 9a. Human review, and why it is template-first

Reviewing several hundred generated queries one at a time is work nobody finishes, and a
review that does not finish is a review that did not happen. So the workflow is organised
around the artifact that makes review efficient:

```bash
uv run python -m tests.d8_bakeoff.corpus.cli export-review        # one bundle per subset
# a native speaker edits `decision`, `reviewed_by`, `reviewed_on`, `notes` in place

uv run python -m tests.d8_bakeoff.corpus.cli export-spot-checks   # ONE file, all subsets
# the individual-query batch; completed rows go to source/spot_checks.yaml
uv run python -m tests.d8_bakeoff.corpus.cli apply-review    # merge back into phrasebook.yaml
uv run python -m tests.d8_bakeoff.corpus.cli build           # regenerate; review propagates
```

A reviewer sees **one row per template** — roughly 8 per language rather than 120 queries —
and each row carries a handful of the queries that template actually produced, so they judge
the *effect* rather than an abstraction. Each row asks six separate questions, because they
fail independently: `natural`, `semantically_equivalent`, `script_correct`,
`code_mixing_natural`, `grades_correct`, plus free-text `notes`.

**The propagation is bounded, recorded and auditable.** A generated query's review state is
the **weakest** of its inputs (template and service name), it records `derived_from` and
`review_inherited`, and `quality.py` requires that **10% of every subset be reviewed
individually** rather than by inheritance. Template review is an efficiency; the spot-check
floor is what stops it being a loophole.

**Reviewer identity cannot be faked.** `apply_decisions` refuses a decision with no reviewer,
no date, or a placeholder name — and the placeholder list includes `claude`, `assistant`,
`ai`, `llm`, `gpt`. A model cannot vouch for whether a Telugu sentence is natural, and a
corpus reviewed by the thing that generated it is not reviewed. Approving something the same
reviewer marked "not natural" is refused as a contradiction.

The single most useful thing a reviewer can flag: **a sentence that is grammatically correct
and that no caller would ever say.** That is the failure this exercise exists to catch, and it
is invisible to everyone who does not speak the language.

## 10. How to run it

```bash
# Corpus state, tier/role breakdown, and every quality gate. Needs nothing supplied.
uv run python -m tests.d8_bakeoff.corpus.cli status

# Build generated_*.yaml from source/. Needs both intake files.
uv run python -m tests.d8_bakeoff.corpus.cli build
```


# Cost estimate only. Embeds nothing, contacts nothing, costs nothing.
uv run python -m tests.d8_bakeoff.run --estimate-only

# Offline run: the deterministic fake and the lexical baseline. Free.
uv run python -m tests.d8_bakeoff.run

# PAID run. Needs all three guards.
RN_D8_ALLOW_PAID=1 OPENAI_API_KEY=sk-... \
    uv run python -m tests.d8_bakeoff.run --paid --yes-i-approve-the-cost
```

**Three independent guards stand between the module and a charge**, mirroring how `live`
tests are guarded ([TESTING §13](../TESTING.md)) for the same reason — one is not enough
for something that spends money:

1. paid candidates run only with `--paid`;
2. `--paid` additionally requires `RN_D8_ALLOW_PAID=1`;
3. an API key must be present, or the run refuses rather than half-running.

Plus `--yes-i-approve-the-cost`, without which a paid run stops after printing the
estimate — so the first paid invocation cannot be an accident and the approving human
sees the number being approved.

Artifacts land in `tests/d8_bakeoff/results/` (git-ignored except its README). The single
run ADR-011 cites is committed deliberately, in the same change as the ADR.

## 11. What still blocks a decision

**Human review is complete. Two blocking gates remain, and both need business input, not
engineering.** The tooling to consume each exists, is tested, and runs offline.

**Both are `BLOCK`, not `FAIL`** — no code change closes either, and the gate renderer says
so explicitly (`0 FAIL (fix in code), 2 BLOCK (awaiting human or business input)`). The
distinction is diagnostic only: `corpus_is_benchmark_ready` treats a blocked gate exactly
like any other failure, because a corpus that cannot test stale retrieval is no more able to
carry a decision than one with a broken generator. What it buys is that a *regression* — say
`no_passage_duplication` breaking — shows as `FAIL` and is instantly distinguishable from a
queue that is simply waiting on a person.

| # | Gate | What closes it | Who |
|---|---|---|---|
| 1 | `adversarial_present` | superseded/withdrawn Rise Next content for the `stale` role | Rise Next team |
| 2 | `size` | four inputs listed below — **a decision, not a task** | you |

**Closed on 2026-08-11:** `review_completeness` (all 8 subsets, 101 templates, 7 service-name
sets) and `spot_check` (76 individually-judged queries, recorded in `source/spot_checks.yaml`).

### 1. Superseded content — `adversarial_present`

One old service description, a withdrawn offer, an expired promotion, a superseded process
description or a former tagline, pasted into the `superseded:` section of
`source/risenext.yaml` (currently `[]`). Each entry needs the text, roughly when it was
replaced, and what replaced it; **approximate dates are fine.** One item unblocks the gate;
two or three make the test meaningfully harder.

Without it the corpus contains no `stale` passage, so it is blind to the stale-retrieval
failure that `documents.status = 'active'` exists to prevent — an agent confidently quoting
withdrawn copy on a live call.

**It cannot be synthesised, and that is not a technicality.** Stale content has a property
that cannot be faked: it was once true, and it is close enough to current copy that a
retriever genuinely confuses the two. Invented "old" text is a semantic distractor wearing a
stale label — it would make the gate pass while measuring a different failure mode, which is
worse than the gate failing. `_EXTERNALLY_SUPPLIED_ROLES` in `quality.py` encodes exactly
this: `STALE` is the one adversarial role whose absence is `BLOCK` rather than `FAIL`, because
`distractor` and `injection` are generated from material already supplied and their absence
*would* be a code defect.

### 2. The `size` gate — Option B is specified but not yet implementable

`TARGET_PASSAGES = 600` was estimated, never measured. The supplied material is fully
decomposed at 143 passages and there is no more in it without inventing facts. §3's measured
evidence says the corpus discriminates at a fraction of 600. Two honest ways forward:

**Option A — more real Rise Next content.** Per-service detail at capabilities-page depth,
more FAQs (the best possible queries *and* passages), case studies, industry pages — any
published copy. Generating filler does not work: invented passages are
`non_decision_synthetic` and can never support a decision, by construction.

**Option B — replace the count with a measured criterion.** The shape is right: *"the corpus
is adequate when the best candidate beats the lexical baseline by a margin on every subset"*
asks a better question than "are there 600 rows", and it is free to evaluate.

**Option B was taken to implementation on 2026-08-11 and stopped, deliberately.** As written
above it is a sketch, not a specification — it names no number — and a pre-registered gate
whose threshold is chosen by the person implementing it, after the results are already known,
is not a gate. §8 already says why: *"Setting thresholds after seeing numbers is
rationalisation, not measurement."* `TARGET_PASSAGES` therefore stays at 600 and `size` stays
`BLOCK`.

**Four inputs are missing. Each needs a decision; none can be derived from the repository:**

| # | Missing | Why it cannot be inferred |
|---|---|---|
| 1 | **The margin.** The text above literally reads "margin X". | Nothing in the repository states it. §8's G1 thresholds (0.90 / 0.85 `answerability@8`) are *candidate acceptance* — "is this model good enough to ship" — not corpus adequacy, which asks whether the corpus can *tell two candidates apart*. Borrowing one for the other would be a category error wearing a real number. |
| 2 | **Which metric, at which `k`.** | §6 defines four (`answerability`, `recall`, `nDCG`, `MRR`) at k ∈ {4, 8, 16}. §11 names none. `answerability@8` is the obvious guess and a guess is exactly what must not be committed. |
| 3 | **Absolute or relative.** | On `cross-script` the offline measurement is 0.258 vs 0.065 — **+19.3 pp absolute, but 4× relative.** A margin of "0.10" passes on one reading and fails on the other. The two readings disagree hardest on the subset D-8 exists for. |
| 4 | **What "the best candidate" means before the paid run.** | `CandidateKind.is_decision_grade` structurally excludes both offline kinds from informing ADR-011. If "best candidate" means a paid one, the gate that authorises the paid run depends on the paid run's own results — circular. If the offline fake may stand in, that is a deliberate narrowing of `is_decision_grade` and belongs in an ADR, not in a gate body. |

**To close it, decide those four and record them in an ADR.** A worked example of the shape
an answer takes — *not a recommendation, and the numbers are placeholders*: "the corpus is
adequate when `offline-fake-256` beats `lexical-trigram` by ≥ N pp absolute on
`answerability@8` in every subset; the offline pair is admissible for this gate only, because
it measures a property of the corpus rather than of a model." Fill in N and the rationale for
each of the four and `_size` becomes a dozen lines.

The two options are not exclusive, and A-then-B is a coherent order: more real content makes
whatever criterion is chosen more convincing.

### Not blocking, but next

- **Approval for the paid run** (DECISION 7) — under two cents, and the least consequential
  item here.
- **Stage 2** for G2/G3/G4/G5 — they need `document_chunks` and a vector column, which is
  exactly what ADR-010 says must not be written until this closes. The dependency is
  deliberately circular-looking and is not: G1 chooses the model and width, the migration is
  then written against that answer, and G2–G5 are measured afterwards against the real schema,
  with ADR-011 recording all of it.
- **Decomposition fidelity** — nobody at Rise Next has yet confirmed that no claim drifted
  when the supplied text was split into 143 passages.

Also outstanding and easy to forget: the **Hindi and Telugu injection-detection patterns** in
`rn_domain.sanitisation` are synthetic and unreviewed. They belong in the same review pass as
the phrasebook, and their recall must not be quoted as coverage until they have had one.

## 12. Output

**ADR-011 — `docs/DECISIONS/ADR-011-embedding-model-and-vector-layout.md`**, discharging
ADR-010, recording the chosen model, width, column type, index strategy, partitioning
verdict and coexistence mechanism, **with the per-language numbers that chose them** and
the path of the committed result artifact.

Until that ADR exists, D-8 is open, and every document that quotes a model or a width is
quoting a placeholder.
