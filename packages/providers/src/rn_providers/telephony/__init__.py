"""Telephony seams and the Exotel frame codec.

The seam (`base`) is provider-neutral. The Exotel specifics (`exotel`) are one codec
plus `ExotelDialect`, which holds every wire assumption that has not been verified
against a real socket — see that module's banner before trusting a byte of it.
"""

from rn_providers.telephony.base import (
    ChunkPolicy,
    ConnectedEvent,
    DtmfEvent,
    FrameDecodeFailed,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
    TelephonyEvent,
    TelephonyTransport,
    chunk_policy_for,
)
from rn_providers.telephony.exotel import (
    ASSUMED_DIALECT,
    EXOTEL_ALIGNMENT_BYTES,
    EXOTEL_MAX_CHUNK_BYTES,
    EXOTEL_MIN_CHUNK_BYTES,
    ExotelDialect,
    decode_inbound_frame,
    encode_clear_frame,
    encode_mark_frame,
    encode_media_frame,
    exotel_chunk_policy,
)

__all__ = [
    "ASSUMED_DIALECT",
    "EXOTEL_ALIGNMENT_BYTES",
    "EXOTEL_MAX_CHUNK_BYTES",
    "EXOTEL_MIN_CHUNK_BYTES",
    "ChunkPolicy",
    "ConnectedEvent",
    "DtmfEvent",
    "ExotelDialect",
    "FrameDecodeFailed",
    "MarkEvent",
    "MediaEvent",
    "StartEvent",
    "StopEvent",
    "TelephonyEvent",
    "TelephonyTransport",
    "chunk_policy_for",
    "decode_inbound_frame",
    "encode_clear_frame",
    "encode_mark_frame",
    "encode_media_frame",
    "exotel_chunk_policy",
]
