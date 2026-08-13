"""The outbound ring buffer and aligner. Where arbitrary deltas become legal chunks.

Three facts collide, and this component is what resolves them:

1. Model deltas arrive at **arbitrary byte sizes**.
2. The telephony provider accepts only aligned chunks within a size window (HC-2, and
   at 24 kHz our own tighter 960-byte quantum — see `ChunkPolicy`).
3. The model generates **much faster than realtime**: a 20-second spoken response
   arrives over the wire in a few seconds.

Writing deltas straight through violates (2) and produces choppy audio that the whole
team will initially misdiagnose as a network problem. It is the single most common way
this class of integration fails, which is why the buffer is a **required component and
not an optimisation** (ADR-003).

Fact (3) is the `Pacer`'s problem, not this module's. This one only answers *"what is
the largest legal chunk I can hand over right now?"*.

## The padding rule, and why it is subtle

A response's final chunk is usually shorter than the provider's floor — there is no
legal way to write 900 bytes when the minimum is 3840. So `take_final()` pads with
digital silence up to the smallest legal emission.

The padding is played, and it is **not the model's audio**. Counting it in the playback
ledger would inflate `audio_end_ms`, which is the over-reporting direction and the
dangerous one. `take_final()` therefore returns the payload **and** the real byte count
separately, and the ledger is fed only the latter. That split is the entire reason this
returns a pair instead of bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

from rn_core.errors import InvariantViolation
from rn_providers.telephony.base import ChunkPolicy

__all__ = ["OutboundChunk", "OutboundRingBuffer"]


@dataclass(frozen=True, slots=True)
class OutboundChunk:
    """One legal emission.

    Attributes:
        payload: What goes on the wire. Always satisfies the chunk policy.
        real_bytes: How much of `payload` is model audio rather than padding. Equal to
            `len(payload)` for every chunk except a padded final one. **This is the
            number the playback ledger is given.**
        item_id: The assistant item this audio belongs to. Carried on the chunk so the
            ledger's per-item reset cannot be driven from stale state elsewhere.
        content_index: The content index within that item.
    """

    payload: bytes
    real_bytes: int
    item_id: str
    content_index: int

    @property
    def padding_bytes(self) -> int:
        return len(self.payload) - self.real_bytes


class OutboundRingBuffer:
    """Accumulates assistant audio; emits only chunks the provider will accept.

    One buffer per *item*: appending audio for a new item flushes what remains of the
    old one, because two items' audio must never end up in one chunk — the ledger's
    accounting is per item and a mixed chunk could not be attributed to either.

    Not thread-safe, and it does not need to be: it is owned by the bridge task.
    """

    __slots__ = ("_buffer", "_content_index", "_item_id", "_policy")

    def __init__(self, *, policy: ChunkPolicy) -> None:
        self._policy = policy
        self._buffer = bytearray()
        self._item_id: str | None = None
        self._content_index = 0

    @property
    def policy(self) -> ChunkPolicy:
        return self._policy

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def item_id(self) -> str | None:
        return self._item_id

    def append(self, pcm: bytes, *, item_id: str, content_index: int = 0) -> None:
        """Buffer audio for an item, dropping any partial audio from a previous one.

        The drop is deliberate and it is not data loss in practice: a new item only
        begins after the previous response finished or was interrupted, and in the
        interrupted case that audio was destined for a `flush()` anyway.
        """
        if item_id != self._item_id or content_index != self._content_index:
            self._buffer.clear()
            self._item_id = item_id
            self._content_index = content_index
        self._buffer.extend(pcm)

    def take_chunk(self) -> OutboundChunk | None:
        """One legal chunk, or `None` if not enough is buffered.

        The size comes from `ChunkPolicy.emission_size`, which returns the **smallest**
        legal emission rather than the largest — because the barge-in uncertainty window
        is exactly one chunk, so a bigger chunk is a wider window. See that method.

        `None` is the normal state between deltas, not an error — the caller loops on it.
        """
        size = self._policy.emission_size(len(self._buffer))
        if size == 0:
            return None
        return self._emit(size, real_bytes=size)

    def take_final(self) -> OutboundChunk | None:
        """Drain the remainder, padding with silence to reach the provider's floor.

        Returns `None` when nothing is buffered. Called repeatedly: a remainder larger
        than the floor yields ordinary chunks first and only the last sub-minimum piece
        is padded, so audio that did not need padding does not get it.

        The returned chunk's `real_bytes` excludes the padding — see the module
        docstring for why that matters more than it looks.
        """
        remaining = len(self._buffer)
        if remaining == 0:
            return None
        size = self._policy.emission_size(remaining)
        if size:
            return self._emit(size, real_bytes=size)

        target = self._policy.effective_min
        if target > self._policy.effective_max:  # pragma: no cover - ChunkPolicy refuses this
            raise InvariantViolation("No legal chunk size exists for this policy.")
        chunk = self._emit(remaining, real_bytes=remaining)
        return OutboundChunk(
            payload=chunk.payload + b"\x00" * (target - remaining),
            real_bytes=remaining,
            item_id=chunk.item_id,
            content_index=chunk.content_index,
        )

    def flush(self) -> int:
        """Discard everything buffered. Returns how many bytes were dropped.

        The second of barge-in's three effects: clearing the provider's buffer is
        pointless while ours keeps feeding it. Never call it on its own — barge-in is
        one function with one call site (`rn_voice.media.bargein`).
        """
        dropped = len(self._buffer)
        self._buffer.clear()
        return dropped

    def _emit(self, size: int, *, real_bytes: int) -> OutboundChunk:
        if self._item_id is None:  # pragma: no cover - append always sets it
            raise InvariantViolation("Ring buffer emitted a chunk with no item id.")
        payload = bytes(self._buffer[:size])
        del self._buffer[:size]
        return OutboundChunk(
            payload=payload,
            real_bytes=real_bytes,
            item_id=self._item_id,
            content_index=self._content_index,
        )
