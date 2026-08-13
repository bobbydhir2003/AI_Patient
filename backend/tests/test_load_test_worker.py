"""Unit tests for the load-test worker's realistic streaming_voice mode
(Issue 6 in the scalability audit).

The worker (load_tests/worker.py) runs in a SEPARATE process against a real
server in production; these tests exercise its pure logic (Metrics accounting,
SSE parsing, and the per-student control flow) directly, with an httpx
MockTransport standing in for the backend - no live server, no real provider
traffic, matching the "never run a real load test" constraint for this task.
"""
import json
import threading
import time
from types import SimpleNamespace

import httpx

from load_tests.worker import Metrics, _parse_sse_block, run_student_streaming_voice


# --------------------------------------------------------------- Metrics
def test_metrics_record_tts_success_and_failure():
    m = Metrics()
    m.record_tts(200, 120.0)
    m.record_tts(200, 80.0)
    m.record_tts(409, 5.0)  # slot exhausted -> degraded/browser fallback
    m.record_tts(500, 10.0)
    m.record_tts(None, 0.0)  # network error

    out = m.overall()
    assert out["ttsRequests"] == 5
    assert out["ttsSuccess"] == 2
    assert out["ttsFailed"] == 3
    assert out["ttsSlotTimeouts"] == 1
    assert out["ttsDegraded"] == 1
    assert out["ttsLatencyMs"]["p50"] is not None  # only successes feed latency


def test_metrics_turn_outcomes():
    m = Metrics()
    m.record_turn_outcome(True)
    m.record_turn_outcome(True)
    m.record_turn_outcome(False)
    out = m.overall()
    assert out["completedTurns"] == 2
    assert out["failedTurns"] == 1


def test_metrics_exposes_http_status_convenience_counts():
    m = Metrics()
    m.record(200, 50.0)
    m.record(409, 5.0)
    m.record(429, 5.0)
    m.record(503, 5.0)
    out = m.overall()
    assert out["http409Count"] == 1
    assert out["http429Count"] == 1
    assert out["http5xxCount"] == 1


# --------------------------------------------------------------- SSE parsing
def test_parse_sse_block_extracts_event_and_data():
    event, data = _parse_sse_block(['event: sentence', 'data: {"index": 0, "text": "Hi."}'])
    assert event == "sentence"
    assert json.loads(data) == {"index": 0, "text": "Hi."}


def test_parse_sse_block_missing_event_is_empty():
    event, data = _parse_sse_block(['data: {}'])
    assert event == ""


# ------------------------------------------ run_student_streaming_voice
_SSE_BODY = (
    'event: sentence\ndata: {"index": 0, "text": "Hello there."}\n\n'
    'event: sentence\ndata: {"index": 1, "text": "How can I help?"}\n\n'
    'event: final\ndata: {"turnId": "t1", "patientText": "Hello there. How can I help?", '
    '"status": "completed"}\n\n'
).encode()


def _make_client(tts_calls, stop: threading.Event, *, sse_status=200, sse_body=_SSE_BODY,
                  tts_status=200):
    """A student loops sessions until `stop`/deadline. The mocked SSE-stream
    call sets `stop` as soon as it is served - since the returned content is a
    fixed byte blob (not a live generator), the CURRENT turn's SSE parsing +
    per-sentence TTS calls still run normally afterward; only the NEXT outer
    while-loop iteration (which re-checks `stop` at its top) is prevented. This
    deterministically caps a test at exactly one fully-executed turn instead of
    racing a wall-clock deadline against an (effectively instant) MockTransport.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"accessToken": "tok"})
        if path == "/api/cases":
            return httpx.Response(200, json=[])
        if path == "/api/sessions" and request.method == "POST":
            return httpx.Response(201, json={"sessionId": "sess1"})
        if path == "/api/interviews/sess1/messages/stream":
            stop.set()
            return httpx.Response(sse_status, content=sse_body,
                                   headers={"content-type": "text/event-stream"})
        if path == "/api/voice/synthesize":
            tts_calls.append(json.loads(request.content))
            return httpx.Response(tts_status, content=b"audio-bytes",
                                   headers={"content-type": "audio/mpeg"})
        if path == "/api/sessions/sess1/complete":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))


def _cfg(**overrides):
    base = dict(
        base_url="http://test", case_id="carly", turns=1, think_time_ms=0,
        enable_tts=True, complete=False, assessment=False, assessment_timeout_s=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_streaming_voice_student_issues_one_sequential_tts_call_per_sentence():
    tts_calls: list[dict] = []
    stop = threading.Event()
    client = _make_client(tts_calls, stop)
    metrics = Metrics()

    run_student_streaming_voice(
        _cfg(), {"email": "a@b.com", "password": "x", "name": "LT"},
        metrics, stop, time.monotonic() + 5, client,
    )

    # Exactly one TTS request per emitted sentence, in order - never several
    # in flight (this is inherently sequential/synchronous code, and the
    # ORDER proves each call happened only after the previous one returned).
    assert [c["text"] for c in tts_calls] == ["Hello there.", "How can I help?"]
    overall = metrics.overall()
    assert overall["ttsRequests"] == 2
    assert overall["ttsSuccess"] == 2
    assert overall["completedTurns"] == 1
    assert overall["failedTurns"] == 0


def test_streaming_voice_without_tts_enabled_never_calls_synthesize():
    tts_calls: list[dict] = []
    stop = threading.Event()
    client = _make_client(tts_calls, stop)
    metrics = Metrics()

    run_student_streaming_voice(
        _cfg(enable_tts=False), {"email": "a@b.com", "password": "x", "name": "LT"},
        metrics, stop, time.monotonic() + 5, client,
    )
    assert tts_calls == []
    assert metrics.overall()["ttsRequests"] == 0
    assert metrics.overall()["completedTurns"] == 1


def test_streaming_voice_multiple_students_overlap_independently():
    """Two virtual students run concurrently (separate stop events, so one
    student finishing does not cut the other off); each still gets exactly
    its own sequential per-sentence TTS calls."""
    tts_calls: list[dict] = []
    lock = threading.Lock()

    def make_handler(stop: threading.Event, session_id: str):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/auth/login":
                return httpx.Response(200, json={"accessToken": "tok"})
            if path == "/api/cases":
                return httpx.Response(200, json=[])
            if path == "/api/sessions" and request.method == "POST":
                return httpx.Response(201, json={"sessionId": session_id})
            if path == f"/api/interviews/{session_id}/messages/stream":
                stop.set()
                return httpx.Response(200, content=_SSE_BODY, headers={"content-type": "text/event-stream"})
            if path == "/api/voice/synthesize":
                with lock:
                    tts_calls.append(json.loads(request.content))
                return httpx.Response(200, content=b"audio-bytes", headers={"content-type": "audio/mpeg"})
            return httpx.Response(404)
        return handler

    metrics = Metrics()
    threads = []
    for i in range(2):
        stop = threading.Event()
        client = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(make_handler(stop, f"sess-{i}"))
        )
        th = threading.Thread(
            target=run_student_streaming_voice,
            args=(_cfg(), {"email": f"s{i}@b.com", "password": "x", "name": f"LT{i}"},
                  metrics, stop, time.monotonic() + 5, client),
        )
        threads.append(th)
        th.start()
    for th in threads:
        th.join(timeout=5)

    # Two students x 2 sentences each = 4 TTS calls total; each student's
    # metrics contribution is independent and both completed their turn.
    assert len(tts_calls) == 4
    overall = metrics.overall()
    assert overall["completedTurns"] == 2
    assert overall["ttsRequests"] == 4


def test_streaming_voice_records_failed_turn_on_non_200_stream():
    tts_calls: list[dict] = []
    stop = threading.Event()
    client = _make_client(tts_calls, stop, sse_status=503, sse_body=b"")
    metrics = Metrics()

    run_student_streaming_voice(
        _cfg(), {"email": "a@b.com", "password": "x", "name": "LT"},
        metrics, stop, time.monotonic() + 5, client,
    )
    assert tts_calls == []
    overall = metrics.overall()
    assert overall["completedTurns"] == 0
    assert overall["failedTurns"] == 1


def test_streaming_voice_records_tts_slot_timeout_as_409():
    tts_calls: list[dict] = []
    stop = threading.Event()
    client = _make_client(tts_calls, stop, tts_status=409)
    metrics = Metrics()

    run_student_streaming_voice(
        _cfg(), {"email": "a@b.com", "password": "x", "name": "LT"},
        metrics, stop, time.monotonic() + 5, client,
    )
    overall = metrics.overall()
    assert overall["ttsSlotTimeouts"] == 2  # both sentences degraded
    assert overall["ttsSuccess"] == 0
