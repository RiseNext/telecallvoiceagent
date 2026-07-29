"""Realtime session — one live call's provider session and turn lifecycle.

Owns the realtime provider connection, session configuration, turn events,
tool-call dispatch, session rollover and reconnection. Consumes `rn_voice.media`
for transport; is consumed by `rn_voice.runtime`.

This layer may hold per-call conversation state. It may NOT hold state shared
between calls, and it may not reach into media internals — it drives media
through its interfaces so the transport can be tested and replaced on its own.

Like every layer in this app it stays off the orchestration framework; the
decision about whether a turn consults orchestration belongs one layer up, in
`rn_voice.runtime`. See docs/DECISIONS/ADR-009-orchestration-boundary.md.
"""

__all__: list[str] = []
