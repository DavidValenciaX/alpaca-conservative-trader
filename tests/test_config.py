"""Configuration normalization and validation tests."""

from __future__ import annotations

import pytest

from config import FundamentalConfig, StrategyConfig, load_config


def test_assets_are_normalized_and_deduplicated(monkeypatch):
    monkeypatch.setenv("ASSETS", " spy, QQQ,spy ,,gld ")
    assert StrategyConfig().assets == ["SPY", "QQQ", "GLD"]


def test_scored_entry_defaults_are_conservative(monkeypatch):
    monkeypatch.delenv("BUY_SIGNAL_MODE", raising=False)
    monkeypatch.delenv("BUY_MIN_SCORE", raising=False)
    monkeypatch.delenv("REQUIRE_UPTREND", raising=False)
    cfg = StrategyConfig()
    assert cfg.buy_signal_mode == "score"
    assert cfg.buy_min_score == 5
    assert cfg.require_uptrend is True


def test_empty_static_assets_allowed_with_runtime_file(monkeypatch):
    monkeypatch.setenv("ASSETS", "")
    monkeypatch.setenv("ASSETS_FILE", "assets.txt")
    cfg = StrategyConfig()
    assert cfg.assets == []
    assert cfg.assets_file == "assets.txt"


def test_fundamental_llm_defaults_bound_requests(monkeypatch):
    for key in (
        "FUNDAMENTAL_MAX_NEWS",
        "FUNDAMENTAL_NEWS_MAX_CHARS",
        "FUNDAMENTAL_LLM_MAX_ATTEMPTS",
        "FUNDAMENTAL_LLM_CIRCUIT_MINUTES",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_TOKENS",
        "LLM_THINKING_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = FundamentalConfig()

    assert cfg.max_news == 10
    assert cfg.news_max_chars == 700
    assert cfg.llm_max_attempts == 2
    assert cfg.llm_circuit_minutes == 30
    assert cfg.llm_timeout_seconds == 45
    assert cfg.llm_max_tokens == 2048
    assert cfg.llm_thinking_enabled is False


def test_fundamental_llm_settings_parse_from_environment(monkeypatch):
    monkeypatch.setenv("FUNDAMENTAL_MAX_NEWS", "8")
    monkeypatch.setenv("FUNDAMENTAL_NEWS_MAX_CHARS", "500")
    monkeypatch.setenv("FUNDAMENTAL_LLM_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("FUNDAMENTAL_LLM_CIRCUIT_MINUTES", "45")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1024")
    monkeypatch.setenv("LLM_THINKING_ENABLED", "true")

    cfg = FundamentalConfig()

    assert cfg.max_news == 8
    assert cfg.news_max_chars == 500
    assert cfg.llm_max_attempts == 1
    assert cfg.llm_circuit_minutes == 45
    assert cfg.llm_timeout_seconds == 60
    assert cfg.llm_max_tokens == 1024
    assert cfg.llm_thinking_enabled is True


# ── Bot modes ────────────────────────────────────────────────────────────


def _set_credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


def test_custom_mode_keeps_env_driven_values(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "custom")
    monkeypatch.setenv("STOP_LOSS_PCT", "1.2")
    cfg = load_config()
    assert cfg.bot_mode == "custom"
    assert cfg.risk.stop_loss_pct == 1.2


def test_named_mode_overrides_env_managed_params(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "agresivo")
    monkeypatch.setenv("STOP_LOSS_PCT", "2.0")
    monkeypatch.setenv("RSI_OVERSOLD", "40")
    cfg = load_config()
    assert cfg.bot_mode == "agresivo"
    assert cfg.risk.stop_loss_pct == 3.5
    assert cfg.risk.take_profit_pct == 8.0
    assert cfg.risk.max_total_exposure_pct == 80.0
    assert cfg.strategy.rsi_oversold == 48.0
    assert cfg.strategy.require_uptrend is False


def test_mode_env_var_is_case_insensitive(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "Balanceado")
    cfg = load_config()
    assert cfg.bot_mode == "balanceado"
    assert cfg.risk.max_total_exposure_pct == 45.0


def test_invalid_mode_fails_validation(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "yolo")
    with pytest.raises(ValueError, match="Unknown BOT_MODE"):
        load_config()


def test_bot_mode_file_is_loaded_from_env(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "custom")
    monkeypatch.setenv("BOT_MODE_FILE", "mode.txt")
    cfg = load_config()
    assert cfg.bot_mode_file == "mode.txt"
