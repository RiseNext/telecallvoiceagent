# D-8 run artifacts

Every bake-off run writes two files here: a JSON artifact (the data) and a Markdown
summary (the same thing, readable). Both are named
`d8-<timestamp>-<decision|not-decision-grade>.json`.

**This directory is git-ignored except for this file.** Runs accumulate quickly and
most of them are exploratory, so committing them all would bury the one that matters.
The rule is:

- exploratory runs stay local;
- the **single run that ADR-011 cites** is committed deliberately with
  `git add -f tests/d8_bakeoff/results/<file>`, in the same change as the ADR.

That way the decision's evidence lives in the repository — an ADR that says "we
measured" and cites nothing is an ADR nobody can check — while the noise does not.

## Reading a report

Read three things, in this order:

1. **`report_is_decision_grade`.** If it is `false`, the report cannot choose a
   production model, and `readiness_notes` says why. The usual reasons are an offline
   candidate (the fake and the lexical baseline are permanently non-decision-grade)
   and dataset subsets awaiting native-speaker review.
2. **`worst_subset`** per candidate, not the pooled average. A candidate can pool to
   0.92 while scoring 0.55 on Telugu, and shipping that is how an India-first product
   gets a language wrong. Gate G1 is expressed per subset for exactly this reason.
3. **`gates`.** Anything marked `NOT EVALUATED` genuinely was not — G2, G3 and G4 need
   a Postgres vector column that does not exist until Stage 2, and G0 (servability) is
   a human judgement. A report showing one green gate and six unevaluated ones is not
   a pass.

`reported_prompt_tokens` is the number that replaces the estimated token band in
`candidates.py` with a measurement. Once a paid run has happened, quote that, not the
estimate.
