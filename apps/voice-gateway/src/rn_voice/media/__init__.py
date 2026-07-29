"""Media transport — the audio hot path.

This layer moves bytes and nothing else: telephony WebSocket transport, frame
decode/encode, resampling, the outbound pacing/alignment ring buffer, played-
millisecond accounting, VAD event plumbing, and the mechanics of barge-in
(clear the telephony buffer, flush our buffer, report the truthful played
position).

THE PERMANENT INVARIANT
-----------------------
Nothing in this package may import an orchestration framework, the
orchestration package, the agent runtime, or a business service. This is not a
"for now" rule that a benchmark can relax — it is the boundary that keeps the
audio path independent of everything above it, and it is enforced by the
"Media transport layer is framework-free and orchestration-free" contract in
the root pyproject.toml.

What this layer knows about a call is what it is handed: sockets, formats,
buffers and timings. It does not know what an agent is, what a tenant is, or
what the conversation is about. See docs/DECISIONS/ADR-009-orchestration-boundary.md.
"""

__all__: list[str] = []
