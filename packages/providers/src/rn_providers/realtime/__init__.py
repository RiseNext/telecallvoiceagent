"""Realtime voice seams.

The seam only. The OpenAI Realtime adapter is **Phase 5** and lands beside this as
`rn_providers.realtime.openai`, against the GA interface — no `OpenAI-Beta` header, no
`session.input_audio_format` string, no `g711_ulaw` enum, all of which were removed on
2026-05-12 (HC-16). Assume every tutorial older than mid-2026 is wrong.
"""

from rn_providers.realtime.session import (
    AudioDelta,
    ErrorEvent,
    ResponseComplete,
    SessionCapabilities,
    SessionClosed,
    SpeechStarted,
    SpeechStopped,
    ToolCallRequested,
    TranscriptDelta,
    VoiceSession,
    VoiceSessionEvent,
)

__all__ = [
    "AudioDelta",
    "ErrorEvent",
    "ResponseComplete",
    "SessionCapabilities",
    "SessionClosed",
    "SpeechStarted",
    "SpeechStopped",
    "ToolCallRequested",
    "TranscriptDelta",
    "VoiceSession",
    "VoiceSessionEvent",
]
