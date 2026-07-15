"""
Unit tests for OrderExecutor's fill-reconciliation parsing and the
bracket-leg cancellation performed before a manual close.

No real Alpaca client is created: static parsers are tested directly, and a
fake trading client is injected into a bare OrderExecutor instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from executor import FilledExitOrder, OrderExecutor


def fake_order(**kwargs):
    defaults = {
        "id": "id",
        "symbol": "SPY",
        "side": "sell",
        "status": "filled",
        "filled_avg_price": "100.0",
        "filled_qty": "10",
        "qty": "10",
        "filled_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "order_class": "simple",
        "type": "market",
        "legs": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── Static parsing helpers ────────────────────────────────────────────────


def test_to_filled_exit_parses_filled_sell():
    result = OrderExecutor._to_filled_exit(fake_order())
    assert isinstance(result, FilledExitOrder)
    assert result.symbol == "SPY"
    assert result.filled_avg_price == 100.0
    assert result.filled_qty == 10.0


def test_to_filled_exit_ignores_buy_orders():
    assert OrderExecutor._to_filled_exit(fake_order(side="buy")) is None


def test_to_filled_exit_ignores_unfilled_orders():
    assert OrderExecutor._to_filled_exit(fake_order(status="new")) is None


def test_to_filled_exit_ignores_zero_price():
    assert OrderExecutor._to_filled_exit(fake_order(filled_avg_price="0")) is None


def test_coerce_datetime_from_iso_string():
    parsed = OrderExecutor._coerce_datetime("2026-01-01T00:00:00Z")
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_enum_to_str_unwraps_enum_like():
    assert OrderExecutor._enum_to_str(SimpleNamespace(value="sell")) == "sell"
    assert OrderExecutor._enum_to_str("buy") == "buy"


# ── Bracket leg extraction ────────────────────────────────────────────────


def test_extract_prefers_filled_legs_over_parent():
    filled_leg = fake_order(id="leg1", side="sell", status="filled")
    open_leg = fake_order(id="leg2", side="sell", status="new")
    parent = fake_order(id="parent", side="buy", status="filled", legs=[filled_leg, open_leg])

    result = OrderExecutor._extract_filled_exit_orders(parent)
    ids = {o.order_id for o in result}
    assert ids == {"leg1"}


def test_extract_uses_primary_when_no_legs():
    order = fake_order(id="solo", legs=None)
    result = OrderExecutor._extract_filled_exit_orders(order)
    assert [o.order_id for o in result] == ["solo"]


# ── Cancel bracket legs before close ──────────────────────────────────────


class FakeClient:
    def __init__(self, open_orders):
        self._open_orders = open_orders
        self.cancelled = []
        self.closed = []
        self.submitted = []

    def get_orders(self, filter=None):
        return self._open_orders

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def close_position(self, symbol_or_asset_id):
        self.closed.append(symbol_or_asset_id)

    def submit_order(self, order_data=None):
        self.submitted.append(order_data)
        return fake_order(id="new", status="new")


def make_config(use_limit_entry=True, entry_limit_buffer_pct=0.1):
    strategy = SimpleNamespace(
        use_limit_entry=use_limit_entry,
        entry_limit_buffer_pct=entry_limit_buffer_pct,
    )
    return SimpleNamespace(strategy=strategy)


def make_executor(open_orders, config=None):
    ex = object.__new__(OrderExecutor)
    ex._client = FakeClient(open_orders)
    ex._config = config
    return ex


def test_cancel_open_orders_for_symbol_cancels_matching_legs():
    tp_leg = fake_order(id="tp", side="sell", status="new")
    sl_leg = fake_order(id="sl", side="sell", status="new")
    bracket = fake_order(id="parent", symbol="SPY", side="buy", status="filled",
                         legs=[tp_leg, sl_leg])
    other = fake_order(id="other", symbol="QQQ", side="sell", status="new")

    ex = make_executor([bracket, other])
    count = ex.cancel_open_orders_for_symbol("SPY")

    assert count == 2
    assert set(ex._client.cancelled) == {"tp", "sl"}


def test_close_position_cancels_orders_first():
    tp_leg = fake_order(id="tp", symbol="SPY", side="sell", status="new")
    ex = make_executor([fake_order(id="p", symbol="SPY", side="buy",
                                   status="filled", legs=[tp_leg])])

    ok = ex.close_position("SPY")
    assert ok is True
    assert "tp" in ex._client.cancelled
    assert "SPY" in ex._client.closed


# ── Duplicate-entry guard ─────────────────────────────────────────────────


def test_has_open_order_detects_matching_leg():
    leg = fake_order(id="tp", symbol="SPY", side="sell", status="new")
    ex = make_executor([fake_order(id="p", symbol="QQQ", side="buy",
                                   status="filled", legs=[leg])])
    # Leg symbol matches SPY even though parent is QQQ.
    assert ex.has_open_order_for_symbol("SPY") is True


def test_has_open_order_false_when_none_match():
    ex = make_executor([fake_order(id="o", symbol="QQQ", side="sell", status="new")])
    assert ex.has_open_order_for_symbol("SPY") is False


# ── Entry order type (limit vs market) ────────────────────────────────────


def test_estimated_entry_price_uses_limit_buffer():
    ex = make_executor(
        [],
        config=make_config(use_limit_entry=True, entry_limit_buffer_pct=0.25),
    )
    assert ex.estimated_entry_price(100.0, side="BUY") == 100.25


def test_estimated_entry_price_uses_signal_price_for_market_order():
    ex = make_executor([], config=make_config(use_limit_entry=False))
    assert ex.estimated_entry_price(100.1234, side="BUY") == 100.1234


def test_limit_entry_builds_limit_request_with_buffer():
    ex = make_executor([], config=make_config(use_limit_entry=True,
                                              entry_limit_buffer_pct=0.1))
    ex.place_bracket_order(
        symbol="SPY", quantity=10, side="BUY", entry_price=100.0,
        stop_loss_price=98.5, take_profit_price=102.5,
    )
    request = ex._client.submitted[0]
    assert isinstance(request, LimitOrderRequest)
    assert float(request.limit_price) == 100.1  # 100 * (1 + 0.1%)


def test_market_entry_builds_market_request():
    ex = make_executor([], config=make_config(use_limit_entry=False))
    ex.place_bracket_order(
        symbol="SPY", quantity=10, side="BUY", entry_price=100.0,
        stop_loss_price=98.5, take_profit_price=102.5,
    )
    request = ex._client.submitted[0]
    assert isinstance(request, MarketOrderRequest)
