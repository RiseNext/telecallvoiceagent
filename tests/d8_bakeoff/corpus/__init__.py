"""Corpus generation: source material in, dataset out. Deterministic and offline.

    source/risenext.yaml     facts (English, source-grounded, human-supplied)
    source/phrasebook.yaml   question phrasings per language (native-reviewed)
                │
                ▼
          corpus/build.py    no LLM, no network, no randomness
                │
                ├──► data/corpus.yaml    passages
                └──► data/queries.yaml   queries

**Four tiers, kept apart on purpose** (`dataset.MaterialTier`):

| Tier | Where it comes from | May support a decision |
|---|---|---|
| `source_grounded` | supplied business content, with a `source_reference` | yes |
| `controlled_synthetic` | a documented deterministic transform of the above | yes, via `derived_from` |
| `adversarial` | deliberate hard cases — distractors, stale, injection, price-bearing | yes |
| `non_decision_synthetic` | invented outright | **never**, whatever its review status |

Collapsing the first two would either block the corpus on individually reviewing every
paraphrase, or let invented facts count as evidence. Keeping them apart is what makes
template-level review both efficient and honest.

**Nothing here calls a model.** Every transform is a template substitution, a character
edit, or a field swap — auditable by reading it, and reproducible byte for byte. If
LLM-assisted generation is ever proposed, the process and cost go in front of a human
first; see `docs/research/D8_BAKEOFF.md`.
"""
