"""The clock the audio path reads. Injected, never global.

Barge-in correctness is a **timing relationship**: `audio_end_ms` is derived from how
long it has been since the last confirmed mark, and a wrong answer corrupts the model's
belief about the conversation without producing an error. A relationship like that is
only testable if the test controls time.

So **nothing in `rn_voice.media` may call `time.monotonic()` directly.** Every timeout,
the pacer's drain schedule and the ledger's wall-clock extrapolation take a clock from
the session. `ManualClock` then makes a barge-in test exact instead of tolerant, and a
tolerant timing test is one that passes on a fast machine and fails in CI.

Milliseconds throughout, matching the playback ledger's unit. Monotonic, never wall
clock: a system clock adjustment mid-call would otherwise make `audio_end_ms` jump.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "ManualClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """A monotonic millisecond clock."""

    def monotonic_ms(self) -> float:
        """Milliseconds since an arbitrary epoch. Never decreases."""
        ...


class SystemClock:
    """The real clock. The only place the audio path touches `time`."""

    __slots__ = ()

    def monotonic_ms(self) -> float:
        return time.monotonic() * 1000.0


class ManualClock:
    """A clock a test advances by hand.

    Deliberately has no automatic advance: a fake clock that ticks on read makes a test
    that *looks* deterministic and is not, because the number of reads becomes part of
    the result.
    """

    __slots__ = ("_now_ms",)

    def __init__(self, start_ms: float = 0.0) -> None:
        self._now_ms = start_ms

    def monotonic_ms(self) -> float:
        return self._now_ms

    def advance(self, milliseconds: float) -> None:
        if milliseconds < 0:
            raise ValueError("A monotonic clock cannot go backwards.")
        self._now_ms += milliseconds
