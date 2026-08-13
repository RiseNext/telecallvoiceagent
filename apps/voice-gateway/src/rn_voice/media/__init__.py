"""MEDIA TRANSPORT. Bytes, buffers, timings. Framework-free. **Permanent.**

Everything in this package moves audio and accounts for it: the pacing and alignment
ring buffer, played-millisecond accounting, barge-in mechanics, and the clock they all
read. Nothing here knows what a conversation is.

**This package may never import LangChain, LangGraph, `rn_orchestration`, `rn_agent` or
`rn_services`**, and an import-linter contract enforces it rather than a convention
asking nicely. The contract does not relax for a benchmark, because the cost of
coupling audio transport to a graph framework is not primarily latency — it is that the
transport stops being independently testable, replaceable and reasonable about.

What it *may* import is `rn_providers`: the chunk rules a telephony adapter declares
and the session protocol a voice adapter satisfies are both transport facts. What it
must never do is decide anything about the conversation those bytes carry.
"""

from rn_voice.media.align import SampleAligner
from rn_voice.media.bargein import BargeInOutcome, ClearPlayback, handle_barge_in
from rn_voice.media.clock import Clock, ManualClock, SystemClock
from rn_voice.media.ledger import LedgerSnapshot, PlaybackLedger
from rn_voice.media.pacer import DEFAULT_LEAD_CHUNKS, Pacer, PacerSinks
from rn_voice.media.ring import OutboundChunk, OutboundRingBuffer
from rn_voice.media.tap import MediaDirection, MediaTap, NullMediaTap

__all__ = [
    "DEFAULT_LEAD_CHUNKS",
    "BargeInOutcome",
    "ClearPlayback",
    "Clock",
    "LedgerSnapshot",
    "ManualClock",
    "MediaDirection",
    "MediaTap",
    "NullMediaTap",
    "OutboundChunk",
    "OutboundRingBuffer",
    "Pacer",
    "PacerSinks",
    "PlaybackLedger",
    "SampleAligner",
    "SystemClock",
    "handle_barge_in",
]
