"""Configuration normalization and validation tests."""

from __future__ import annotations

import pytest

from config import StrategyConfig, load_config


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
