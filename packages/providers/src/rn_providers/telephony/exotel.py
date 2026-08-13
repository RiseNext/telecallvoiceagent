"""Exotel AgentStream frame codec — and the one object that holds everything unverified.

> ## ⚠️ THE OUTBOUND FRAME SHAPE IS **[A] ASSUMED**, NOT CONFIRMED.
>
> Exotel's documentation says the outbound frame has "the same structure as incoming"
> and does **not** confirm whether `sequence_number`, `media.chunk` and
> `media.timestamp` are required or ignored, or whether `stream_sid` must be echoed
> (PROVIDER_CONSTRAINTS §6a-3). The sample-rate query parameter name is also
> unverified — `?sample-rate=16000` was seen exactly once and is uncorroborated
> (§6a-2, anti-fact #9) — as is the endpoint casing (§6a-1) and whether the byte
> thresholds scale with sample rate at all (§6a-4).
>
> **Every one of those assumptions is a field on `ExotelDialect` and nowhere else.**
> When the Phase-4G wire capture happens, settling all four is editing one frozen
> dataclass and re-running the tests. It is not a refactor. That isolation is the
> entire reason this object exists, and adding an `if` on a provider quirk anywhere
> else in this file defeats it.
>
> **Nothing in this module has been checked against a real Exotel socket.** No trace
> has been captured, and the fixtures in `tests/fixtures/telephony/` are
> hand-authored from the documented shapes — they are labelled as such and they are
> not evidence.

What **is** confirmed and is therefore not on the dialect object:

* **HC-1** — audio travels as base64 inside JSON **text** frames, never binary frames,
  and the codec is raw slin (s16le mono LE), never G.711. This is the constraint that
  makes a resampler unavoidable on this stack.
* **HC-2** — outbound payloads decode to a multiple of 320 bytes, ≥ 3200, ≤ 100000.
  Enforced by `ChunkPolicy`, not here.
* **HC-8** — barge-in is `{"event":"clear","stream_sid":...}`, and the documented
  example carries no `sequence_number`.
* **HC-9** — a mark we send is echoed once the audio preceding it has finished playing.

The encoder refuses to emit a payload that violates HC-2. That check is deliberately
duplicated with the ring buffer's: the buffer decides *what* to emit and the codec is
the last thing before the wire, and a rule enforced in exactly one place is a rule that
a future second call site skips.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Final

from rn_core.errors import InvariantViolation, ProviderError
from rn_providers.audio.formats import SUPPORTED_RATES, AudioFormat
from rn_providers.telephony.base import (
    ChunkPolicy,
    ConnectedEvent,
    DtmfEvent,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
    TelephonyEvent,
    chunk_policy_for,
)

__all__ = [
    "ASSUMED_DIALECT",
    "EXOTEL_ALIGNMENT_BYTES",
    "EXOTEL_MAX_CHUNK_BYTES",
    "EXOTEL_MIN_CHUNK_BYTES",
    "ExotelDialect",
    "decode_inbound_frame",
    "encode_clear_frame",
    "encode_mark_frame",
    "encode_media_frame",
    "exotel_chunk_policy",
]

#: HC-2 **[C]**. Absolute byte rules, confirmed against the provider's documentation.
#: Whether they *scale with sample rate* is §6a-4 and is **[A]**; the dialect carries
#: that question, these three constants carry the confirmed values.
EXOTEL_ALIGNMENT_BYTES: Final[int] = 320
EXOTEL_MIN_CHUNK_BYTES: Final[int] = 3200
EXOTEL_MAX_CHUNK_BYTES: Final[int] = 100_000


@dataclass(frozen=True, slots=True)
class ExotelDialect:
    """Every unverified Exotel wire assumption, in one place, each tagged with its item.

    Defaults are the **most conservative reading of the documentation**: send every
    field the inbound frame carries, on the theory that a provider ignoring a field it
    did not want is far more likely than a provider rejecting one it did. That is a
    judgement, not a fact, and it is exactly what the capture will replace.
    """

    #: §6a-3 **[A]** — whether the outbound frame must echo the stream id.
    echo_stream_sid: bool = True
    #: §6a-3 **[A]** — whether a top-level `sequence_number` is expected outbound.
    include_sequence_number: bool = True
    #: §6a-3 **[A]** — whether `media.chunk` is expected outbound.
    include_media_chunk: bool = True
    #: §6a-3 **[A]** — whether `media.timestamp` is expected outbound.
    include_media_timestamp: bool = True
    #: HC-8 **[C]** — the documented `clear` example carries no `sequence_number`.
    #: On the dialect anyway, because it is the same family of question and a capture
    #: that contradicts the example should be a one-line change like the rest.
    clear_includes_sequence_number: bool = False
    #: §6a-3 **[A]** — whether `mark` frames carry a sequence number.
    mark_includes_sequence_number: bool = False
    #: §6a-2 **[A]**, anti-fact #9 — seen once as `sample-rate`, uncorroborated.
    #: Could be `sample_rate` or `samplerate`. ADR-003 says read the negotiated rate
    #: back from the `start` event rather than trusting this.
    sample_rate_query_param: str = "sample-rate"
    #: §6a-1 **[A]** — canonical v1 docs show `/v1/Accounts/{sid}/Calls/connect`
    #: (PascalCase); the AgentStream developer guide renders it lowercase. These cannot
    #: both be right. PROVIDER_CONSTRAINTS says assume PascalCase.
    connect_path_pascal_case: bool = True
    #: §6a-4 **[A]** — whether the 320/3200/100000 byte rules scale with sample rate or
    #: are absolute at every rate. `False` means absolute, which is what
    #: `exotel_chunk_policy` implements and what ADR-003's latency table assumes.
    #: If the capture says otherwise, the 24 kHz default itself is back on the table.
    byte_thresholds_scale_with_rate: bool = False


#: The dialect in force until a capture replaces it. **Named so it cannot be mistaken
#: for a verified one**, and referenced by every caller rather than being a default
#: argument, so `grep ASSUMED_DIALECT` finds everything that rests on guesswork.
ASSUMED_DIALECT: Final[ExotelDialect] = ExotelDialect()


def exotel_chunk_policy(
    fmt: AudioFormat, *, dialect: ExotelDialect = ASSUMED_DIALECT
) -> ChunkPolicy:
    """The outbound write rules for a negotiated format.

    Raises:
        InvariantViolation: if the dialect claims the byte thresholds scale with rate.
            There is no documented scaling rule to implement — the question (§6a-4) is
            open — so a caller who flips that flag has to supply the answer along with
            it. Refusing is honest; inventing a scaling factor would produce numbers
            that look measured.
    """
    if dialect.byte_thresholds_scale_with_rate:
        raise InvariantViolation(
            "This dialect claims Exotel's byte thresholds scale with sample rate "
            "(§6a-4), but no scaling rule is documented and none has been measured. "
            "Settle it with the wire capture and implement the measured rule here.",
            detail={"rate_hz": fmt.rate_hz},
        )
    return chunk_policy_for(
        fmt,
        alignment_bytes=EXOTEL_ALIGNMENT_BYTES,
        min_bytes=EXOTEL_MIN_CHUNK_BYTES,
        max_bytes=EXOTEL_MAX_CHUNK_BYTES,
    )


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


def decode_inbound_frame(
    text: str, *, fallback_format: AudioFormat | None = None
) -> TelephonyEvent:
    """Parse one inbound JSON text frame into a typed event.

    Args:
        fallback_format: Used only when a `start` event does not report a
            `media_format` we can parse. Supplying it is how a caller says "I asked for
            this rate"; ADR-003 still prefers the provider's own answer when there is
            one, and this argument is never consulted if there is.

    Raises:
        ProviderError: for anything unparseable — malformed JSON, a missing `event`, an
            unknown event name, a payload that is not valid base64. These are
            *provider* failures, not caller errors, and they must not surface as a
            `ValueError` from three frames down.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "Inbound telephony frame is not valid JSON.", detail={"error": str(exc)}
        ) from exc
    if not isinstance(raw, dict):
        raise ProviderError("Inbound telephony frame is not a JSON object.")

    event = raw.get("event")
    if not isinstance(event, str):
        raise ProviderError("Inbound telephony frame carries no event name.")

    match event:
        case "connected":
            return ConnectedEvent()
        case "start":
            return _decode_start(raw, fallback_format)
        case "media":
            return _decode_media(raw)
        case "dtmf":
            return _decode_dtmf(raw)
        case "mark":
            return MarkEvent(name=str(_nested(raw, "mark", "name") or ""))
        case "stop":
            return StopEvent()
        case _:
            raise ProviderError("Unknown inbound telephony event.", detail={"event": event[:40]})


def _decode_start(raw: dict[str, Any], fallback: AudioFormat | None) -> StartEvent:
    start = raw.get("start")
    block: dict[str, Any] = start if isinstance(start, dict) else {}
    media_format = _decode_media_format(block.get("media_format"), fallback)
    parameters = block.get("custom_parameters")
    return StartEvent(
        stream_sid=str(raw.get("stream_sid") or block.get("stream_sid") or ""),
        call_sid=str(block.get("call_sid") or ""),
        media_format=media_format,
        custom_parameters=(
            {str(key): str(value) for key, value in parameters.items()}
            if isinstance(parameters, dict)
            else {}
        ),
    )


def _decode_media_format(raw: Any, fallback: AudioFormat | None) -> AudioFormat:
    """Read the negotiated format from the provider, preferring it over what we asked.

    ADR-003 is explicit that the rate is read back here where possible, because the
    query parameter that requests it is unverified. A provider-reported rate outside
    `SUPPORTED_RATES` is refused rather than coerced: running the whole media path at a
    rate no component was written for produces audio that is wrong in a way nobody
    traces back to a missing branch.
    """
    if isinstance(raw, dict):
        rate = raw.get("sample_rate", raw.get("rate"))
        if rate is not None:
            try:
                parsed = int(rate)
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    "Telephony start event reported an unparseable sample rate.",
                    detail={"sample_rate": str(rate)[:20]},
                ) from exc
            if parsed not in SUPPORTED_RATES:
                raise ProviderError(
                    "Telephony start event reported an unsupported sample rate.",
                    detail={"sample_rate": parsed, "supported": sorted(SUPPORTED_RATES)},
                )
            return AudioFormat(rate_hz=parsed)
    if fallback is not None:
        return fallback
    raise ProviderError(
        "Telephony start event reported no media format and no fallback was supplied."
    )


def _decode_media(raw: dict[str, Any]) -> MediaEvent:
    payload = _nested(raw, "media", "payload")
    if not isinstance(payload, str):
        raise ProviderError("Inbound media frame carries no base64 payload.")
    try:
        # `validate=True` so that a payload containing non-alphabet characters is an
        # error rather than being silently discarded, which would deliver short audio.
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError(
            "Inbound media payload is not valid base64.", detail={"error": str(exc)}
        ) from exc
    return MediaEvent(
        payload=decoded,
        sequence_number=_optional_int(raw.get("sequence_number")),
        chunk=_optional_int(_nested(raw, "media", "chunk")),
        timestamp_ms=_optional_int(_nested(raw, "media", "timestamp")),
    )


def _decode_dtmf(raw: dict[str, Any]) -> DtmfEvent:
    digit = _nested(raw, "dtmf", "digit")
    if digit is None:
        raise ProviderError("Inbound dtmf frame carries no digit.")
    return DtmfEvent(digit=str(digit))


def _nested(raw: dict[str, Any], outer: str, inner: str) -> Any:
    block = raw.get(outer)
    return block.get(inner) if isinstance(block, dict) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # Advisory fields only. A provider sending a non-numeric sequence number is
        # odd, but it is not a reason to drop a frame of audio on the floor.
        return None


# --------------------------------------------------------------------------
# Encoding — every field set here is [A]. See the module banner.
# --------------------------------------------------------------------------


def encode_media_frame(
    payload: bytes,
    *,
    stream_sid: str,
    policy: ChunkPolicy,
    sequence_number: int,
    chunk_index: int,
    timestamp_ms: int,
    dialect: ExotelDialect = ASSUMED_DIALECT,
) -> str:
    """Encode one outbound audio payload as a JSON text frame.

    Raises:
        InvariantViolation: if the payload does not satisfy `policy`. HC-2 is a hard
            provider rule and an illegal write produces choppy audio that the whole
            team initially misdiagnoses as a network problem — so it is refused at the
            last possible moment as well as at the buffer that produced it.
    """
    if not policy.is_legal(len(payload)):
        raise InvariantViolation(
            "Refusing to write an outbound payload that violates the chunk policy.",
            detail={
                "bytes": len(payload),
                "min": policy.effective_min,
                "max": policy.effective_max,
                "alignment": policy.effective_alignment,
            },
        )
    media: dict[str, Any] = {"payload": base64.b64encode(payload).decode("ascii")}
    if dialect.include_media_chunk:
        media["chunk"] = chunk_index
    if dialect.include_media_timestamp:
        # A string, matching the inbound shape in REALTIME_VOICE §1.1 where `timestamp`
        # is quoted. Whether Exotel cares is §6a-3.
        media["timestamp"] = str(timestamp_ms)

    frame: dict[str, Any] = {"event": "media"}
    if dialect.include_sequence_number:
        frame["sequence_number"] = sequence_number
    if dialect.echo_stream_sid:
        frame["stream_sid"] = stream_sid
    frame["media"] = media
    return _dumps(frame)


def encode_mark_frame(
    name: str,
    *,
    stream_sid: str,
    sequence_number: int,
    dialect: ExotelDialect = ASSUMED_DIALECT,
) -> str:
    """Encode a mark. One per emitted chunk (REALTIME_VOICE §3).

    Per chunk rather than per utterance, deliberately: it costs one tiny frame per
    chunk — 12.5/s/call at 80 ms chunks — and it bounds the barge-in uncertainty window
    to a single chunk instead of a whole utterance.
    """
    frame: dict[str, Any] = {"event": "mark"}
    if dialect.mark_includes_sequence_number:
        frame["sequence_number"] = sequence_number
    if dialect.echo_stream_sid:
        frame["stream_sid"] = stream_sid
    frame["mark"] = {"name": name}
    return _dumps(frame)


def encode_clear_frame(
    *,
    stream_sid: str,
    sequence_number: int,
    dialect: ExotelDialect = ASSUMED_DIALECT,
) -> str:
    """Encode a barge-in clear (HC-8).

    Discards what Exotel has buffered and not yet played. It does **not** stop our
    generator and it does **not** tell the model anything — which is why barge-in is
    three operations with one call site and this is only the first of them.
    """
    frame: dict[str, Any] = {"event": "clear"}
    if dialect.clear_includes_sequence_number:
        frame["sequence_number"] = sequence_number
    if dialect.echo_stream_sid:
        frame["stream_sid"] = stream_sid
    return _dumps(frame)


def _dumps(frame: dict[str, Any]) -> str:
    """Compact, key-order-stable JSON.

    `separators` because at 12.5 frames/second/call/direction the whitespace is real
    bandwidth, and `sort_keys=False` because the insertion order above is chosen to
    read like the documented examples when someone is diffing a capture against it.
    """
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
