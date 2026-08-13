"""The offline Aira retrieval demo.

**Everything Rise Next-specific about this retrieval slice lives in this package**, and nothing
outside it. `rn_services.retrieval` and `rn_agent.tools.builtin.search` are generic
platform code that has never heard of Rise Next, Aira, or a knowledge base about
digital marketing; this package is the tenant configuration that gives them something
to retrieve. That separation is the point — Aira is a tenant, not the product
(`CLAUDE.md`).

The corpus is the D-8 bake-off corpus, read **read-only** from `tests/d8_bakeoff/data`.
Nothing here writes to it, regenerates it or depends on its gates.
"""
