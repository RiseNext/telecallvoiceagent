# Source material intake

This directory is where **real RiseNext content** arrives. Everything downstream of it —
the corpus, the queries, the benchmark, ADR-011 — inherits its trustworthiness from here,
so the schema is deliberately fussy about two things: **where a fact came from**, and
**whether a fact is authoritative or descriptive**.

## What to fill in

| File | Who fills it | Status |
|---|---|---|
| `TEMPLATE.yaml` | — | the schema, with an annotated example of every field |
| `risenext.yaml` | the Rise Next team | ✅ **supplied 2026-07-30, fully decomposed.** Ten categories, 41 facts, 7 services, 69 named capabilities — every entry source-grounded and `reviewed_by: Rise Next team` |
| `phrasebook.template.yaml` | — | the query-phrasing schema, with examples |
| `phrasebook.yaml` | **native Hindi and Telugu speakers** | ✅ **101 templates, all `native_reviewed`** — Rise Next team, 2026-08-11, across all eight subsets, with all 7 service-name sets |
| `spot_checks.yaml` | the same reviewers | ✅ **76 individually-judged queries, all approved** — the floor that bounds template-level review |

Nothing in this repository invents Rise Next facts and labels them source-grounded. The
corpus built from this directory is **143 passages and 804 queries**, and every one of those
passages is a split or a join of text in `risenext.yaml` — never an elaboration.

**One thing still needed from the Rise Next team**, which the tooling already consumes and
which cannot be synthesised:

- **Superseded content** for the `superseded:` section, currently `[]` — one old service
  description, a withdrawn offer, an expired promotion, a superseded process description or
  a former tagline. Each entry needs the text, roughly when it was replaced and what
  replaced it; approximate dates are fine, and one item is enough. Without it there is no
  `stale` passage and the corpus cannot measure the failure `documents.status = 'active'`
  exists to prevent. Invented "old" text does not work: it is a semantic distractor wearing
  a stale label, and it would make the gate pass while measuring something else.

**Native-speaker review is done, so more content is now the top blocker.** The corpus is 143
passages against a 600 target. The highest-value additions are per-service detail at
capabilities-page depth, more FAQs (they make the best queries *and* the best passages),
case studies and industry pages.

**If you add content, add spot checks in the same change.** Every subset currently meets the
10% individual-review floor with **zero margin**, so new queries push the floor up and reopen
a closed gate.

## The two things the schema is fussy about

### 1. Where a fact came from

Every entry carries `source_reference` — a URL, a document name, "brochure v3 p2", "confirmed
by <person> on <date>". It exists so that when a benchmark result is disputed six months
from now, the disputed fact can be traced to something. An entry claiming
`tier: source_grounded` with no `source_reference` is **rejected by the loader**, because
that specific mislabelling is how invented content ends up quoted as fact.

### 2. What kind of thing a fact is

This is the distinction PRD §6.5 calls a correctness requirement rather than a style
preference, and it is the one thing in this directory that can cause a real customer
problem. **Four values, not two** — the sorting instinct of "price / not price" produces two
wrong answers.

| Classification | Means | Retrieval may return it? |
|---|---|---|
| `descriptive` | Answers *"what do you do?"* — service descriptions, capabilities, company background, process, FAQs | **yes.** This is what the knowledge base is for |
| `policy` | A business rule whose **statement** is the answer — "quotations are customised", "we never promise guaranteed rankings", "we are not a lender" | **yes, deliberately** |
| `authoritative` | A current, changeable, exact **value** — a price figure, an availability slot, an order status | **no.** A typed tool serves it from a system of record |
| `structural` | Domain/CRM **field definitions** — the list of things a lead capture must collect | **no**, and it never becomes a passage at all |

**The `policy` row is the one that surprises people.** Hiding anything price-related from
retrieval feels safe and is backwards. The danger is not that retrieval returns *"every
project is quoted individually"* — that **is** the correct answer to "how much does this
cost?". The danger is that retrieval returns a **number**. Withhold the policy and a pricing
question has no correct passage at all, which is exactly the state in which a model invents
a figure. So the loader refuses `policy` marked `never_rag: true`, just as firmly as it
refuses `authoritative` marked `never_rag: false`.

Prices live in `service_prices` and reach the model only through `get_service_pricing`.

**On supplying prices here: prefer not to.** You *may* — mark them `authority: authoritative`,
`kind: price`, `never_rag: true`, and they become **price-bearing adversarial passages** that
are never gold. But the current file supplies none, which buys something stronger than "prices
are labelled as traps": the `no_numeric_prices_in_corpus` gate can assert that **no numeric
price exists anywhere in the corpus**, so a pricing query cannot be answered with a figure by
construction. Adding one trades that guarantee away, and the gate will fail the build to make
sure the trade is deliberate.

`structural` exists because a lead-capture field list is schema, not content. No caller asks
what fields the form has, so a passage of it adds noise without adding a test — and a naive
"never_rag ⇒ it must be a price trap" rule would have labelled the field list as a price.

## How this becomes a corpus

```
source/risenext.yaml        (facts, English, source-grounded)
source/phrasebook.yaml      (question phrasings per language)
             │
             ▼
    corpus/build.py          deterministic, offline, no LLM, no network
             │
             ├──► data/generated_corpus.yaml    source-grounded + adversarial passages
             └──► data/generated_queries.yaml   template × service, per subset
```

The multiplication is the point, over three fan-out axes — **capability-scoped × 69
sub-services, service-scoped × 7 services, business-wide × 1** — which turns 101 reviewable
templates into 804 judged queries. A reviewer validates the 12 templates in Devanagari rather
than the 49 Hindi queries generated from them, which is the difference between a review that
happens and one that does not.

Capability templates fan out over all 69 sub-services in `en` and a **strided sample**
elsewhere: a truncated `[:20]` would take everything from the first service and nothing from
the last, so a sampled subset would silently test Technology Solutions alone.

Every generated row carries **provenance** back to the intake entry it came from:
`source_id`, `source_version`, `source_type`, `generated_from` and `human_review_required`.
Seven phrasings of one template share one `source_id`, so a paraphrase can never quietly
become an independent business fact, and counting distinct `source_id`s is the honest measure
of how much a subset actually tests.

That propagation is bounded, not unlimited: a fraction of generated items in every subset
must **also** be individually reviewed (`quality.py::SPOT_CHECK_FRACTION`), or the subset
does not pass its gate. Template-level review is an efficiency, not a loophole.

## Filling in the phrasebook

`phrasebook.yaml` holds question *phrasings* with a `{service}` slot, per subset. A native
speaker writes how a real caller would actually ask — including the terse, impatient and
code-mixed forms, which are the ones that matter and the ones a translator tends to
sanitise into textbook grammar.

Also needed there: the **service-name translations**. `"website development"` in Devanagari,
in romanised Hindi, in Telugu script and in romanised Telugu. Those are what let a template
and a service combine into a natural query.

Both files are reviewed like code. `review_status: native_reviewed` requires a named
`reviewed_by` and a `reviewed_on` date — the loader rejects a review claim with no reviewer,
because otherwise "reviewed" is a word someone typed and there is nobody to ask when a
judgement is disputed.
