"""
executor.py — Places, modifies, and cancels orders via the Alpaca Trading API.

All orders are placed as bracket orders (entry + stop-loss + take-profit)
to ensure every position has built-in risk management from the start.

Never places a market order without a stop-loss bracket.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import List, Optional
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading import Order as AlpacaOrder
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from config import AppConfig
from logger import get_logger

log = get_logger()


class ExecutionError(Exception):
    """Raised when an order operation fails after retries."""


@dataclass
class FilledExitOrder:
    """Represents a filled SELL order that fully or partially exits a trade."""

    order_id: str
    symbol: str
    filled_avg_price: float
    filled_qty: float
    filled_at: datetime
    order_class: str = ""
    order_type: str = ""


def _retry(max_attempts: int = 3, base_delay: float = 1.0):
    """Exponential backoff decorator for API calls."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    log.warning(
                        f"{func.__name__} attempt {attempt}/{max_attempts} failed: {e}"
                    )
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        time.sleep(delay)
            raise ExecutionError(str(last_exc)) from last_exc

        return wrapper

    return decorator


class OrderExecutor:
    """Manages order placement, modification, and cancellation."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = TradingClient(
            api_key=config.alpaca.api_key,
            secret_key=config.alpaca.secret_key,
            paper=config.paper_mode,
        )

    def estimated_entry_price(self, signal_price: float, side: str = "BUY") -> float:
        """Return the worst-case entry price used for sizing and risk checks."""
        strategy_cfg = self._config.strategy
        if not strategy_cfg.use_limit_entry:
            return signal_price

        buffer = strategy_cfg.entry_limit_buffer_pct / 100.0
        multiplier = 1 + buffer if side.upper() == "BUY" else 1 - buffer
        return round(signal_price * multiplier, 2)

    @_retry(max_attempts=3)
    def place_bracket_order(
        self,
        symbol: str,
        quantity: float,
        side: str,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> Optional[AlpacaOrder]:
        """
        Place a bracket order with attached stop-loss and take-profit orders,
        so every position has automatic risk management from the moment it fills.

        The entry defaults to a limit order (slippage-capped) when
        ``strategy.use_limit_entry`` is enabled; otherwise a market order.

        Returns the Alpaca Order object on success, None on rejection.
        """
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        strategy_cfg = self._config.strategy
        common = dict(
            symbol=symbol,
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            stop_loss=StopLossRequest(stop_price=str(stop_loss_price)),
            take_profit=TakeProfitRequest(limit_price=str(take_profit_price)),
        )

        if strategy_cfg.use_limit_entry:
            # Buy slightly above / sell slightly below the signal price to allow
            # small moves to fill while still capping slippage.
            limit_price = self.estimated_entry_price(entry_price, side)
            entry_request = LimitOrderRequest(limit_price=str(limit_price), **common)
            entry_desc = f"LIMIT @ ${limit_price:.2f}"
        else:
            entry_request = MarketOrderRequest(**common)
            entry_desc = f"MARKET @ ~${entry_price:.4f}"

        log.info(
            f"Placing {side} bracket order: {quantity} {symbol} | "
            f"entry {entry_desc} | "
            f"SL: ${stop_loss_price:.4f} | TP: ${take_profit_price:.4f}"
        )

        try:
            order = self._client.submit_order(order_data=entry_request)
            log.info(
                f"Order submitted: id={order.id}, "
                f"symbol={order.symbol}, "
                f"side={order.side}, "
                f"qty={order.qty}, "
                f"status={order.status}"
            )
            return order
        except Exception as e:
            rejection_reason = str(e)
            log.error(f"Order REJECTED: {symbol} {side} {quantity} — {rejection_reason}")
            return None

    @_retry(max_attempts=3)
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        try:
            self._client.cancel_order_by_id(order_id=order_id)
            log.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            log.warning(f"Failed to cancel order {order_id}: {e}")
            return False

    @_retry(max_attempts=3)
    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        try:
            self._client.cancel_all_orders()
            log.info("All open orders cancelled")
        except Exception as e:
            log.warning(f"Failed to cancel all orders: {e}")

    @_retry(max_attempts=3)
    def get_orders(
        self,
        status: str = "open",
        limit: int = 50,
        after: Optional[datetime] = None,
        nested: bool = False,
    ) -> List[AlpacaOrder]:
        """Fetch open or closed orders."""
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=status,
            limit=limit,
            after=after,
            nested=nested,
        )
        orders = self._client.get_orders(filter=request)
        return orders

    def get_recent_filled_sell_orders(self, limit: int = 200) -> List[FilledExitOrder]:
        """
        Fetch recent filled SELL orders, including filled bracket legs.

        Alpaca's order history is submission-ordered, so we fetch a bounded recent
        window and deduplicate downstream instead of relying on `after=` filters.
        """
        recent_orders = self.get_orders(status="closed", limit=limit, nested=True)
        filled_exits: List[FilledExitOrder] = []

        for order in recent_orders:
            filled_exits.extend(self._extract_filled_exit_orders(order))

        filled_exits.sort(key=lambda order: order.filled_at)
        return filled_exits

    @staticmethod
    def _extract_filled_exit_orders(order: AlpacaOrder) -> List[FilledExitOrder]:
        filled: List[FilledExitOrder] = []

        legs = getattr(order, "legs", None) or []
        for leg in legs:
            leg_fill = OrderExecutor._to_filled_exit(leg)
            if leg_fill:
                filled.append(leg_fill)

        primary_fill = OrderExecutor._to_filled_exit(order)
        if primary_fill and not legs:
            filled.append(primary_fill)

        return filled

    @staticmethod
    def _to_filled_exit(order: AlpacaOrder) -> Optional[FilledExitOrder]:
        side = OrderExecutor._enum_to_str(getattr(order, "side", ""))
        status = OrderExecutor._enum_to_str(getattr(order, "status", ""))
        filled_avg_price = getattr(order, "filled_avg_price", None)
        filled_qty = getattr(order, "filled_qty", None) or getattr(order, "qty", None)
        filled_at = getattr(order, "filled_at", None)

        if side != "sell" or status != "filled":
            return None
        if filled_avg_price in (None, "", "0", 0) or filled_qty in (None, "", "0", 0):
            return None

        parsed_filled_at = OrderExecutor._coerce_datetime(filled_at)
        if not parsed_filled_at:
            return None

        return FilledExitOrder(
            order_id=str(getattr(order, "id", "")),
            symbol=str(getattr(order, "symbol", "")),
            filled_avg_price=float(filled_avg_price),
            filled_qty=float(filled_qty),
            filled_at=parsed_filled_at,
            order_class=OrderExecutor._enum_to_str(getattr(order, "order_class", "")),
            order_type=OrderExecutor._enum_to_str(
                getattr(order, "type", getattr(order, "order_type", ""))
            ),
        )

    @staticmethod
    def _enum_to_str(value) -> str:
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    @staticmethod
    def _coerce_datetime(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def has_open_order_for_symbol(self, symbol: str) -> bool:
        """
        Return True if there is any resting (non-terminal) order for the symbol.

        Used to avoid stacking duplicate entries when a previous limit entry is
        still working and has not filled yet.
        """
        try:
            open_orders = self.get_orders(status="open", limit=200, nested=True)
        except Exception as e:
            log.warning(f"Could not fetch open orders for {symbol}: {e}")
            # Fail safe: assume an order exists so we don't stack entries.
            return True

        for order in open_orders:
            candidates = [order] + list(getattr(order, "legs", None) or [])
            for candidate in candidates:
                if str(getattr(candidate, "symbol", "")) == symbol:
                    return True
        return False

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        """
        Cancel every open order tied to a symbol (including live bracket legs).

        Bracket stop-loss / take-profit legs reserve the position's shares, so
        they must be cancelled before a manual close, otherwise Alpaca rejects
        the close with an "insufficient qty / held for orders" error.

        Returns the number of orders for which a cancel was requested.
        """
        try:
            open_orders = self.get_orders(status="open", limit=200, nested=True)
        except Exception as e:
            log.warning(f"Could not fetch open orders for {symbol}: {e}")
            return 0

        terminal_states = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
        cancelled = 0
        for order in open_orders:
            candidates = [order] + list(getattr(order, "legs", None) or [])
            for candidate in candidates:
                if str(getattr(candidate, "symbol", "")) != symbol:
                    continue
                status = self._enum_to_str(getattr(candidate, "status", "")).lower()
                if status in terminal_states:
                    continue
                order_id = str(getattr(candidate, "id", ""))
                if not order_id:
                    continue
                if self.cancel_order(order_id):
                    cancelled += 1

        if cancelled:
            log.info(f"Cancelled {cancelled} open order(s) for {symbol} before close")
        return cancelled

    def close_position(self, symbol: str, cancel_orders: bool = True) -> bool:
        """Close an open position with a market order.

        By default cancels any open bracket legs for the symbol first so the
        close is not rejected for shares held by resting SL/TP orders.
        """
        if cancel_orders:
            self.cancel_open_orders_for_symbol(symbol)
        try:
            self._client.close_position(symbol_or_asset_id=symbol)
            log.info(f"Submitted close order for {symbol}")
            return True
        except Exception as e:
            log.warning(f"Failed to close position for {symbol}: {e}")
            return False

    def close_all_positions(self) -> None:
        """Close all open positions."""
        try:
            self._client.close_all_positions()
            log.info("All positions closed")
        except Exception as e:
            log.warning(f"Failed to close all positions: {e}")
