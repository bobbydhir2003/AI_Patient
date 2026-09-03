"""Phase 1: worker-owned Realtime lifetime and true readiness (no network)."""
import asyncio
from types import SimpleNamespace

from app.core.config import get_settings
from app.livekit_agent.realtime_client import REALTIME_PCM_SAMPLE_RATE
from tests.test_livekit_phase_c import _control_messages, _make_ready_session
from tests.test_livekit_poc import _fake_rtc_for_worker
from tests.test_livekit_realtime_phase_a import _pump_until


class _PersistentRealtime:
    input_sample_rate = REALTIME_PCM_SAMPLE_RATE

    def __init__(self):
        self.pushes: list[bytes] = []
        self.start_calls = 0
        self.close_calls = 0
        self.cancel_calls = 0
        self._ready = asyncio.Event()
        self.is_ready = False
        self.close_reason = None

    async def start(self):
        self.start_calls += 1

    async def wait_until_ready(self, _timeout):
        await self._ready.wait()
        return self.is_ready

    def become_ready(self):
        self.is_ready = True
        self._ready.set()

    def push_audio_bytes(self, pcm):
        self.pushes.append(pcm)

    async def cancel_active_response(self):
        self.cancel_calls += 1

    async def aclose(self):
        self.close_calls += 1


class _AudioStream:
    streams = {}

    def __init__(self, track, **_kwargs):
        self.queue = asyncio.Queue()
        self.closed = False
        self.streams[track.name] = self

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def aclose(self):
        self.closed = True

    def frame(self, payload: bytes):
        self.queue.put_nowait(SimpleNamespace(frame=SimpleNamespace(
            data=payload, sample_rate=REALTIME_PCM_SAMPLE_RATE,
            num_channels=1, duration=0.02,
        )))

    def end(self):
        self.queue.put_nowait(None)


def _enable_realtime(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_realtime_engine_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    return settings


def _install_stream_rtc(fake_rtc):
    fake_rtc.AudioStream = _AudioStream
    fake_rtc.TrackKind = SimpleNamespace(KIND_AUDIO="audio")


def test_one_realtime_session_is_constructed_and_started_per_worker(monkeypatch, engine):
    with _fake_rtc_for_worker():
        worker, _room, _sid = _make_ready_session(engine, monkeypatch)
        _enable_realtime(monkeypatch)
        made = []

        class FakeSession(_PersistentRealtime):
            def __init__(self, **_kwargs):
                super().__init__()
                made.append(self)

        monkeypatch.setattr("app.livekit_agent.realtime_session.RealtimeSession", FakeSession)
        monkeypatch.setattr("app.livekit_agent.realtime_client.OpenAIRealtimeClient", lambda **_kwargs: object())

        async def scenario():
            first = await worker._maybe_start_realtime_session("student", "track-a")
            second = await worker._maybe_start_realtime_session("student", "track-b")
            assert first is second
            assert first.start_calls == 1

        asyncio.run(scenario())

    assert len(made) == 1


def test_stream_end_and_replacement_reuse_session_and_reject_old_frames(monkeypatch, engine):
    with _fake_rtc_for_worker() as fake_rtc:
        _install_stream_rtc(fake_rtc)
        worker, _room, _sid = _make_ready_session(engine, monkeypatch)
        realtime = _PersistentRealtime()
        realtime.become_ready()
        worker._realtime_engine_active = True
        worker._realtime_session = realtime
        worker._realtime_session_started.set()

        participant = SimpleNamespace(identity="student")
        track_a = SimpleNamespace(name="a", kind="audio")
        track_b = SimpleNamespace(name="b", kind="audio")
        publication_a = SimpleNamespace(sid="track-a")
        publication_b = SimpleNamespace(sid="track-b")

        async def scenario():
            worker._start_student_audio_ingest(track_a, publication_a, participant)
            assert await _pump_until(lambda: "a" in _AudioStream.streams)
            _AudioStream.streams["a"].frame(b"a-first")
            assert await _pump_until(lambda: realtime.pushes == [b"a-first"])

            worker._start_student_audio_ingest(track_b, publication_b, participant)
            assert await _pump_until(lambda: "b" in _AudioStream.streams)
            _AudioStream.streams["a"].frame(b"a-late")
            _AudioStream.streams["b"].frame(b"b-first")
            assert await _pump_until(lambda: b"b-first" in realtime.pushes)
            assert b"a-late" not in realtime.pushes
            assert worker._realtime_session is realtime
            assert realtime.close_calls == 0

            _AudioStream.streams["b"].end()
            assert await _pump_until(lambda: "track-b" not in worker._student_audio_tasks)
            assert worker._realtime_session is realtime
            assert realtime.close_calls == 0

        asyncio.run(scenario())


def test_worker_ready_requires_configured_provider_and_attached_mic(monkeypatch, engine):
    with _fake_rtc_for_worker():
        worker, room, _sid = _make_ready_session(engine, monkeypatch)
        _enable_realtime(monkeypatch)
        realtime = _PersistentRealtime()

        async def fake_start(_identity, _track_sid):
            worker._realtime_session = realtime
            await realtime.start()
            return realtime

        monkeypatch.setattr(worker, "_maybe_start_realtime_session", fake_start)

        async def scenario():
            await worker.start()
            assert _control_messages(room, "agent_ready") == []
            realtime.become_ready()
            assert await _pump_until(lambda: worker._realtime_configured_ready)
            assert _control_messages(room, "agent_ready") == []
            worker._realtime_producer_attached = True
            worker._maybe_send_realtime_agent_ready()
            assert await _pump_until(lambda: len(_control_messages(room, "agent_ready")) == 1)
            await worker.aclose(reason="test")

        asyncio.run(scenario())


def test_worker_shutdown_closes_persistent_session_exactly_once(monkeypatch, engine):
    with _fake_rtc_for_worker():
        worker, _room, _sid = _make_ready_session(engine, monkeypatch)
        realtime = _PersistentRealtime()
        worker._realtime_engine_active = True
        worker._realtime_session = realtime
        worker._realtime_session_started.set()

        async def scenario():
            await worker.aclose(reason="first")
            await worker.aclose(reason="duplicate")

        asyncio.run(scenario())

    assert realtime.close_calls == 1
    assert realtime.cancel_calls == 1


def test_provider_failure_cannot_emit_or_retain_ready(monkeypatch, engine):
    with _fake_rtc_for_worker():
        worker, room, _sid = _make_ready_session(engine, monkeypatch)
        async def scenario():
            worker._realtime_engine_active = True
            realtime = _PersistentRealtime()
            worker._realtime_session = realtime
            worker._realtime_configured_ready = True
            worker._realtime_producer_attached = True
            realtime.become_ready()
            worker._maybe_send_realtime_agent_ready()
            assert await _pump_until(lambda: len(_control_messages(room, "agent_ready")) == 1)

            worker._on_realtime_unavailable("provider_connection_closed")
            assert worker._realtime_configured_ready is False
            assert worker._shutdown_called is True
            if worker._shutdown_task is not None:
                await worker._shutdown_task

        asyncio.run(scenario())
