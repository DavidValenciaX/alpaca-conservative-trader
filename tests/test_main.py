"""
Unit tests for the market gate in main.py: clock-authoritative open/close
detection (holidays, early closes) with a local fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import main
from config import AppConfig
from portfolio import MarketStatus
from strategy import Signal, SignalResult


class FakePortfolio:
    def __init__(self, status):
        self._status = status

    def get_market_status(self):
        return self._status


def test_gate_closed_when_clock_says_closed():
    portfolio = FakePortfolio(MarketStatus(is_open=False, next_open=None, next_close=None))
    assert main._market_gate(portfolio) is False


def test_gate_blocks_near_close(monkeypatch):
    monkeypatch.setattr(main, "_is_in_open_no_trade_window", lambda: False)
    near_close = datetime.now(timezone.utc) + timedelta(minutes=5)
    portfolio = FakePortfolio(
        MarketStatus(is_open=True, next_open=None, next_close=near_close)
    )
    assert main._market_gate(portfolio) is False


def test_gate_blocks_in_opening_window(monkeypatch):
    monkeypatch.setattr(main, "_is_in_open_no_trade_window", lambda: True)
    far_close = datetime.now(timezone.utc) + timedelta(hours=3)
    portfolio = FakePortfolio(
        MarketStatus(is_open=True, next_open=None, next_close=far_close)
    )
    assert main._market_gate(portfolio) is False


def test_gate_open_when_clear(monkeypatch):
    monkeypatch.setattr(main, "_is_in_open_no_trade_window", lambda: False)
    far_close = datetime.now(timezone.utc) + timedelta(hours=3)
    portfolio = FakePortfolio(
        MarketStatus(is_open=True, next_open=None, next_close=far_close)
    )
    assert main._market_gate(portfolio) is True


def test_gate_handles_naive_next_close(monkeypatch):
    monkeypatch.setattr(main, "_is_in_open_no_trade_window", lambda: False)
    naive_near_close = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).replace(tzinfo=None)  # simulate a tz-naive clock value
    portfolio = FakePortfolio(
        MarketStatus(is_open=True, next_open=None, next_close=naive_near_close)
    )
    assert main._market_gate(portfolio) is False


def test_gate_fallback_open_when_clock_unavailable(monkeypatch):
    monkeypatch.setattr(main, "_is_market_open", lambda: True)
    monkeypatch.setattr(main, "_is_in_no_trade_window", lambda: False)
    portfolio = FakePortfolio(None)
    assert main._market_gate(portfolio) is True


def test_gate_fallback_closed_when_clock_unavailable(monkeypatch):
    monkeypatch.setattr(main, "_is_market_open", lambda: False)
    portfolio = FakePortfolio(None)
    assert main._market_gate(portfolio) is False


def test_runtime_asset_file_overrides_config_and_keeps_held_symbol(tmp_path):
    assets_file = tmp_path / "assets.txt"
    assets_file.write_text("aapl, MSFT\n# comment\nAAPL\n", encoding="utf-8")

    result = main._resolve_asset_universe(
        configured_assets=["SPY", "QQQ"],
        held_symbols=["GLD"],
        assets_file=str(assets_file),
    )

    assert result == ["AAPL", "MSFT", "GLD"]


class CyclePortfolio:
    def get_snapshot(self):
        position = SimpleNamespace(
            symbol="SPY",
            qty=10.0,
            avg_entry_price=100.0,
            current_price=101.0,
            market_value=1010.0,
        )
        return SimpleNamespace(
            portfolio_value=10_000.0,
            buying_power=20_000.0,
            last_equity=10_000.0,
            positions=[position],
        )


class CycleRiskManager:
    can_open_positions = False

    def sync_existing_positions(self, _positions):
        pass

    def check_daily_loss(self, **_kwargs):
        pass


class CycleDataFeed:
    def get_historical_bars(self, symbols, days_back):
        index = pd.MultiIndex.from_tuples(
            [("SPY", pd.Timestamp("2026-01-01", tz="UTC"))],
            names=["symbol", "timestamp"],
        )
        return pd.DataFrame({"close": [101.0]}, index=index)


class SellStrategy:
    def compute_indicators(self, df):
        return df

    def evaluate(self, symbol, _row, has_position):
        assert has_position is True
        return SignalResult(symbol=symbol, signal=Signal.SELL, price=101.0)


class CycleExecutor:
    def __init__(self):
        self.closed = []

    def get_recent_filled_sell_orders(self, limit):
        return []

    def close_position(self, symbol):
        self.closed.append(symbol)
        return True


def test_risk_halt_blocks_entries_but_still_allows_exit(monkeypatch):
    executor = CycleExecutor()
    monkeypatch.setattr(main, "_market_gate", lambda _portfolio: True)
    monkeypatch.setattr(main, "_shutdown_requested", False)

    main.run_trading_cycle(
        data_feed=CycleDataFeed(),
        strategy=SellStrategy(),
        risk_manager=CycleRiskManager(),
        executor=executor,
        portfolio=CyclePortfolio(),
        assets=["SPY"],
        use_closed_bars_only=False,
    )

    assert executor.closed == ["SPY"]


# ── Bot mode hot-reload ──────────────────────────────────────────────────


def _make_app_config(monkeypatch, bot_mode: str, mode_file) -> AppConfig:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    return AppConfig(
        bot_mode=bot_mode,
        bot_mode_file=str(mode_file) if mode_file else "",
    )


def test_mode_file_switches_mode_in_place(monkeypatch, tmp_path):
    mode_file = tmp_path / "mode.txt"
    mode_file.write_text("# comment\nagresivo\n", encoding="utf-8")
    config = _make_app_config(monkeypatch, "conservador", mode_file)
    risk_ref = config.risk

    main._maybe_reload_mode(config)

    assert config.bot_mode == "agresivo"
    assert config.risk is risk_ref
    assert config.risk.stop_loss_pct == 3.5
    assert config.risk.max_total_exposure_pct == 80.0
    assert config.strategy.rsi_oversold == 48.0


def test_mode_file_with_invalid_mode_keeps_current(monkeypatch, tmp_path):
    mode_file = tmp_path / "mode.txt"
    mode_file.write_text("yolo\n", encoding="utf-8")
    config = _make_app_config(monkeypatch, "conservador", mode_file)
    original_stop = config.risk.stop_loss_pct

    main._maybe_reload_mode(config)

    assert config.bot_mode == "conservador"
    assert config.risk.stop_loss_pct == original_stop


def test_mode_reload_is_noop_without_file_or_change(monkeypatch, tmp_path):
    config = _make_app_config(monkeypatch, "conservador", None)
    main._maybe_reload_mode(config)
    assert config.bot_mode == "conservador"

    mode_file = tmp_path / "mode.txt"
    mode_file.write_text("Conservador\n", encoding="utf-8")
    config = _make_app_config(monkeypatch, "conservador", mode_file)
    main._maybe_reload_mode(config)
    assert config.bot_mode == "conservador"
