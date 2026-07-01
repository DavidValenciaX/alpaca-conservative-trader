"""
Unit tests for the market gate in main.py: clock-authoritative open/close
detection (holidays, early closes) with a local fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import main
from portfolio import MarketStatus


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
