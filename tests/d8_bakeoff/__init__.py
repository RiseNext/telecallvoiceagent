"""The D-8 embedding bake-off.

Open decision **D-8** asks which embedding model, at what width, in which Postgres
column type, with what index and what partitioning ([ADR-010]). ADR-010 defers all
of it to a measurement, and this package *is* the measurement: dataset, metrics,
candidate manifest, harness and result artifact.

**Why it lives under `tests/`.** It is evaluation machinery, and everything here has
to pass the same gates as production code — `ruff`, `mypy --strict`, and the test
suite — because a benchmark nobody can trust is worse than no benchmark. Putting it
in a top-level `benchmarks/` directory would place it outside `testpaths` and
outside `mypy_path`, i.e. outside every check. The metric functions have unit tests
of their own; a scoring bug that flatters one candidate is exactly the failure a
bake-off cannot afford.

**Nothing here decides anything.** The harness produces a report. A human reads the
report and writes ADR-011. Two guards make that structural rather than aspirational:

* a report is marked **decision-grade only** when every candidate it scored is a real
  model *and* every dataset subset it scored is native-speaker reviewed;
* the fake provider and the lexical baseline are permanently non-decision-grade, so
  a number produced offline can never be quoted as a model comparison.

**No paid call happens by importing anything here.** Paid candidates require an
explicit opt-in flag *and* an API key in the environment, and the default test run
exercises only the offline candidates.
"""
