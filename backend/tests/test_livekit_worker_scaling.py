"""Focused tests for horizontal LiveKit-worker scaling configuration.

These prove the worker's capacity/identity config is EXPLICIT (wired into
WorkerOptions from app.core.config, not hidden framework defaults) and that the
SAME agent_name + config is what makes N identical worker machines coexist -
without asserting any real multi-machine CPU behavior (which a single test host
cannot represent).
"""
import pytest

from app.core.config import get_settings
from app.livekit_agent import worker

LIVEKIT_URL = "wss://fake-project.livekit.cloud"
LIVEKIT_API_KEY = "test-lk-key"
LIVEKIT_API_SECRET = "test-lk-secret-at-least-32-chars-long"


def _enable_livekit(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "livekit_poc_enabled", True)
    monkeypatch.setattr(s, "livekit_url", LIVEKIT_URL)
    monkeypatch.setattr(s, "livekit_api_key", LIVEKIT_API_KEY)
    monkeypatch.setattr(s, "livekit_api_secret", LIVEKIT_API_SECRET)
    return s


# --------------------------------------------------------------------------
# WorkerOptions is built from configured settings (not framework defaults)
# --------------------------------------------------------------------------
def test_worker_options_use_configured_agent_name(monkeypatch):
    s = _enable_livekit(monkeypatch)
    monkeypatch.setattr(s, "livekit_agent_name", "ptai-custom-agent")
    opts = worker._build_worker_options()
    assert opts.agent_name == "ptai-custom-agent"


def test_worker_options_use_configured_load_threshold(monkeypatch):
    s = _enable_livekit(monkeypatch)
    monkeypatch.setattr(s, "livekit_worker_load_threshold", 0.55)
    opts = worker._build_worker_options()
    # Passed as a PLAIN float (not a ServerEnvOption) so it applies in both the
    # `dev` and `start` CLI modes.
    assert opts.load_threshold == 0.55
    assert isinstance(opts.load_threshold, float)


def test_worker_options_use_configured_num_idle_processes(monkeypatch):
    s = _enable_livekit(monkeypatch)
    monkeypatch.setattr(s, "livekit_worker_num_idle_processes", 3)
    opts = worker._build_worker_options()
    assert opts.num_idle_processes == 3


def test_worker_options_use_configured_memory_guards(monkeypatch):
    s = _enable_livekit(monkeypatch)
    monkeypatch.setattr(s, "livekit_worker_job_memory_warn_mb", 750)
    monkeypatch.setattr(s, "livekit_worker_job_memory_limit_mb", 1500)
    opts = worker._build_worker_options()
    assert opts.job_memory_warn_mb == 750
    assert opts.job_memory_limit_mb == 1500


def test_worker_options_defaults_are_correct(monkeypatch):
    """The intended production defaults (agent_name / 0.70 / 6 / 500 / 0),
    verified from the real Settings defaults, and per-job PROCESS isolation."""
    from livekit.agents import JobExecutorType

    _enable_livekit(monkeypatch)  # only sets credentials; leaves capacity at defaults
    s = get_settings()
    assert s.livekit_agent_name == "ptai-patient-agent"
    assert s.livekit_worker_load_threshold == 0.70
    assert s.livekit_worker_num_idle_processes == 6
    assert s.livekit_worker_job_memory_warn_mb == 500
    assert s.livekit_worker_job_memory_limit_mb == 0

    opts = worker._build_worker_options()
    assert opts.agent_name == "ptai-patient-agent"
    assert opts.load_threshold == 0.70
    assert opts.num_idle_processes == 6
    assert opts.job_memory_warn_mb == 500
    assert opts.job_memory_limit_mb == 0
    assert opts.job_executor_type == JobExecutorType.PROCESS


# --------------------------------------------------------------------------
# Worker identity (observability) — host/pid/instance, never secrets
# --------------------------------------------------------------------------
def test_worker_instance_id_defaults_to_hostname(monkeypatch):
    monkeypatch.setattr(get_settings(), "livekit_worker_instance_id", "")
    assert worker._worker_instance_id() == worker._WORKER_HOST


def test_worker_instance_id_uses_configured_label(monkeypatch):
    monkeypatch.setattr(get_settings(), "livekit_worker_instance_id", "worker-a")
    assert worker._worker_instance_id() == "worker-a"


def test_worker_ident_contains_host_pid_instance_and_no_secrets(monkeypatch):
    s = _enable_livekit(monkeypatch)
    monkeypatch.setattr(s, "livekit_worker_instance_id", "worker-b")
    ident = worker._worker_ident()
    assert "worker_host=" in ident
    assert "worker_pid=" in ident
    assert "worker_instance=worker-b" in ident
    # Must never leak credentials or prompt IDs.
    for secret in (LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL):
        assert secret not in ident
    monkeypatch.setattr(s, "openai_realtime_carly_prompt_id", "pmpt_secret_123")
    assert "pmpt_secret_123" not in worker._worker_ident()


# --------------------------------------------------------------------------
# Multi-worker: SAME agent_name is configuration-compatible across instances
# --------------------------------------------------------------------------
def test_multiple_instances_share_agent_name_but_differ_only_in_log_label(monkeypatch):
    """Two worker 'machines' (different instance labels) register under the SAME
    agent_name with IDENTICAL dispatch-relevant options - which is exactly what
    lets LiveKit Cloud treat them as one interchangeable pool."""
    s = _enable_livekit(monkeypatch)

    monkeypatch.setattr(s, "livekit_worker_instance_id", "worker-a")
    opts_a = worker._build_worker_options()
    label_a = worker._worker_instance_id()

    monkeypatch.setattr(s, "livekit_worker_instance_id", "worker-b")
    opts_b = worker._build_worker_options()
    label_b = worker._worker_instance_id()

    # Dispatch identity + capacity are identical across the two "machines" ...
    assert opts_a.agent_name == opts_b.agent_name
    assert opts_a.load_threshold == opts_b.load_threshold
    assert opts_a.num_idle_processes == opts_b.num_idle_processes
    # ... and only the human log label differs (never affects dispatch).
    assert label_a == "worker-a" and label_b == "worker-b"


def test_no_per_case_or_per_prompt_worker_routing(monkeypatch):
    """A worker must NOT specialize by case/prompt - every worker under the same
    agent_name can serve every patient, so LiveKit Cloud can send any job to any
    worker. agent_name is a fixed constant, not derived from case/prompt."""
    s = _enable_livekit(monkeypatch)
    opts = worker._build_worker_options()
    name = opts.agent_name
    for case in ("carly", "camden", "sofia", "jayden"):
        assert case not in name
    # request handler accepts by fixed identity, not by case/prompt.
    assert worker.AGENT_PARTICIPANT_IDENTITY == "patient-agent"


def test_pocsession_worker_id_is_optional_and_stored(monkeypatch):
    """PocAgentSession accepts an optional worker_id (log-only) without breaking
    existing constructor call sites that omit it."""
    from tests.test_livekit_poc import _FakeAgentRoom

    room = _FakeAgentRoom()
    # Omitted -> defaults to "" (backward compatible with existing tests).
    sess = worker.PocAgentSession(
        room=room, session_id="s1", case_id="carly", on_shutdown=lambda r: None,
    )
    assert sess._worker_id == ""
    # Provided -> stored for distribution logging.
    sess2 = worker.PocAgentSession(
        room=room, session_id="s2", case_id="carly", on_shutdown=lambda r: None,
        worker_id="AW_abc123",
    )
    assert sess2._worker_id == "AW_abc123"
