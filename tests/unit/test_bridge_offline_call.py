"""The complete offline call simulation. **Zero network, zero cost, zero credentials.**

This is the Phase-4 criterion *"the full bridge loop runs in CI against fakes with zero
paid API calls"*, and it is what PRD §7's *"the full call flow must be exercisable
without placing a paid phone call"* means in practice.

The chain, end to end:

    agent tool  →  VoiceSession seam  →  audio bridge  →  fake telephony
    (real registry, real dispatcher)      (real)            (auditing fake)

Only two things are fakes and both are seams that exist for exactly this: the realtime
provider (Phase 4 has no adapter) and the telephony socket. Everything between them is
the code that will run on a real call — the same ring buffer, the same pacer, the same
ledger, the same barge-in function.

**The tool path is genuinely real.** The `search_knowledge` tool, the `ToolRegistry`,
`dispatch_tool_call` and the tenant context are the production ones, wired in through
the bridge's `ToolCallSink` — which is a callable precisely so `rn_voice` never imports
`rn_agent`. That layering is what this test is able to demonstrate rather than assert.

> **Nothing here is evidence about Exotel.** The frames are hand-authored from
> documented shapes and audited against `ExotelDialect`, which is **[A]**. This proves
> the bridge obeys the rules we believe apply. Phase 4G proves the rules.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from rn_agent.tools import REGISTRY, dispatch_tool_call
from rn_agent.tools.base import ToolRuntime, ToolServices
from rn_core.ids import new_id
from rn_domain.identifiers import AgentVersionId, KnowledgeBaseId, OrganizationId
from rn_domain.tenancy import TenantContext
from rn_providers.audio.formats import PCM_8K, PCM_24K, AudioFormat, ms_of_bytes
from rn_providers.fakes.realtime import (
    SARVAM_LIKE_CAPABILITIES,
    CloseSession,
    DropSocket,
    EmitAudio,
    EmitSpeechStarted,
    EmitToolCall,
    EmitTranscript,
    EndResponse,
    FakeRealtimeProvider,
)
from rn_providers.fakes.telephony import (
    FakeTelephonyProvider,
    InboundAudio,
    InboundDtmf,
    MalformedFrame,
    Stop,
    TelephonyFaults,
    WaitForOutbound,
)
from rn_providers.realtime.session import ToolCallRequested
from rn_services.contracts import RetrievalResult, RetrievedChunk
from rn_voice.session.bridge import AudioBridge, ToolCallSink

#: `provider`, not `unit`: ROADMAP's Phase-4 deliverable says the fakes are driven by
#: `provider`-marked tests, and the marker is what tells a reader this exercises a
#: provider adapter surface rather than pure logic. It still touches no network.
pytestmark = pytest.mark.provider

ORGANIZATION_ID = OrganizationId(new_id())
AGENT_VERSION_ID = AgentVersionId(new_id())
BASE_ID = KnowledgeBaseId(new_id())


async def _instant_sleep(_milliseconds: float) -> None:
    """The pacer's sleep, removed. A 20-second response then runs in no wall-clock time.

    Not `asyncio.sleep(0)` by accident — the point is that the pacer's *schedule* is
    still computed and still asserted on; only the waiting is elided.
    """
    return None


class _StubRetriever:
    """A `KnowledgeRetriever` with one passage. The retrieval path is Phase 3's; this
    test is about the audio path reaching it, not about retrieval quality."""

    async def search(
        self,
        *,
        query: str,
        knowledge_base_ids: Sequence[KnowledgeBaseId] | None = None,
        k: int,
    ) -> RetrievalResult:
        chunk = RetrievedChunk(
            chunk_id="chunk-0",
            knowledge_base_id=BASE_ID,
            knowledge_base_name="Services",
            content="We build websites and mobile applications.",
            score=0.9,
            embedding_model="stub-model",
        )
        return RetrievalResult(chunks=(chunk,), requested_k=k, embedding_model="stub-model")


class _Scope:
    """The agent version's tool scope, as the dispatcher needs it."""

    @property
    def organization_id(self) -> OrganizationId:
        return ORGANIZATION_ID

    @property
    def agent_version_id(self) -> AgentVersionId:
        return AGENT_VERSION_ID

    @property
    def enabled_tools(self) -> frozenset[str]:
        return frozenset({"search_knowledge"})


def _real_tool_sink() -> tuple[ToolCallSink, list[str]]:
    """A tool sink backed by the **real** registry and dispatcher.

    A closure rather than a class so the layering is visible at a glance: the bridge is
    handed a callable, and everything on the far side of it is `rn_agent`, which
    `rn_voice` does not import.
    """
    seen: list[str] = []
    runtime = ToolRuntime(
        context=TenantContext(
            organization_id=ORGANIZATION_ID, permissions=frozenset({"org:knowledge:read"})
        ),
        agent_version_id=AGENT_VERSION_ID,
        services=ToolServices(retrieval=_StubRetriever()),
    )

    async def sink(call: ToolCallRequested) -> str:
        seen.append(call.name)
        envelope = await dispatch_tool_call(
            registry=REGISTRY,
            scope=_Scope(),
            runtime=runtime,
            name=call.name,
            arguments_json=call.arguments_json,
        )
        payload: dict[str, object] = {"outcome": envelope.outcome.value}
        if envelope.data is not None:
            payload["data"] = dict(envelope.data)
        return json.dumps(payload, sort_keys=True)

    return sink, seen


def _bridge(
    server: FakeTelephonyProvider,
    session: FakeRealtimeProvider,
    *,
    tool_sink: ToolCallSink | None = None,
) -> AudioBridge:
    return AudioBridge(
        # The fakes satisfy `TelephonyTransport` and `VoiceSession` structurally; the
        # protocols are what the bridge is typed against, so no cast is needed.
        transport=server,
        session=session,
        tool_sink=tool_sink,
        sleep=_instant_sleep,
    )


# ---------------------------------------------------------------------------
# The complete call
# ---------------------------------------------------------------------------


async def test_a_complete_call_runs_end_to_end_against_fakes() -> None:
    """Caller speaks, agent answers, agent uses a tool, call ends. No network anywhere."""
    server = FakeTelephonyProvider(
        [
            InboundAudio(milliseconds=200),
            WaitForOutbound(chunks=2),
            InboundDtmf(digit="1"),
            InboundAudio(milliseconds=100),
            Stop(),
        ],
        media_format=PCM_24K,
    )
    session = FakeRealtimeProvider(
        [
            EmitTranscript(text="Hello, I'm an AI assistant.", is_final=True),
            EmitAudio(milliseconds=400, item_id="item-1", delta_bytes=1234),
            EmitToolCall(
                call_id="call-1", name="search_knowledge", arguments_json='{"query": "services"}'
            ),
            EmitAudio(milliseconds=200, item_id="item-1", delta_bytes=777),
            EndResponse(item_id="item-1"),
            CloseSession(),
        ]
    )
    sink, seen = _real_tool_sink()
    bridge = _bridge(server, session, tool_sink=sink)

    result = await bridge.run()

    # -- the caller was heard -------------------------------------------------
    assert result.inbound_frames == 15, "200 ms + 100 ms at 20 ms per frame"
    assert session.pushed_bytes == result.inbound_bytes, "24 kHz both sides: no resampling"
    assert result.dtmf_digits == ["1"]

    # -- the agent was heard --------------------------------------------------
    assert result.outbound_chunks > 0
    assert server.media_frames, "the caller must actually have received audio"
    for frame in server.media_frames:
        assert server.chunk_policy.is_legal(len(frame.payload))
    assert len(server.mark_frames) == len(server.media_frames), "one mark per chunk"

    # -- the tool ran, through the real dispatcher ----------------------------
    assert seen == ["search_knowledge"]
    assert result.tool_calls == 1
    assert len(session.tool_results) == 1
    returned = json.loads(session.tool_results[0][1])
    assert returned["outcome"] == "ok"
    assert returned["data"]["results"][0]["content"].startswith("We build websites")

    # -- and the accounting is sane ------------------------------------------
    assert result.outbound_bytes == server.played_bytes
    assert bridge.ledger.enqueued_ms <= server.played_ms() + 1


async def test_every_outbound_chunk_is_legal_at_every_rate() -> None:
    """The alignment invariant, through the whole bridge rather than the buffer alone.

    At 8 kHz the model's 24 kHz audio is downsampled on the way out, so this also
    exercises the expensive resampling direction inside a real call.
    """
    for fmt in (PCM_8K, AudioFormat(rate_hz=16000), PCM_24K):
        server = FakeTelephonyProvider(
            [InboundAudio(milliseconds=100), WaitForOutbound(chunks=1), Stop()],
            media_format=fmt,
        )
        session = FakeRealtimeProvider(
            [
                EmitAudio(milliseconds=900, item_id="item-1", delta_bytes=999),
                EndResponse(item_id="item-1"),
                CloseSession(),
            ]
        )
        result = await _bridge(server, session).run()

        assert server.media_frames, f"no audio was emitted at {fmt.rate_hz} Hz"
        for frame in server.media_frames:
            assert server.chunk_policy.is_legal(len(frame.payload)), (
                f"illegal chunk at {fmt.rate_hz} Hz: {len(frame.payload)} bytes"
            )
        assert result.outbound_bytes == server.played_bytes


async def test_the_bridge_resamples_only_when_the_formats_differ() -> None:
    """An OpenAI-primary agent at 24 kHz resamples nothing; at 8 kHz both directions
    convert. Same code path, no branch at the call site."""
    matched = FakeTelephonyProvider([InboundAudio(milliseconds=100), Stop()], media_format=PCM_24K)
    session = FakeRealtimeProvider([CloseSession()])
    await _bridge(matched, session).run()
    assert session.pushed_bytes == 100 * PCM_24K.bytes_per_second // 1000

    converting = FakeTelephonyProvider(
        [InboundAudio(milliseconds=100), Stop()], media_format=PCM_8K
    )
    session_2 = FakeRealtimeProvider([CloseSession()])
    await _bridge(converting, session_2).run()
    # 8 kHz in, 24 kHz to the model: three times the bytes, less the resampler's tail.
    inbound_ms = ms_of_bytes(session_2.pushed_bytes, PCM_24K)
    assert 80 <= inbound_ms <= 100


# ---------------------------------------------------------------------------
# Barge-in, inside a live call
# ---------------------------------------------------------------------------


async def test_barge_in_during_a_call_clears_flushes_and_truncates_once() -> None:
    """**The barge-in criterion, end to end.** The caller interrupts after the agent has
    been speaking, and exactly one of each effect fires — with a truthful `audio_end_ms`."""
    server = FakeTelephonyProvider(
        [
            InboundAudio(milliseconds=100),
            WaitForOutbound(chunks=2),
            InboundAudio(milliseconds=40),
            Stop(),
        ],
        media_format=PCM_24K,
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=2000, item_id="item-1", delta_bytes=4096),
            EmitSpeechStarted(),
            EmitAudio(milliseconds=500, item_id="item-2"),
            EndResponse(item_id="item-2"),
            CloseSession(),
        ]
    )
    bridge = _bridge(server, session)

    result = await bridge.run()

    assert result.barge_in_count == 1
    assert server.clear_count == 1
    assert len(session.truncations) == 1

    outcome = result.barge_ins[0]
    truncation = session.truncations[0]
    assert truncation.audio_end_ms == outcome.audio_end_ms
    assert truncation.item_id == "item-1"

    # The invariant that outranks the rest: we never claim more was heard than was sent.
    written_ms = ms_of_bytes(sum(len(frame.payload) for frame in server.media_frames), PCM_24K)
    assert outcome.audio_end_ms <= written_ms + 1

    # And the clear happened before the audio for the next item.
    order = server.event_order
    assert "clear" in order
    assert order.index("clear") < len(order) - 1


async def test_a_barge_in_discards_our_own_unsent_audio_too() -> None:
    """Clearing the provider's buffer while ours keeps feeding it is a no-op with extra
    steps. The flush is the second of the three effects for that reason."""
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=60), WaitForOutbound(chunks=1), Stop()],
        media_format=PCM_24K,
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=3000, item_id="item-1", delta_bytes=60000),
            EmitSpeechStarted(),
            CloseSession(),
        ]
    )
    result = await _bridge(server, session).run()

    assert result.barge_in_count == 1
    assert result.barge_ins[0].flushed_bytes > 0, "un-sent audio must have been dropped"


async def test_a_cascade_session_is_cancelled_rather_than_truncated() -> None:
    """`supports_remote_truncation=False` — there is no remote conversation state, so the
    same effect is achieved by a different mechanism, and the bridge branches on the
    capability rather than assuming OpenAI."""
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=100), WaitForOutbound(chunks=1), Stop()],
        media_format=PCM_8K,
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=1000, item_id="item-1"),
            EmitSpeechStarted(),
            CloseSession(),
        ],
        capabilities=SARVAM_LIKE_CAPABILITIES,
    )
    result = await _bridge(server, session).run()

    assert result.barge_in_count == 1
    assert session.truncations == []
    assert session.cancellations == 1


# ---------------------------------------------------------------------------
# Misbehaving provider
# ---------------------------------------------------------------------------


async def test_a_malformed_frame_does_not_end_the_call() -> None:
    """One bad frame out of twenty per second must not drop a call."""
    server = FakeTelephonyProvider(
        [
            InboundAudio(milliseconds=40),
            MalformedFrame(raw="{not json at all"),
            InboundAudio(milliseconds=40),
            Stop(),
        ],
        media_format=PCM_24K,
    )
    result = await _bridge(server, FakeRealtimeProvider([CloseSession()])).run()

    assert result.decode_errors == 1
    assert result.inbound_frames == 4, "both audio bursts still arrived"
    assert result.stop_reason == "telephony_stopped"


async def test_marks_that_never_arrive_keep_the_estimate_low() -> None:
    """**The pathological case the `min()` clamp exists for.** With no echoes at all,
    `confirmed_ms` stays at zero and the estimate must never jump to `enqueued_ms`."""
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=60), WaitForOutbound(chunks=3), Stop()],
        media_format=PCM_24K,
        faults=TelephonyFaults(mark_loss_rate=1.0),
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=1000, item_id="item-1"),
            EndResponse(item_id="item-1"),
            CloseSession(),
        ]
    )
    bridge = _bridge(server, session)

    result = await bridge.run()

    assert result.marks_echoed == 0
    assert bridge.ledger.confirmed_ms == 0.0
    assert bridge.ledger.estimate_played_ms() <= bridge.ledger.enqueued_ms


async def test_a_delayed_mark_echo_still_reconciles() -> None:
    """A sink that is genuinely behind. The ledger tracks what was confirmed, not what
    was hoped."""
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=60), WaitForOutbound(chunks=4), Stop()],
        media_format=PCM_24K,
        faults=TelephonyFaults(mark_echo_delay=2),
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=1200, item_id="item-1"),
            EndResponse(item_id="item-1"),
            CloseSession(),
        ]
    )
    bridge = _bridge(server, session)

    result = await bridge.run()

    assert result.marks_echoed > 0
    assert bridge.ledger.confirmed_ms <= bridge.ledger.enqueued_ms


async def test_a_dropped_socket_ends_the_call_without_an_exception() -> None:
    """The provider vanishing mid-response is a call that ends, not a crash."""
    server = FakeTelephonyProvider([InboundAudio(milliseconds=200), Stop()], media_format=PCM_24K)
    session = FakeRealtimeProvider([EmitAudio(milliseconds=300, item_id="item-1"), DropSocket()])
    result = await _bridge(server, session).run()
    assert result.stop_reason in {"session_closed", "telephony_stopped"}


async def test_writing_after_the_stream_stops_is_refused() -> None:
    """The fake enforces it so the bridge cannot learn a habit a real socket punishes."""
    server = FakeTelephonyProvider([Stop()], media_format=PCM_24K)
    async for _ in server.events():
        pass
    with pytest.raises(Exception, match="stopped"):
        await server.send_media(b"\x00" * server.chunk_policy.effective_min)


# ---------------------------------------------------------------------------
# No network, no cost
# ---------------------------------------------------------------------------


def test_the_media_path_imports_no_transport_library() -> None:
    """The whole basis of "zero paid API calls": there is nothing here that *could* dial.

    Asserted on a fresh interpreter's module table rather than by inspection, because an
    import added three modules deep is exactly the kind that inspection misses.
    """
    import subprocess
    import sys

    code = (
        "import sys;"
        "import rn_voice.session.bridge;"
        "import rn_providers.fakes.telephony;"
        "import rn_providers.fakes.realtime;"
        "banned={'httpx','websockets','openai','aiohttp','requests'};"
        "found=sorted(banned & set(sys.modules));"
        "print(found)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]", f"transport libraries loaded: {result.stdout}"


# ---------------------------------------------------------------------------
# The D-5 recording tap
# ---------------------------------------------------------------------------


async def test_the_recording_tap_is_disabled_by_default() -> None:
    """Open decision **D-5** is Phase 8. The tap point exists now only because
    retrofitting one into a latency-critical loop is expensive and leaving an unused,
    disabled one is not (ROADMAP, Phase 4)."""
    from rn_voice.media.tap import NullMediaTap

    server = FakeTelephonyProvider([InboundAudio(milliseconds=40), Stop()], media_format=PCM_24K)
    bridge = _bridge(server, FakeRealtimeProvider([CloseSession()]))
    await bridge.run()

    assert isinstance(bridge._tap, NullMediaTap)
    assert NullMediaTap.enabled is False


async def test_a_wired_tap_observes_both_directions() -> None:
    """Inbound as received; outbound **after** transcoding and alignment, so what is
    observed is what was actually played rather than what the model generated."""
    from rn_voice.media.tap import MediaDirection

    seen: list[tuple[str, int]] = []

    class _Recorder:
        def observe(self, direction: MediaDirection, pcm: bytes) -> None:
            seen.append((direction.value, len(pcm)))

    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=60), WaitForOutbound(chunks=1), Stop()],
        media_format=PCM_24K,
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=400, item_id="item-1"),
            EndResponse(item_id="item-1"),
            CloseSession(),
        ]
    )
    await AudioBridge(
        transport=server, session=session, sleep=_instant_sleep, tap=_Recorder()
    ).run()

    directions = {direction for direction, _ in seen}
    assert directions == {"inbound", "outbound"}
    outbound = [size for direction, size in seen if direction == "outbound"]
    assert all(server.chunk_policy.is_legal(size) for size in outbound)


async def test_a_misbehaving_tap_cannot_end_a_call() -> None:
    """A recording feature that can drop a call is worse than no recording feature."""
    from rn_voice.media.tap import MediaDirection, NullMediaTap

    class _Broken:
        def observe(self, direction: MediaDirection, pcm: bytes) -> None:
            raise RuntimeError("storage is down")

    server = FakeTelephonyProvider([InboundAudio(milliseconds=60), Stop()], media_format=PCM_24K)
    bridge = AudioBridge(
        transport=server,
        session=FakeRealtimeProvider([CloseSession()]),
        sleep=_instant_sleep,
        tap=_Broken(),
    )

    result = await bridge.run()

    assert result.inbound_frames == 3, "the call continued"
    assert isinstance(bridge._tap, NullMediaTap)


# ---------------------------------------------------------------------------
# TESTING §3.3 — adversarial delta sizes, through the whole bridge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta_bytes",
    [1, 2, 7919, 300_000],
    ids=["one-byte", "one-sample", "7919", "300kb"],
)
async def test_the_ring_buffer_aligns_output_for_every_adversarial_delta_size(
    delta_bytes: int,
) -> None:
    """*"1 byte, 7919 bytes, 300 KB in one delta, and a delta split mid-sample. The ring
    buffer must produce aligned output regardless (HC-2)."* — TESTING §3.3.

    Asserted through the **bridge**, not the buffer alone. The buffer's own sweep proves
    the aligner; this proves the whole chain — aligner, transcoder, ring buffer, pacer —
    survives sizes nobody chose. The one-byte case is the sharp one: it is half a sample,
    so `SampleAligner` has to hold it back before the resampler ever sees it.
    """
    # Long enough that the delta size is genuinely exercised: a 300 KB delta needs 300 KB
    # of audio to exist, or the test would silently degrade into "one delta, whole
    # response" and quietly stop testing the documented case.
    milliseconds = max(600, -(-delta_bytes // PCM_24K.bytes_per_second) * 1000)
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=40), WaitForOutbound(chunks=1), Stop()],
        media_format=PCM_24K,
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=milliseconds, item_id="item-1", delta_bytes=delta_bytes),
            EndResponse(item_id="item-1"),
            CloseSession(),
        ]
    )

    result = await _bridge(server, session).run()

    assert server.media_frames, f"no audio emitted for delta_bytes={delta_bytes}"
    for frame in server.media_frames:
        assert server.chunk_policy.is_legal(len(frame.payload)), (
            f"illegal chunk {len(frame.payload)} B from {delta_bytes} B deltas"
        )
        assert len(frame.payload) % 2 == 0, "a split sample reached the wire"
    assert result.outbound_bytes == server.played_bytes


async def test_a_delta_split_mid_sample_does_not_corrupt_the_stream() -> None:
    """Odd-sized deltas in sequence. Dropping the orphan byte would byte-swap every
    sample after it — full-scale noise, and easy to misattribute to the network."""
    server = FakeTelephonyProvider(
        [InboundAudio(milliseconds=40), WaitForOutbound(chunks=1), Stop()],
        media_format=PCM_24K,
    )
    session = FakeRealtimeProvider(
        [
            EmitAudio(milliseconds=400, item_id="item-1", delta_bytes=999),
            EmitAudio(milliseconds=400, item_id="item-1", delta_bytes=333),
            EndResponse(item_id="item-1"),
            CloseSession(),
        ]
    )

    await _bridge(server, session).run()

    assert server.media_frames
    for frame in server.media_frames:
        assert server.chunk_policy.is_legal(len(frame.payload))


# ---------------------------------------------------------------------------
# TESTING §3.3 — hostile function-call arguments, over the realtime path
# ---------------------------------------------------------------------------


async def _dispatch_over_the_bridge(*calls: EmitToolCall) -> list[dict[str, object]]:
    """Run tool calls through fake realtime → bridge → real dispatcher; return envelopes.

    The point is the *path*. The dispatcher's own tests cover these cases directly; this
    covers them arriving the way they actually will — as provider events crossing the
    `ToolCallSink` seam that exists so `rn_voice` never imports `rn_agent`.
    """
    server = FakeTelephonyProvider([InboundAudio(milliseconds=20), Stop()], media_format=PCM_24K)
    session = FakeRealtimeProvider([*calls, CloseSession()])
    sink, _ = _real_tool_sink()

    await _bridge(server, session, tool_sink=sink).run()

    return [json.loads(output) for _, output in session.tool_results]


async def test_valid_arguments_are_dispatched_and_answered() -> None:
    envelopes = await _dispatch_over_the_bridge(
        EmitToolCall(call_id="c1", name="search_knowledge", arguments_json='{"query": "services"}')
    )
    assert [e["outcome"] for e in envelopes] == ["ok"]


async def test_invalid_arguments_come_back_as_invalid_arguments() -> None:
    """The model corrects these well, which is why the outcome is its own vocabulary
    entry rather than a generic failure."""
    envelopes = await _dispatch_over_the_bridge(
        EmitToolCall(
            call_id="c1", name="search_knowledge", arguments_json='{"query": "x", "limit": 99}'
        )
    )
    assert [e["outcome"] for e in envelopes] == ["invalid_arguments"]


async def test_a_tool_the_agent_version_does_not_enable_is_denied() -> None:
    """`list_knowledge_bases` is in the process-wide registry but not in this version's
    enabled set. The enabled list is session configuration — no injected text adds a tool
    (AGENT_ARCHITECTURE §5.2), and the dispatcher refuses even if the model asks."""
    envelopes = await _dispatch_over_the_bridge(
        EmitToolCall(call_id="c1", name="list_knowledge_bases", arguments_json="{}")
    )
    assert [e["outcome"] for e in envelopes] == ["denied"]


async def test_an_unknown_tool_name_is_denied_identically() -> None:
    """One message for "no such tool", "not enabled" and "no permission", so a caller
    cannot probe the tool set by comparing refusals."""
    envelopes = await _dispatch_over_the_bridge(
        EmitToolCall(call_id="c1", name="wire_me_money", arguments_json="{}")
    )
    assert [e["outcome"] for e in envelopes] == ["denied"]


async def test_a_forged_organization_id_is_stripped_and_the_call_still_runs() -> None:
    """*"args containing an `organization_id` (which must be ignored and logged as a
    security event)"* — TESTING §3.3, ARCHITECTURE §5.

    The dispatcher strips server-owned names **before** validation, so the attempt is
    recorded rather than arriving as an ordinary validation error with the signal lost.
    Tenant identity comes from the `ToolRuntime`, which the model cannot reach.
    """
    envelopes = await _dispatch_over_the_bridge(
        EmitToolCall(
            call_id="c1",
            name="search_knowledge",
            arguments_json=(
                '{"query": "services", "organization_id": "00000000-0000-0000-0000-000000000000"}'
            ),
        )
    )
    assert [e["outcome"] for e in envelopes] == ["ok"]
    # The forged value could not have been honoured: it is not a field on the args model
    # at all, so there is no path by which it becomes a tenant.
    from rn_agent.tools.builtin.search import SearchKnowledgeArgs

    assert "organization_id" not in SearchKnowledgeArgs.model_fields


async def test_a_sequence_of_hostile_calls_all_resolve_without_ending_the_call() -> None:
    """Four hostile calls in one response. None may kill the session: a caller who can
    end a call by making the model misbehave has a denial-of-service."""
    envelopes = await _dispatch_over_the_bridge(
        EmitToolCall(call_id="c1", name="search_knowledge", arguments_json='{"limit": 99}'),
        EmitToolCall(call_id="c2", name="list_knowledge_bases", arguments_json="{}"),
        EmitToolCall(call_id="c3", name="search_knowledge", arguments_json="not json at all"),
        EmitToolCall(call_id="c4", name="search_knowledge", arguments_json='{"query": "ok"}'),
    )
    assert len(envelopes) == 4
    assert envelopes[-1]["outcome"] == "ok", "a healthy call must still work afterwards"
    assert all("outcome" in envelope for envelope in envelopes)
