"""Configuration normalization and validation tests."""

from __future__ import annotations

from config import StrategyConfig


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
