"""The two Phase-4 fakes, against what TESTING.md §3.2 and §3.3 require of them.

These are conformance tests for the *fakes themselves*. They matter more than they look:
every other Phase-4 assertion is made through these fakes, so a fake that quietly fails
to inject a fault is a whole class of production failure that is never exercised.

Each test names the documented requirement it covers.
"""

from __future__ import annotations

from typing import Any

import pytest

from rn_core.errors import InvariantViolation
from rn_providers.audio.formats import PCM_8K, PCM_24K, bytes_of_ms, ms_of_bytes
from rn_providers.fakes.realtime import (
    OPENAI_LIKE_CAPABILITIES,
    CloseSession,
    DropSocket,
    EmitAudio,
    EmitError,
    EmitToolCall,
    FakeRealtimeProvider,
    GoSilent,
)
from rn_providers.fakes.telephony import (
    FakeTelephonyProvider,
    InboundAudio,
    MalformedFrame,
    Pace,
    Stop,
    TelephonyFaults,
)
from rn_providers.realtime.session import (
    AudioDelta,
    ErrorEvent,
    SessionClosed,
)
from rn_providers.telephony.base import (
    FrameDecodeFailed,
    MarkEvent,
    MediaEvent,
    StartEvent,
    StopEvent,
)
from rn_voice.media.clock import ManualClock

pytestmark = pytest.mark.provider


async def _drain(server: FakeTelephonyProvider) -> list[Any]:
    return [event async for event in server.events()]


# ---------------------------------------------------------------------------
# TESTING §3.2 — inbound fault injection
# ---------------------------------------------------------------------------


async def test_dropped_frames_are_not_delivered() -> None:
    """Packet loss, as the application sees it."""
    plain = await _drain(FakeTelephonyProvider([InboundAudio(milliseconds=100), Stop()]))
    lossy = await _drain(
        FakeTelephonyProvider(
            [InboundAudio(milliseconds=100), Stop()],
            faults=TelephonyFaults(drop_frames=frozenset({1, 2})),
        )
    )
    media = sum(1 for event in plain if isinstance(event, MediaEvent))
    lossy_media = sum(1 for event in lossy if isinstance(event, MediaEvent))
    assert media == 5
    assert lossy_media == 3


async def test_a_duplicated_frame_arrives_twice() -> None:
    """Providers retry. The application must tolerate it."""
    events = await _drain(
        FakeTelephonyProvider(
            [InboundAudio(milliseconds=60), Stop()],
            faults=TelephonyFaults(duplicate_frame=1),
        )
    )
    assert sum(1 for event in events if isinstance(event, MediaEvent)) == 4


async def test_a_sequence_jump_does_not_break_the_stream() -> None:
    """Nothing may depend on `sequence_number` contiguity."""
    events = await _drain(
        FakeTelephonyProvider(
            [InboundAudio(milliseconds=100), Stop()],
            faults=TelephonyFaults(sequence_jump_at=1),
        )
    )
    numbers = [event.sequence_number for event in events if isinstance(event, MediaEvent)]
    assert numbers == [0, 1, 1002, 1003, 1004]


async def test_closing_without_stop_ends_the_stream_anyway() -> None:
    """A dropped socket rather than a hang-up. The bridge must finalise on iterator
    exhaustion, not on a `stop` event that never comes."""
    events = await _drain(
        FakeTelephonyProvider(
            [InboundAudio(milliseconds=40)], faults=TelephonyFaults(close_without_stop=True)
        )
    )
    assert not any(isinstance(event, StopEvent) for event in events)
    assert any(isinstance(event, MediaEvent) for event in events)


async def test_the_start_event_can_be_delayed_to_probe_the_connect_deadline() -> None:
    """HC-5: the bot must respond within ~10 s of connect. Holding `start` back is how a
    test proves the bridge does not wait for it before making a sound."""
    server = FakeTelephonyProvider(
        [Stop()], faults=TelephonyFaults(delay_start_event_ms=9500), pace=Pace.INSTANT
    )
    events = await _drain(server)
    assert any(isinstance(event, StartEvent) for event in events)


async def test_a_malformed_frame_is_reported_as_an_event_not_raised() -> None:
    """Raising would end the iterator and therefore the call, over one bad frame."""
    events = await _drain(FakeTelephonyProvider([MalformedFrame(raw="{not json"), Stop()]))
    assert any(isinstance(event, FrameDecodeFailed) for event in events)
    assert any(isinstance(event, StopEvent) for event in events)


# ---------------------------------------------------------------------------
# TESTING §3.2 — mark echoes
# ---------------------------------------------------------------------------


async def test_mark_loss_is_deterministic_not_random() -> None:
    """A random loss rate produces a test that fails one run in twenty. Every run of this
    must drop the same marks."""
    seen: list[tuple[str, ...]] = []
    for _ in range(3):
        server = FakeTelephonyProvider(
            [InboundAudio(milliseconds=20), Stop()],
            faults=TelephonyFaults(mark_loss_rate=0.5),
        )
        for index in range(6):
            await server.send_mark(f"m-{index}")
        events = await _drain(server)
        seen.append(tuple(event.name for event in events if isinstance(event, MarkEvent)))
    assert seen[0] == seen[1] == seen[2]
    assert len(seen[0]) < 6, "a 50% loss rate must actually lose marks"


async def test_a_total_mark_loss_echoes_nothing() -> None:
    """The pathological case the ledger's `min()` clamp exists for."""
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=20), Stop()],
        faults=TelephonyFaults(mark_loss_rate=1.0),
    )
    await server.send_mark("m-0")
    events = await _drain(server)
    assert not any(isinstance(event, MarkEvent) for event in events)


# ---------------------------------------------------------------------------
# TESTING §3.2 — the outbound assertion sink
# ---------------------------------------------------------------------------


async def test_an_odd_byte_count_is_refused() -> None:
    """s16le framing: an odd count means a split sample, and every subsequent sample
    would be byte-swapped — loud static, easily blamed on the network."""
    server = FakeTelephonyProvider(media_format=PCM_24K)
    with pytest.raises(InvariantViolation, match="odd byte count"):
        await server.send_media(b"\x00" * (server.chunk_policy.effective_min + 1))


async def test_an_unaligned_payload_is_refused() -> None:
    server = FakeTelephonyProvider(media_format=PCM_24K)
    with pytest.raises(InvariantViolation, match="illegal outbound media payload"):
        await server.send_media(b"\x00" * (server.chunk_policy.effective_min + 2))


async def test_a_payload_below_the_floor_is_refused() -> None:
    server = FakeTelephonyProvider(media_format=PCM_8K)
    with pytest.raises(InvariantViolation):
        await server.send_media(b"\x00" * 320)


async def test_outbound_audio_that_outruns_real_time_is_refused() -> None:
    """**Nothing else can catch this.** A bridge that dumps a whole response passes every
    alignment and size check, and then barge-in accounting is guessing across the entire
    utterance instead of across one chunk."""
    clock = ManualClock()
    server = FakeTelephonyProvider(
        media_format=PCM_24K,
        clock_ms=clock.monotonic_ms,
        jitter_allowance_ms=200.0,
    )
    chunk = b"\x00" * server.chunk_policy.effective_min  # 80 ms of audio

    # The clock never advances, so every write puts another 80 ms into a sink that has
    # played nothing. The allowance absorbs the first couple; a dumped response cannot
    # get far past it.
    with pytest.raises(InvariantViolation, match="outrunning real time"):
        for _ in range(10):
            await server.send_media(chunk)
    assert len(server.media_frames) < 5, "the sink must complain early, not after a whole response"


async def test_pacing_at_real_time_is_accepted() -> None:
    """The same writes, paced. The assertion must not fire on correct behaviour."""
    clock = ManualClock()
    server = FakeTelephonyProvider(
        media_format=PCM_24K, clock_ms=clock.monotonic_ms, jitter_allowance_ms=200.0
    )
    chunk = b"\x00" * server.chunk_policy.effective_min
    for _ in range(8):
        await server.send_media(chunk)
        clock.advance(ms_of_bytes(len(chunk), PCM_24K))
    assert len(server.media_frames) == 8


async def test_a_frame_that_does_not_echo_the_stream_id_is_refused() -> None:
    """A cross-call audio leak is the failure this prevents."""
    from rn_providers.telephony.exotel import encode_media_frame, exotel_chunk_policy

    server = FakeTelephonyProvider(media_format=PCM_24K, stream_sid="stream-a")
    frame = encode_media_frame(
        b"\x00" * server.chunk_policy.effective_min,
        stream_sid="stream-b",
        policy=exotel_chunk_policy(PCM_24K),
        sequence_number=0,
        chunk_index=0,
        timestamp_ms=0,
    )
    with pytest.raises(InvariantViolation, match="stream id"):
        await server.send_raw(frame)


async def test_a_non_media_outbound_event_is_refused() -> None:
    server = FakeTelephonyProvider(media_format=PCM_24K)
    with pytest.raises(InvariantViolation, match="does not accept"):
        await server.send_raw('{"event":"hangup","stream_sid":"fake-stream-sid"}')


# ---------------------------------------------------------------------------
# TESTING §3.3 — adversarial delta sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta_bytes", [1, 2, 7919, 300_000], ids=["one", "two", "7919", "300k"])
async def test_the_realtime_fake_emits_the_documented_adversarial_delta_sizes(
    delta_bytes: int,
) -> None:
    """1 byte, 7919 bytes and 300 KB in one delta are named in TESTING §3.3.

    The one-byte case is the interesting one: it is half a sample, so the *aligner* has
    to hold it back rather than the resampler receiving it.
    """
    session = FakeRealtimeProvider(
        [EmitAudio(milliseconds=400, item_id="item-1", delta_bytes=delta_bytes), CloseSession()]
    )
    deltas = [event async for event in session.stream_output() if isinstance(event, AudioDelta)]

    assert deltas, "the fake must emit something at every delta size"
    assert max(len(delta.pcm) for delta in deltas) <= max(delta_bytes, 2)
    total = sum(len(delta.pcm) for delta in deltas)
    assert total == bytes_of_ms(400, PCM_24K)


async def test_a_delta_may_end_mid_sample() -> None:
    """The general case, not a defensive one: a byte stream split at an odd size."""
    session = FakeRealtimeProvider(
        [EmitAudio(milliseconds=100, item_id="item-1", delta_bytes=999), CloseSession()]
    )
    deltas = [event async for event in session.stream_output() if isinstance(event, AudioDelta)]
    assert any(len(delta.pcm) % 2 for delta in deltas)


# ---------------------------------------------------------------------------
# TESTING §3.3 — errors, closes, half-open, latency, session update
# ---------------------------------------------------------------------------


async def test_an_error_event_does_not_close_the_session() -> None:
    """A rate-limit notice mid-response is not a dead call."""
    session = FakeRealtimeProvider(
        [EmitError(code="rate_limit_exceeded"), EmitAudio(milliseconds=40), CloseSession()]
    )
    events = [event async for event in session.stream_output()]

    assert any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, AudioDelta) for event in events)
    assert isinstance(events[-1], SessionClosed)


async def test_a_dropped_socket_carries_its_close_code() -> None:
    session = FakeRealtimeProvider([DropSocket(reason="server error", code=1011)])
    events = [event async for event in session.stream_output()]
    assert isinstance(events[-1], SessionClosed)
    assert events[-1].reason == "server error"


async def test_a_half_open_socket_produces_no_close_at_all() -> None:
    """**The nastiest realtime failure**: still connected, never sends anything again.
    Nothing raises and nothing closes, so a bridge that waits for a close event hangs a
    live call in silence."""
    session = FakeRealtimeProvider([EmitAudio(milliseconds=40), GoSilent()])
    events = [event async for event in session.stream_output()]

    assert any(isinstance(event, AudioDelta) for event in events)
    assert not any(isinstance(event, SessionClosed) for event in events)


async def test_latency_injection_is_awaited_on_every_provider_call() -> None:
    """The real India-to-provider RTT is unmeasured (§6a-17). A local test that assumes
    zero network is lying about the turn budget, so the fake can add one."""
    waits: list[float] = []

    async def record(milliseconds: float) -> None:
        waits.append(milliseconds)

    session = FakeRealtimeProvider(latency_ms=120.0, sleep=record)
    await session.open()
    await session.push_audio(b"\x00" * 960, PCM_24K)
    await session.truncate(item_id="item-1", content_index=0, audio_end_ms=40)
    await session.cancel_generation()
    await session.submit_tool_result(call_id="c", output_json="{}")

    assert waits == [120.0] * 5


async def test_zero_latency_awaits_nothing() -> None:
    """The default. Logic tests must not pay for a network that is not there."""
    waits: list[float] = []

    async def record(milliseconds: float) -> None:
        waits.append(milliseconds)

    session = FakeRealtimeProvider(sleep=record)
    await session.open()
    assert waits == []


async def test_the_session_update_hook_sees_the_declared_tools() -> None:
    """HC-19: the Realtime tool schema is **flat**, and getting it wrong fails silently —
    the session accepts the nested shape and the model then never calls the tool."""
    seen: list[Any] = []
    specs = [{"type": "function", "name": "search_knowledge", "parameters": {}}]

    session = FakeRealtimeProvider(tools=specs, on_session_update=seen.append)
    await session.open()

    assert seen and seen[0][0]["name"] == "search_knowledge"
    assert "function" not in seen[0][0], "a nested spec would be silently ignored by the provider"


async def test_pushing_an_unaccepted_format_is_refused() -> None:
    """The bridge is supposed to have resolved a transcoder. A real provider would reject
    this too, but later and less clearly."""
    session = FakeRealtimeProvider(capabilities=OPENAI_LIKE_CAPABILITIES)
    with pytest.raises(InvariantViolation, match="does not accept"):
        await session.push_audio(b"\x00" * 320, PCM_8K)


async def test_a_tool_call_can_arrive_before_any_audio() -> None:
    """`speech_started` and function calls may arrive at any moment, including before the
    assistant has produced a byte."""
    session = FakeRealtimeProvider(
        [EmitToolCall(call_id="c1", name="search_knowledge"), CloseSession()]
    )
    events = [event async for event in session.stream_output()]
    assert events[0].__class__.__name__ == "ToolCallRequested"
