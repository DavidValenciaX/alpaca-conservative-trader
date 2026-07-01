"""
Unit tests for PortfolioTracker snapshot parsing.

These tests lock in compatibility with Alpaca account objects that omit some
optional fields, such as day_trade_count.
"""

from __future__ import annotations

from types import SimpleNamespace

from portfolio import PortfolioTracker


class FakeClient:
    def __init__(self, account, positions):
        self._account = account
        self._positions = positions

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return self._positions


def make_tracker(account, positions):
    tracker = object.__new__(PortfolioTracker)
    tracker._client = FakeClient(account=account, positions=positions)
    tracker._config = None
    tracker._last_snapshot = None
    return tracker


def test_get_snapshot_defaults_missing_day_trade_count_to_zero():
    account = SimpleNamespace(
        cash="1000",
        portfolio_value="1200",
        buying_power="1000",
        last_equity="1180",
    )
    positions = [
        SimpleNamespace(
            symbol="SPY",
            qty="2",
            avg_entry_price="500",
            current_price="505",
            market_value="1010",
            unrealized_pl="10",
            unrealized_pl_pct="0.01",
        )
    ]

    tracker = make_tracker(account=account, positions=positions)
    snapshot = tracker.get_snapshot()

    assert snapshot.day_trade_count == 0
    assert snapshot.last_equity == 1180.0
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "SPY"
