"""The recording tap point. **Disabled by default, and deliberately present anyway.**

Open decision **D-5** — *do we record calls at all in V1, and is it per-tenant?* — is
not due until Phase 8, because it changes the disclosure script and the storage path.
But [ROADMAP](../../../../../docs/ROADMAP.md) makes it a Phase-4 consideration for one
reason that has nothing to do with the answer:

> *"Yes, cheaply — **if** the bridge is built with a tap point from the start.
> Retrofitting a media tap into a latency-critical loop is expensive; leaving an unused,
> disabled-by-default tap is not."*

So this exists, does nothing, and costs one `is None` check per frame. That is the whole
design. When D-5 says yes, a `MediaTap` implementation is written and wired at the
composition root; when it says no, this file is deleted and nothing else changes.

**What a tap must never become.** It is on the audio path, so it may not block, may not
do I/O inline, and may not raise into the pump — a recording feature that can drop a
call is worse than no recording feature. `NullMediaTap` documents that contract by
satisfying it trivially, and the bridge guards every call so an implementation that
breaks it degrades to "no recording" rather than "no call".

**Nothing here decides anything about D-5.** No retention, no consent, no storage
target, no format. Those are the decision; this is only the place the bytes would be
handed to it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = ["MediaDirection", "MediaTap", "NullMediaTap"]


class MediaDirection(StrEnum):
    """Which leg a frame came from.

    Recorded separately rather than mixed: a mixed recording cannot be used to debug
    barge-in, and the two legs have different consent and retention consequences — the
    caller's audio is theirs, the agent's is ours.
    """

    INBOUND = "inbound"
    """From the caller, as received from telephony."""

    OUTBOUND = "outbound"
    """To the caller, as written to telephony — after transcoding and alignment, so it
    is what was actually played rather than what the model generated."""


@runtime_checkable
class MediaTap(Protocol):
    """Observes media frames without participating in them.

    Synchronous and non-async on purpose: an `async` tap invites an implementation that
    awaits a network write inside the audio pump, which is exactly the thing
    `CLAUDE.md`'s first non-negotiable rule forbids. An implementation buffers here and
    flushes on a background task.
    """

    def observe(self, direction: MediaDirection, pcm: bytes) -> None:
        """Take a copy of one frame. Must not block, must not raise."""
        ...


class NullMediaTap:
    """The default. Records nothing, costs nothing, exists so the call site does not branch.

    `enabled` is `False` so a caller can skip even the method call in a hot loop, and so
    a log line can state honestly whether anything was recorded.
    """

    __slots__ = ()

    enabled: bool = False

    def observe(self, direction: MediaDirection, pcm: bytes) -> None:  # noqa: ARG002
        # Both arguments are deliberately unused: this satisfies the protocol and does
        # nothing, which is the entire design until D-5 is answered.
        return None
