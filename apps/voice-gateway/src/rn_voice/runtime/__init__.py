"""Agent runtime — the decision layer of a live call.

Resolves the agent definition for a call, composes instructions, exposes the
tool registry to the session, applies guardrails, and decides what happens on a
turn. This is the layer that is ALLOWED to consult an orchestration layer when
a turn genuinely needs multi-step reasoning, recovery or complex state.

That permission is deliberate and it is gated, not free:

  - Orchestration is invoked from here, never from `rn_voice.media` or
    `rn_voice.session`.
  - Anything synchronous inside a live turn needs a measured latency
    justification before it ships. Off-turn and background use needs none.
  - The default path must keep working when orchestration is disabled,
    unavailable or too slow.

See docs/DECISIONS/ADR-009-orchestration-boundary.md for the gate, and
docs/REALTIME_VOICE.md for the latency budget it has to fit inside.
"""

__all__: list[str] = []
