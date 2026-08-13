"""Barge-in. **One function. One call site. Three effects.**

Three call sites is how you get a system that is correct in testing and corrupt in
production — one of them drifts, and the drift is invisible because each individual
step still works.

    freeze  →  clear  →  flush  →  truncate

**The order is not stylistic.** Freeze the ledger *first*: the pacer runs on another
task and will happily advance `enqueued_ms` while the `clear` is being awaited. Freeze
afterwards and you report audio the caller never heard — the over-reporting direction,
which is the one that makes a conversation unrecoverable (see `ledger`).

Then, in order:

1. **clear** — discard what the provider has buffered but not yet played (HC-8). It
   discards *their* buffer only; it does not stop our generator, which is why one
   operation is not enough.
2. **flush** — discard our own un-sent audio. Clearing theirs while ours keeps feeding
   them is a no-op with extra steps.
3. **truncate** — tell the model the truth (HC-7). On the WebSocket transport OpenAI
   does **not** auto-truncate on barge-in; only WebRTC does. A session with no remote
   state to correct (`supports_remote_truncation=False`) gets `cancel_generation()`
   instead, which is the same effect through a different mechanism.

The budget for our own work here is **≤ 20 ms** (a target, unmeasured). What the caller
perceives is bounded by the provider's un-played buffer, which the pacer holds at one
or two chunks — 80-160 ms at 24 kHz. That is what makes the PRD's ~200 ms barge-in
requirement achievable at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rn_core.logging import get_logger
from rn_providers.realtime.session import VoiceSession
from rn_voice.media.ledger import PlaybackLedger
from rn_voice.media.ring import OutboundRingBuffer

__all__ = ["BargeInOutcome", "ClearPlayback", "handle_barge_in"]

_logger = get_logger(__name__)

#: Discards the provider's un-played buffer. A callable rather than a transport object
#: so barge-in has no telephony dependency at all.
type ClearPlayback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BargeInOutcome:
    """What one barge-in did. Returned so a caller can record it without re-deriving it."""

    audio_end_ms: int
    item_id: str | None
    content_index: int
    #: Bytes of our own un-sent audio that were dropped.
    flushed_bytes: int
    #: Whether the model was told (`truncate`) or merely stopped (`cancel_generation`).
    truncated: bool

    @property
    def cancelled(self) -> bool:
        return not self.truncated


async def handle_barge_in(
    *,
    ledger: PlaybackLedger,
    buffer: OutboundRingBuffer,
    session: VoiceSession,
    clear_playback: ClearPlayback,
    now_ms: float | None = None,
) -> BargeInOutcome:
    """Perform a barge-in. **The only sanctioned way to do any of these four things.**

    Args:
        ledger: Frozen first, before anything can advance it.
        buffer: Our un-sent audio, discarded.
        session: Told the truth about what was played.
        clear_playback: Discards the provider's un-played buffer. Passed in rather than
            taken from a transport object so this module needs no telephony import and
            stays testable with a two-line stub.
        now_ms: Instant of the interruption. Defaults to the ledger's clock.

    Returns:
        A `BargeInOutcome`. `audio_end_ms` is an **integer** because that is what the
        provider's truncate call takes, and it is rounded **down** — the same bias-low
        rule as everywhere else in the accounting.
    """
    frozen_ms = ledger.freeze(now_ms)
    item_id = ledger.item_id
    content_index = ledger.content_index
    # Truncate, never round-half-up: half a millisecond in the wrong direction is still
    # the wrong direction, and it costs nothing to be consistent about it.
    audio_end_ms = int(frozen_ms)

    await clear_playback()
    flushed = buffer.flush()

    truncated = False
    if session.capabilities.supports_remote_truncation and item_id is not None:
        await session.truncate(
            item_id=item_id, content_index=content_index, audio_end_ms=audio_end_ms
        )
        truncated = True
    else:
        # No remote conversation state to correct — the context is ours. Unify at the
        # effect level rather than pretending the mechanisms are the same.
        await session.cancel_generation()

    _logger.info(
        "voice.bargein.handled",
        # Durations and counts only. No transcript, no audio, no caller identity.
        item_id=item_id,
        audio_end_ms=audio_end_ms,
        flushed_bytes=flushed,
        truncated=truncated,
        enqueued_ms=round(ledger.enqueued_ms, 3),
        confirmed_ms=round(ledger.confirmed_ms, 3),
    )
    return BargeInOutcome(
        audio_end_ms=audio_end_ms,
        item_id=item_id,
        content_index=content_index,
        flushed_bytes=flushed,
        truncated=truncated,
    )
