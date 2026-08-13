"""Audio formats and transcoding, at the telephony-adapter boundary (ADR-003).

`numpy` and `soxr` are imported lazily inside `PolyphaseTranscoder`, not here, so that
importing this package costs nothing to a process that only needs `AudioFormat` — the
worker and the API never touch a media frame and should not pay for numpy to say so.
"""

from rn_providers.audio.formats import (
    FRAME_MS,
    PCM_8K,
    PCM_16K,
    PCM_24K,
    SAMPLE_WIDTH_BYTES,
    SUPPORTED_RATES,
    AudioEncoding,
    AudioFormat,
    bytes_of_ms,
    ms_of_bytes,
)
from rn_providers.audio.transcoder import (
    AudioTranscoder,
    PassthroughTranscoder,
    PolyphaseTranscoder,
    ResamplerQuality,
    resolve_transcoder,
)

__all__ = [
    "FRAME_MS",
    "PCM_8K",
    "PCM_16K",
    "PCM_24K",
    "SAMPLE_WIDTH_BYTES",
    "SUPPORTED_RATES",
    "AudioEncoding",
    "AudioFormat",
    "AudioTranscoder",
    "PassthroughTranscoder",
    "PolyphaseTranscoder",
    "ResamplerQuality",
    "bytes_of_ms",
    "ms_of_bytes",
    "resolve_transcoder",
]
