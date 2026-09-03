"""prompt_agent barge-in / audio-decoupling unit tests.

These prove OUR wiring around OpenAI Realtime's native barge-in, without a
network or a real LiveKit AudioSource:

  * audio-delta handling NEVER blocks on the (awaited) publish sink, so the
    Realtime receive loop can process input_audio_buffer.speech_started the
    instant it arrives (the barge-in latency fix),
  * outbound patient audio is bounded (drops oldest under a pathological flood),
  * speech_started flushes not-yet-published patient audio and marks the active
    response so its late deltas are dropped.

They deliberately do NOT assert audio quality / real interruption timing - those
are live-only (real key + device). Following this suite's convention, each async
scenario is driven with asyncio.run (no pytest-asyncio dependency).
"""
import asyncio
import base64

from app.livekit_agent.realtime_prompt_agent import (
    _MAX_QUEUED_OUT_CHUNKS,
    PromptAgentRuntime,
)


def _mk_runtime(on_audio):
    """A runtime with no-op persistence callbacks and a caller-supplied audio
    sink. db_factory is never used because we never drive transcription/done."""
    return PromptAgentRuntime(
        session_id="s1",
        case_id="carly",
        config={"model": "gpt-realtime-2.1-mini", "voice": "sage"},
        db_factory=lambda: (_ for _ in ()).throw(AssertionError("db not expected")),
        on_audio=on_audio,
        on_student_final=lambda *a: None,
        on_patient_final=lambda *a: None,
    )


def _audio_delta(response_id: str, pcm: bytes):
    return {
        "type": "response.output_audio.delta",
        "response_id": response_id,
        "delta": base64.b64encode(pcm).decode("ascii"),
    }


def test_audio_delta_does_not_block_receive_loop():
    """The audio-delta handler must return promptly even when the publish sink
    (capture_frame) is blocked - otherwise speech_started would be stuck behind
    buffered audio (the exact production barge-in stall)."""

    async def scenario():
        release = asyncio.Event()
        published: list[bytes] = []

        async def slow_sink(pcm: bytes) -> None:
            await release.wait()  # simulate a full/back-pressured AudioSource
            published.append(pcm)

        rt = _mk_runtime(slow_sink)
        rt.start()
        rt._on_response_created({"response": {"id": "r1"}})

        # Each handle_event must complete FAST despite the sink being blocked
        # (they only decode + enqueue).
        for i in range(5):
            await asyncio.wait_for(
                rt.handle_event(
                    "response.output_audio.delta", _audio_delta("r1", bytes([i]) * 480)
                ),
                timeout=0.5,
            )

        # Nothing published yet (publisher parked in the blocked sink): the
        # receive path is decoupled from playout back-pressure.
        assert published == []

        # Let the publisher drain; audio still flows, in order.
        release.set()
        await asyncio.sleep(0.05)
        assert len(published) == 5
        await rt.aclose()

    asyncio.run(scenario())


def test_outbound_audio_queue_is_bounded():
    """A pathological flood (publisher blocked forever) must not grow memory
    without bound; the oldest chunk is dropped and the queue stays capped."""

    async def scenario():
        release = asyncio.Event()

        async def blocked_sink(pcm: bytes) -> None:
            await release.wait()

        rt = _mk_runtime(blocked_sink)
        rt.start()
        rt._on_response_created({"response": {"id": "r1"}})

        for _ in range(_MAX_QUEUED_OUT_CHUNKS + 50):
            await rt.handle_event(
                "response.output_audio.delta", _audio_delta("r1", b"\x01\x02")
            )

        # One chunk may be in the publisher's hand (awaiting the sink); the queue
        # itself never exceeds its cap, and overflow was recorded as drops.
        assert rt._audio_out.qsize() <= _MAX_QUEUED_OUT_CHUNKS
        assert rt._out_frames_dropped >= 50
        release.set()
        await rt.aclose()

    asyncio.run(scenario())


def test_speech_started_flushes_queued_patient_audio():
    """Barge-in: queued-but-unplayed patient audio is dropped, and late deltas
    for the interrupted response never publish."""

    async def scenario():
        release = asyncio.Event()
        published: list[bytes] = []

        async def gated_sink(pcm: bytes) -> None:
            await release.wait()
            published.append(pcm)

        rt = _mk_runtime(gated_sink)
        rt.start()
        rt._on_response_created({"response": {"id": "r1"}})

        # Queue a few chunks while the publisher is parked on the gate.
        for _ in range(4):
            await rt.handle_event(
                "response.output_audio.delta", _audio_delta("r1", b"\xaa" * 480)
            )
        assert rt._audio_out.qsize() >= 1

        # Student barges in.
        await rt.handle_event("input_audio_buffer.speech_started", {})
        assert rt._audio_out.qsize() == 0, "speech_started must flush queued patient audio"

        # A late delta for the interrupted response must be dropped, not published.
        await rt.handle_event(
            "response.output_audio.delta", _audio_delta("r1", b"\xbb" * 480)
        )
        assert rt._audio_out.qsize() == 0, "stale delta for interrupted response must drop"

        # Let the publisher run; at most the single in-flight pre-barge chunk
        # plays, never the flushed remainder or the post-barge stale delta.
        release.set()
        await asyncio.sleep(0.05)
        assert len(published) <= 1
        await rt.aclose()

    asyncio.run(scenario())


def test_aclose_stops_publisher_cleanly():
    async def scenario():
        async def sink(pcm: bytes) -> None:
            return None

        rt = _mk_runtime(sink)
        rt.start()
        assert rt._publisher_task is not None
        await asyncio.wait_for(rt.aclose(), timeout=1.0)
        assert rt._publisher_task is None

    asyncio.run(scenario())
