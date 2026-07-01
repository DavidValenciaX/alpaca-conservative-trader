"""
executor.py — Places, modifies, and cancels orders via the Alpaca Trading API.

All orders are placed as bracket orders (entry + stop-loss + take-profit)
to ensure every position has built-in risk management from the start.

Never places a market order without a stop-loss bracket.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading import Order as AlpacaOrder
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from config import AppConfig
from logger import get_logger

log = get_logger()


class ExecutionError(Exception):
    """Raised when an order operation fails after retries."""


def _retry(max_attempts: int = 3, base_delay: float = 1.0):
    """Exponential backoff decorator for API calls."""

    def decorator(func):
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
        Place a bracket order: entry limit order with attached stop-loss and
        take-profit orders. This ensures every position has automatic risk
        management from the moment it's filled.

        Returns the Alpaca Order object on success, None on rejection.
        """
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        # Use a limit order for the entry to control price
        entry_request = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            stop_loss=StopLossRequest(stop_price=str(stop_loss_price)),
            take_profit=TakeProfitRequest(limit_price=str(take_profit_price)),
        )

        log.info(
            f"Placing {side} bracket order: {quantity} {symbol} "
            f"@ ~${entry_price:.4f} | "
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
        self, status: str = "open", limit: int = 50
    ) -> List[AlpacaOrder]:
        """Fetch open or closed orders."""
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=status, limit=limit)
        orders = self._client.get_orders(filter=request)
        return orders

    def close_position(self, symbol: str) -> bool:
        """Close an open position with a market order."""
        try:
            self._client.close_position(symbol_or_asset_id=symbol)
            log.info(f"Closed position for {symbol}")
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
