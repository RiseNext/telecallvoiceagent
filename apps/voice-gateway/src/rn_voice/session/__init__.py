"""REALTIME SESSION. The bridge between a telephony stream and a voice provider.

Drives `rn_voice.media` through its interfaces; `media` knows nothing above it, and an
import-linter contract keeps that layering executable rather than aspirational.

Phase 4 ships the bridge only, driven by fakes. Session pre-warming, the 10-second
connect deadline, tool dispatch on its own task, the snapshot cache and rollover across
the two independent 60-minute clocks are **Phase 5**.
"""

from rn_voice.session.bridge import AudioBridge, BridgeResult, ToolCallSink

__all__ = ["AudioBridge", "BridgeResult", "ToolCallSink"]
