"""Playback accounting. The highest-risk arithmetic in the system.

`audio_end_ms` is the value `conversation.item.truncate` carries on every barge-in
(HC-7), and getting it wrong is the only failure in the audio path that **announces
nothing**. A dropped socket raises. A malformed frame raises. Unaligned chunks sound
choppy and someone files a bug within the hour. A wrong `audio_end_ms` produces no
error, no log line and perfectly clean audio — and then the model behaves rationally on
a false premise.

**The two directions are not symmetric.**

* **Over-report** — the model believes the caller heard something they did not. It will
  not repeat it; it will *reference* it: *"as I mentioned, the setup fee is…"* for
  something never spoken. The caller experiences an agent confidently discussing a
  conversation that did not happen, and it is invisible in the transcript, because the
  transcript records what the model **said**, not what was **played**.
* **Under-report** — the model believes it said less than it did and may repeat a
  sentence. Mildly awkward. Recoverable. The caller thinks the agent is being thorough.

So: **bias low, always.** That single principle explains every clamp below.

## Three quantities

| Field | Meaning | Trust |
|---|---|---|
| `enqueued_ms` | audio handed to the telephony socket | ours — an **upper bound** on what was heard |
| `confirmed_ms` | audio the provider echoed a mark for (HC-9) | **the only ground truth** |
| `estimate_played_ms(now)` | `min(enqueued_ms, confirmed_ms + since_last_mark)` | what we send |

The wall-clock extrapolation assumes the provider plays out in realtime once playback
has started, which is **[A] and unverified** — hence the `min()`, which makes the worst
case "we under-report" rather than "we over-report".

## Milliseconds, never bytes

Milliseconds are rate-invariant, so a resampler cannot corrupt the accounting. Bytes
are not: the same byte count is a different duration on each side of a rate change.

## The reset that everything depends on

**A ledger belongs to one assistant audio *item*, and it resets when the item id
changes.** `truncate` takes an item id and an `audio_end_ms` measured *from the start of
that item's audio*. A global counter here is a guaranteed, silent, unbounded
corruption — it grows across items and every truncation after the first is wrong by the
length of everything before it. `note_enqueued` therefore takes the item id and resets
on change rather than trusting a caller to remember.
"""

from __future__ import annotations

from dataclasses import dataclass

from rn_core.errors import InvariantViolation
from rn_core.logging import get_logger
from rn_providers.audio.formats import AudioFormat, ms_of_bytes
from rn_voice.media.clock import Clock

__all__ = ["LedgerSnapshot", "PlaybackLedger"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """The ledger's state at one instant. What a golden file diffs."""

    item_id: str | None
    content_index: int
    enqueued_ms: float
    confirmed_ms: float
    estimate_ms: float

    @property
    def lag_ms(self) -> float:
        """`enqueued_ms - confirmed_ms` — a first-class health metric.

        Steady growth means we are outrunning the sink, which is the backpressure
        signal. It is also the width of the barge-in uncertainty window.
        """
        return self.enqueued_ms - self.confirmed_ms


class PlaybackLedger:
    """Tracks what the caller has actually heard, per assistant audio item.

    Args:
        fmt: The **telephony** format. Accounting is in the rate the audio is played
            at, not the rate the model emitted it at, because that is what the caller
            heard.
        clock: Injected, so a barge-in test is exact rather than tolerant.
    """

    __slots__ = (
        "_clock",
        "_confirmed_ms",
        "_content_index",
        "_enqueued_ms",
        "_format",
        "_frozen_ms",
        "_item_id",
        "_last_mark_at_ms",
        "_pending",
        "_resets",
        "_truncate_divergence_ms",
    )

    def __init__(self, *, fmt: AudioFormat, clock: Clock) -> None:
        self._format = fmt
        self._clock = clock
        self._item_id: str | None = None
        self._content_index = 0
        self._enqueued_ms = 0.0
        self._confirmed_ms = 0.0
        self._last_mark_at_ms = clock.monotonic_ms()
        # mark name -> cumulative enqueued ms at the moment it was written. A mark echo
        # confirms everything up to that point, so the value is a position, not a delta.
        self._pending: dict[str, float] = {}
        self._frozen_ms: float | None = None
        self._resets = 0
        self._truncate_divergence_ms = 0.0

    # -- state -------------------------------------------------------------

    @property
    def item_id(self) -> str | None:
        return self._item_id

    @property
    def content_index(self) -> int:
        return self._content_index

    @property
    def enqueued_ms(self) -> float:
        return self._enqueued_ms

    @property
    def confirmed_ms(self) -> float:
        return self._confirmed_ms

    @property
    def resets(self) -> int:
        """How many times the item changed. A test asserts two items produce two."""
        return self._resets

    @property
    def truncate_divergence_ms(self) -> float:
        """Total lateness of marks that arrived for audio we had already truncated past.

        A health metric, not a correction. Consistently large or growing divergence
        means the pacing lead has drifted or the provider's buffering differs from our
        model — and it shows up as conversational weirdness long before anyone traces it
        back here.
        """
        return self._truncate_divergence_ms

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            item_id=self._item_id,
            content_index=self._content_index,
            enqueued_ms=self._enqueued_ms,
            confirmed_ms=self._confirmed_ms,
            estimate_ms=self.estimate_played_ms(),
        )

    # -- accounting --------------------------------------------------------

    def note_enqueued(self, *, item_id: str, content_index: int, byte_count: int) -> float:
        """Record audio written to the telephony socket. Returns cumulative ms for the item.

        Resets automatically when `item_id` or `content_index` changes. That is not a
        convenience — it is the invariant this class exists to hold, and making it the
        caller's job is how the silent corruption in the module docstring happens.

        **Padding must not be counted.** The ring buffer pads a final short chunk with
        silence to reach the provider's floor; that silence is played but it is not the
        model's audio, and counting it would inflate `audio_end_ms` — the dangerous
        direction. The buffer therefore reports only real bytes here.
        """
        if byte_count < 0:
            raise InvariantViolation("Negative enqueued byte count.")
        if item_id != self._item_id or content_index != self._content_index:
            self._reset_for(item_id, content_index)
        self._enqueued_ms += ms_of_bytes(byte_count, self._format)
        return self._enqueued_ms

    def note_mark_written(self, name: str) -> None:
        """Record a mark we wrote, pinned to the audio position it follows."""
        self._pending[name] = self._enqueued_ms

    def note_mark_echoed(self, name: str) -> None:
        """The provider finished playing the audio before this mark (HC-9).

        An unknown name is ignored rather than raising: it is exactly what arrives after
        a `clear`, when the audio a mark was waiting on has been discarded, and it is
        also what a duplicate echo looks like. Neither is a reason to fail a live call.
        """
        position = self._pending.pop(name, None)
        if position is None:
            return
        if self._frozen_ms is not None and position > self._frozen_ms:
            # A late mark for audio we already truncated past. Recorded as a metric; it
            # must never retroactively raise the estimate, which would be the
            # over-reporting direction.
            self._truncate_divergence_ms += position - self._frozen_ms
        self._confirmed_ms = max(self._confirmed_ms, position)
        self._last_mark_at_ms = self._clock.monotonic_ms()

    def estimate_played_ms(self, now_ms: float | None = None) -> float:
        """What we would report as `audio_end_ms` right now.

        `min(enqueued, confirmed + elapsed_since_last_mark)`. The `min()` is the whole
        safety argument: extrapolation can only ever approach what we actually wrote,
        never exceed it, so a provider that plays slower than realtime makes us
        under-report rather than over-report.
        """
        if self._frozen_ms is not None:
            return self._frozen_ms
        now = self._clock.monotonic_ms() if now_ms is None else now_ms
        elapsed = max(0.0, now - self._last_mark_at_ms)
        return min(self._enqueued_ms, self._confirmed_ms + elapsed)

    # -- barge-in ----------------------------------------------------------

    def freeze(self, now_ms: float | None = None) -> float:
        """Fix the estimate for a barge-in, and stop it advancing.

        Called **first** in the barge-in sequence, before the clear and before the
        flush, because the pacer runs on another task and will happily advance
        `enqueued_ms` while the clear is being awaited. Freezing after that would report
        audio the caller never heard.

        Idempotent within an item: a second freeze returns the same value, so a
        duplicated trigger cannot inflate the number.
        """
        if self._frozen_ms is None:
            self._frozen_ms = self.estimate_played_ms(now_ms)
            _logger.info(
                "voice.playback.frozen",
                # Durations only — no transcript, no payload, no caller data.
                item_id=self._item_id,
                frozen_ms=round(self._frozen_ms, 3),
                enqueued_ms=round(self._enqueued_ms, 3),
                confirmed_ms=round(self._confirmed_ms, 3),
            )
        return self._frozen_ms

    @property
    def is_frozen(self) -> bool:
        return self._frozen_ms is not None

    def resume(self) -> None:
        """Release a freeze without changing the item.

        For the case where a barge-in turns out to be spurious. Deliberately does not
        clear `_pending`: marks still outstanding refer to audio that really was written.
        """
        self._frozen_ms = None
        self._last_mark_at_ms = self._clock.monotonic_ms()

    # -- internals ---------------------------------------------------------

    def _reset_for(self, item_id: str, content_index: int) -> None:
        self._item_id = item_id
        self._content_index = content_index
        self._enqueued_ms = 0.0
        self._confirmed_ms = 0.0
        self._frozen_ms = None
        self._pending.clear()
        self._last_mark_at_ms = self._clock.monotonic_ms()
        self._resets += 1
