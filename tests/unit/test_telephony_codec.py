"""`ChunkPolicy` arithmetic and the Exotel frame codec.

The chunk-policy tests are the important half. HC-2's byte rules and our 20 ms
accounting quantum interact in a way that produces three different minimum emissions at
three rates, and getting that arithmetic wrong is not an error — it is `audio_end_ms`
drifting, which fails silently and corrupts a conversation.

The codec tests are checked against **[A] assumed** shapes. They assert that the encoder
does what `ExotelDialect` says, not that `ExotelDialect` is right about Exotel. Nothing
here is evidence about the provider; see Phase 4G.
"""

from __future__ import annotations

import base64
import json

import pytest

from rn_core.errors import InvariantViolation, ProviderError
from rn_providers.audio.formats import PCM_8K, PCM_16K, PCM_24K, AudioFormat, ms_of_bytes
from rn_providers.telephony.base import (
    ChunkPolicy,
    ConnectedEvent,
    DtmfEvent,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
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

#: `provider`: this is the Exotel adapter's codec, exercised against a mocked wire.
pytestmark = pytest.mark.provider


# ---------------------------------------------------------------------------
# The derivation ADR-003 states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "alignment", "minimum", "minimum_ms"),
    [
        (PCM_8K, 320, 3200, 200.0),
        (PCM_16K, 640, 3200, 100.0),
        (PCM_24K, 960, 3840, 80.0),
    ],
    ids=["8k", "16k", "24k"],
)
def test_the_three_rates_produce_the_documented_chunk_rules(
    fmt: AudioFormat, alignment: int, minimum: int, minimum_ms: float
) -> None:
    """These six numbers are ADR-003 and REALTIME_VOICE §1.4, derived rather than typed.

    The 24 kHz row is the one that matters: 320 bytes there is 6.667 ms, and
    accumulating playback in units of 6.667 ms drifts `audio_end_ms`. Reconciling the
    provider's 320 with our 960-byte frame quantum is what turns the floor into 3840.
    """
    policy = exotel_chunk_policy(fmt)
    assert policy.effective_alignment == alignment
    assert policy.effective_min == minimum
    assert ms_of_bytes(policy.effective_min, fmt) == minimum_ms


def test_every_legal_emission_is_a_whole_number_of_milliseconds() -> None:
    """The property the frame quantum exists to guarantee. Without it, 24 kHz
    accounting accumulates two-thirds of a millisecond of error per chunk."""
    for fmt in (PCM_8K, PCM_16K, PCM_24K):
        policy = exotel_chunk_policy(fmt)
        for multiple in range(1, 20):
            size = policy.effective_alignment * multiple
            assert ms_of_bytes(size, fmt).is_integer()


def test_the_ceiling_is_rounded_down_to_alignment() -> None:
    """100000 is not a multiple of 960, so the 24 kHz ceiling is 99840 — and a chunk of
    exactly 100000 bytes would be rejected by the provider."""
    policy = exotel_chunk_policy(PCM_24K)
    assert policy.effective_max == 99_840
    assert policy.effective_max <= EXOTEL_MAX_CHUNK_BYTES
    assert not policy.is_legal(EXOTEL_MAX_CHUNK_BYTES)


def test_the_confirmed_constants_are_the_documented_ones() -> None:
    assert (EXOTEL_ALIGNMENT_BYTES, EXOTEL_MIN_CHUNK_BYTES, EXOTEL_MAX_CHUNK_BYTES) == (
        320,
        3200,
        100_000,
    )


def test_legality_is_floor_ceiling_and_alignment_together() -> None:
    policy = exotel_chunk_policy(PCM_24K)
    assert not policy.is_legal(policy.effective_min - policy.effective_alignment)
    assert policy.is_legal(policy.effective_min)
    assert not policy.is_legal(policy.effective_min + 1)
    assert policy.is_legal(policy.effective_max)
    assert not policy.is_legal(policy.effective_max + policy.effective_alignment)


def test_the_emission_size_is_the_smallest_legal_chunk_not_the_largest() -> None:
    """**A design decision, pinned.** The barge-in uncertainty window is exactly one
    chunk, so chunk size *is* the window: 80 ms at 24 kHz if we emit the minimum, 2.08
    seconds if we emit the provider's maximum. Emitting "as much as is buffered" is the
    obvious implementation and it silently destroys the property the pacer exists for.
    """
    policy = exotel_chunk_policy(PCM_24K)
    assert policy.emission_size(policy.effective_min - 1) == 0
    assert policy.emission_size(policy.effective_min) == policy.effective_min
    assert policy.emission_size(10**7) == policy.effective_min
    assert ms_of_bytes(policy.emission_size(10**7), PCM_24K) == 80.0
    # And the ceiling arithmetic still exists for callers that genuinely want it.
    assert policy.largest_legal_chunk(10**7) == policy.effective_max


def test_largest_legal_chunk_is_zero_when_too_little_is_available() -> None:
    """ "Not enough buffered yet" is the normal state between deltas, not an error."""
    policy = exotel_chunk_policy(PCM_24K)
    assert policy.largest_legal_chunk(policy.effective_min - 1) == 0
    assert policy.largest_legal_chunk(policy.effective_min) == policy.effective_min
    assert policy.largest_legal_chunk(policy.effective_min + 100) == policy.effective_min
    assert policy.largest_legal_chunk(10**7) == policy.effective_max


def test_a_policy_with_no_legal_size_is_refused() -> None:
    """A quantum larger than the provider's window would mean nothing could ever be
    emitted — which must fail loudly rather than as a pacer that silently never writes."""
    with pytest.raises(InvariantViolation):
        ChunkPolicy(alignment_bytes=320, min_bytes=3200, max_bytes=3300, frame_quantum_bytes=2000)


def test_a_dialect_claiming_the_thresholds_scale_is_refused() -> None:
    """§6a-4 is open. A flag that claims it is settled must come with the measured rule;
    inventing a scaling factor would produce numbers that look measured and are not."""
    with pytest.raises(InvariantViolation, match="6a-4"):
        exotel_chunk_policy(PCM_24K, dialect=ExotelDialect(byte_thresholds_scale_with_rate=True))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def test_a_media_frame_decodes_to_raw_pcm() -> None:
    payload = b"\x01\x02\x03\x04"
    frame = json.dumps(
        {
            "event": "media",
            "sequence_number": 412,
            "stream_sid": "s",
            "media": {
                "chunk": 411,
                "timestamp": "8240",
                "payload": base64.b64encode(payload).decode(),
            },
        }
    )
    event = decode_inbound_frame(frame)
    assert isinstance(event, MediaEvent)
    assert event.payload == payload
    assert (event.sequence_number, event.chunk, event.timestamp_ms) == (412, 411, 8240)


def test_the_start_event_is_where_the_rate_is_read_from() -> None:
    """ADR-003: the query parameter that *requests* a rate is unverified (§6a-2), so the
    negotiated rate is whatever the provider reports here."""
    frame = json.dumps(
        {
            "event": "start",
            "stream_sid": "stream-1",
            "start": {
                "call_sid": "call-1",
                "media_format": {"encoding": "raw", "sample_rate": 8000, "bit_rate": "16"},
                "custom_parameters": {"session_id": "abc"},
            },
        }
    )
    event = decode_inbound_frame(frame, fallback_format=PCM_24K)
    assert isinstance(event, StartEvent)
    assert event.media_format == PCM_8K, "the provider's answer must beat our fallback"
    assert event.custom_parameters == {"session_id": "abc"}


def test_a_start_event_with_no_format_falls_back_only_if_offered() -> None:
    frame = json.dumps({"event": "start", "start": {"call_sid": "c"}})
    assert decode_inbound_frame(frame, fallback_format=PCM_8K).media_format == PCM_8K  # type: ignore[union-attr]
    with pytest.raises(ProviderError):
        decode_inbound_frame(frame)


def test_an_unsupported_reported_rate_is_refused_not_coerced() -> None:
    """Running the media path at a rate no component was written for produces audio that
    is wrong in a way nobody traces back to a missing branch."""
    frame = json.dumps({"event": "start", "start": {"media_format": {"sample_rate": 44100}}})
    with pytest.raises(ProviderError, match="unsupported sample rate"):
        decode_inbound_frame(frame)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[]",
        '{"no_event": 1}',
        '{"event": "teleport"}',
        '{"event": "media", "media": {}}',
        '{"event": "media", "media": {"payload": "!!!not base64!!!"}}',
        '{"event": "dtmf", "dtmf": {}}',
    ],
    ids=["nonjson", "notobject", "noevent", "unknown", "nopayload", "badbase64", "nodigit"],
)
def test_every_malformed_frame_raises_a_provider_error(raw: str) -> None:
    """A provider failure, never a bare `ValueError` from three frames down."""
    with pytest.raises(ProviderError):
        decode_inbound_frame(raw)


def test_the_lifecycle_events_decode() -> None:
    assert isinstance(decode_inbound_frame('{"event":"connected"}'), ConnectedEvent)
    assert isinstance(decode_inbound_frame('{"event":"stop"}'), StopEvent)
    assert decode_inbound_frame('{"event":"dtmf","dtmf":{"digit":"5"}}') == DtmfEvent(digit="5")
    assert decode_inbound_frame('{"event":"mark","mark":{"name":"m1"}}') == MarkEvent(name="m1")


def test_an_unparseable_sequence_number_does_not_drop_the_audio() -> None:
    """Advisory fields only. Dropping a frame of caller audio over a malformed counter
    would be a worse bug than the counter."""
    frame = json.dumps(
        {
            "event": "media",
            "sequence_number": "not-a-number",
            "media": {"payload": base64.b64encode(b"\x00\x00").decode()},
        }
    )
    event = decode_inbound_frame(frame)
    assert isinstance(event, MediaEvent)
    assert event.sequence_number is None
    assert event.payload == b"\x00\x00"


# ---------------------------------------------------------------------------
# Encoding — [A] shapes, isolated behind the dialect
# ---------------------------------------------------------------------------


def _legal_payload(fmt: AudioFormat = PCM_24K) -> bytes:
    return b"\x00" * exotel_chunk_policy(fmt).effective_min


def test_the_assumed_dialect_emits_every_documented_field() -> None:
    """The conservative reading: send everything the inbound frame carries. A provider
    ignoring an unwanted field is likelier than one rejecting a field it did want."""
    frame = json.loads(
        encode_media_frame(
            _legal_payload(),
            stream_sid="stream-1",
            policy=exotel_chunk_policy(PCM_24K),
            sequence_number=7,
            chunk_index=6,
            timestamp_ms=1200,
            dialect=ASSUMED_DIALECT,
        )
    )
    assert frame["event"] == "media"
    assert frame["stream_sid"] == "stream-1"
    assert frame["sequence_number"] == 7
    assert frame["media"]["chunk"] == 6
    assert frame["media"]["timestamp"] == "1200"
    assert base64.b64decode(frame["media"]["payload"]) == _legal_payload()


def test_every_unverified_field_is_switchable_from_one_object() -> None:
    """**The isolation this design exists for.** When the capture lands, settling §6a-3
    is editing one frozen dataclass — not a refactor of the encoder."""
    minimal = ExotelDialect(
        echo_stream_sid=False,
        include_sequence_number=False,
        include_media_chunk=False,
        include_media_timestamp=False,
    )
    frame = json.loads(
        encode_media_frame(
            _legal_payload(),
            stream_sid="stream-1",
            policy=exotel_chunk_policy(PCM_24K),
            sequence_number=7,
            chunk_index=6,
            timestamp_ms=1200,
            dialect=minimal,
        )
    )
    assert set(frame) == {"event", "media"}
    assert set(frame["media"]) == {"payload"}


def test_an_illegal_payload_is_refused_at_the_encoder_too() -> None:
    """Duplicated with the ring buffer's check on purpose: this is the last thing before
    the wire, and a rule enforced in exactly one place is one a second call site skips."""
    with pytest.raises(InvariantViolation):
        encode_media_frame(
            b"\x00" * 100,
            stream_sid="s",
            policy=exotel_chunk_policy(PCM_24K),
            sequence_number=0,
            chunk_index=0,
            timestamp_ms=0,
        )


def test_the_clear_frame_matches_the_documented_example() -> None:
    """HC-8: the documented example carries no `sequence_number`."""
    frame = json.loads(encode_clear_frame(stream_sid="stream-1", sequence_number=9))
    assert frame == {"event": "clear", "stream_sid": "stream-1"}


def test_a_mark_frame_carries_its_name() -> None:
    frame = json.loads(encode_mark_frame("m-1", stream_sid="stream-1", sequence_number=9))
    assert frame["event"] == "mark"
    assert frame["mark"] == {"name": "m-1"}


def test_encoded_frames_round_trip_through_the_decoder() -> None:
    """The two halves of the codec must agree, whatever the dialect turns out to be."""
    payload = _legal_payload(PCM_8K)
    encoded = encode_media_frame(
        payload,
        stream_sid="s",
        policy=exotel_chunk_policy(PCM_8K),
        sequence_number=1,
        chunk_index=1,
        timestamp_ms=20,
    )
    decoded = decode_inbound_frame(encoded)
    assert isinstance(decoded, MediaEvent)
    assert decoded.payload == payload


def test_the_assumed_dialect_is_documented_as_assumed() -> None:
    """A guard against someone quietly renaming it to something reassuring. Every
    default on it is [A]; the name is the warning that travels with it."""
    from rn_providers.telephony import exotel as module

    assert ExotelDialect() == module.ASSUMED_DIALECT
    assert "[A]" in (module.ExotelDialect.__doc__ or "") or "**[A]" in (module.__doc__ or "")
