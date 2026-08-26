"""Safety and fallback tests for the fundamental worker/overlay."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fundamental_agent import FundamentalAssessment
from fundamental_types import FundamentalAgentError
from fundamental_overlay import FundamentalService
from macro_data import MacroSnapshot


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 14, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def config(tmp_path, mode="overlay", fail_open=True, **overrides):
    fundamental = SimpleNamespace(
        enabled=True,
        mode=mode,
        poll_interval_minutes=15,
        min_inference_gap_minutes=0,
        state_ttl_minutes=120,
        fail_open=fail_open,
        veto_confidence=0.75,
        state_file=str(tmp_path / "fundamental.json"),
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_timeout_seconds=20,
        rss_urls=[],
        fred_api_key="",
        fred_series=[],
    )
    for key, value in overrides.items():
        setattr(fundamental, key, value)
    return SimpleNamespace(
        strategy=SimpleNamespace(assets=["SPY", "QQQ"]),
        fundamental=fundamental,
        alpaca=SimpleNamespace(api_key="", secret_key=""),
    )


def assessment(stance="neutral", confidence=0.4, event_risk="low", generated_at=None):
    generated_at = generated_at or datetime.now(timezone.utc)
    view = {
        "stance": stance,
        "confidence": confidence,
        "event_risk": event_risk,
        "horizon": "swing",
        "reasons": ["test"],
        "sources": ["test"],
    }
    return FundamentalAssessment.from_dict(
        {"regime": "neutral", "asset_views": {"SPY": view, "QQQ": view}},
        ["SPY", "QQQ"],
        generated_at=generated_at,
    )


class MacroFake:
    def fetch_snapshot(self, series):
        return MacroSnapshot({}, datetime.now(timezone.utc), available=False)


class AgentFake:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def analyze(self, news, macro, technical_context):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return self.result


class FailingSource:
    def __init__(self):
        self.calls = 0

    def fetch_since(self, symbols, since, until):
        self.calls += 1
        raise TimeoutError("provider timeout")


class FailingAgent:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def analyze(self, news, macro, technical_context):
        self.calls += 1
        raise self.error


def service(tmp_path, result=None, mode="overlay", **kwargs):
    clock = Clock()
    agent = AgentFake(result or assessment())
    instance = FundamentalService(
        config(tmp_path, mode=mode, **kwargs),
        sources=[],
        macro_source=MacroFake(),
        agent=agent,
        now_fn=clock,
    )
    return instance, agent, clock


def test_shadow_mode_never_vetoes(tmp_path):
    service_instance, _, _ = service(
        tmp_path, result=assessment("negative", 0.99, "high"), mode="shadow"
    )
    assert service_instance.check_buy("SPY").allowed is True
    assert service_instance.check_buy("SPY").status == "shadow"


def test_overlay_vetoes_only_negative_high_confidence_or_high_event_risk(tmp_path):
    negative, _, _ = service(tmp_path, result=assessment("negative", 0.8, "medium"))
    negative._assessment = negative._assessment or assessment("negative", 0.8, "medium")
    assert negative.check_buy("SPY").allowed is False

    event, _, _ = service(tmp_path / "event", result=assessment("positive", 0.2, "high"))
    event._assessment = event._assessment or assessment("positive", 0.2, "high")
    assert event.check_buy("SPY").allowed is False

    positive, _, _ = service(tmp_path / "positive", result=assessment("positive", 0.9))
    positive._assessment = positive._assessment or assessment("positive", 0.9)
    assert positive.check_buy("SPY").allowed is True


def test_stale_or_failed_state_falls_back_to_technical_only(tmp_path):
    instance, _, clock = service(tmp_path)
    instance._assessment = assessment(generated_at=clock() - timedelta(minutes=121))
    decision = instance.check_buy("SPY")
    assert decision.allowed is True
    assert decision.status == "technical_only"

    strict, _, _ = service(tmp_path / "strict", fail_open=False)
    strict._assessment = None
    assert strict.check_buy("SPY").allowed is False


def test_refresh_persists_valid_state_and_context(tmp_path):
    instance, agent, _ = service(tmp_path)
    result = SimpleNamespace(
        symbol="spy",
        signal=SimpleNamespace(value="BUY"),
        price=100.0,
        reason="technical pullback",
        indicators={"buy_score": 5.0},
    )
    instance.update_technical_context(result)
    instance.refresh_once()
    assert agent.calls == 1
    persisted = json.loads((tmp_path / "fundamental.json").read_text())
    assert persisted["assessment"]["regime"] == "neutral"
    assert persisted["last_inference_at"] == "2026-08-24T14:00:00+00:00"
    assert instance.status()["assessment_available"] is True


def test_inference_status_reports_success_and_latency(tmp_path):
    instance, agent, _ = service(tmp_path)

    instance.refresh_once()

    status = instance.status()
    assert agent.calls == 1
    assert status["inference_attempts"] == 1
    assert status["inference_successes"] == 1
    assert status["inference_failures"] == 0
    assert status["inference_success_rate"] == 1.0
    assert status["last_inference_elapsed_seconds"] is not None
    assert status["last_inference_error_type"] is None


def test_two_refreshes_never_run_llm_concurrently(tmp_path):
    instance, agent, _ = service(tmp_path)
    threads = [threading.Thread(target=instance.refresh_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert agent.max_active == 1


def test_failed_source_retries_then_opens_a_circuit(tmp_path):
    clock = Clock()
    source = FailingSource()
    instance = FundamentalService(
        config(tmp_path),
        sources=[source],
        macro_source=MacroFake(),
        agent=None,
        now_fn=clock,
        sleep_fn=lambda _seconds: None,
    )
    instance.refresh_once()
    assert source.calls == 3
    instance.refresh_once()
    assert source.calls == 3


def test_transient_inference_failure_has_one_retry_and_long_circuit(tmp_path):
    clock = Clock()
    agent = FailingAgent(TimeoutError("LLM timeout"))
    instance = FundamentalService(
        config(tmp_path),
        sources=[],
        macro_source=MacroFake(),
        agent=agent,
        now_fn=clock,
        sleep_fn=lambda _seconds: None,
    )

    instance.refresh_once()

    assert agent.calls == 2
    assert instance.status()["inference_circuit_until"] == (
        clock() + timedelta(minutes=30)
    ).isoformat()
    assert instance.status()["inference_attempts"] == 2
    assert instance.status()["inference_successes"] == 0
    assert instance.status()["inference_failures"] == 2
    assert instance.status()["inference_success_rate"] == 0.0
    assert instance.status()["last_inference_error_type"] == "TimeoutError"

    instance.refresh_once()
    assert agent.calls == 2


def test_non_transient_inference_failure_is_not_retried(tmp_path):
    agent = FailingAgent(FundamentalAgentError("invalid JSON"))
    instance = FundamentalService(
        config(tmp_path),
        sources=[],
        macro_source=MacroFake(),
        agent=agent,
        sleep_fn=lambda _seconds: None,
    )

    instance.refresh_once()

    assert agent.calls == 1
