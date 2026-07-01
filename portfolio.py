"""
portfolio.py — Tracks open positions, unrealized P&L, and cash balance.

Provides a snapshot of the current account state used by the risk manager
and for periodic portfolio logging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from alpaca.trading.client import TradingClient

from config import AppConfig
from logger import get_logger

log = get_logger()


@dataclass
class PositionInfo:
    """Summary of a single open position."""

    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_pct: float


@dataclass
class PortfolioSnapshot:
    """Full account snapshot at a point in time."""

    timestamp: datetime
    cash: float
    portfolio_value: float
    buying_power: float
    positions: List[PositionInfo]
    day_trade_count: int = 0

    @property
    def total_exposure_pct(self) -> float:
        """Percentage of portfolio value currently in positions."""
        if self.portfolio_value <= 0:
            return 0.0
        position_value = sum(p.market_value for p in self.positions)
        return (position_value / self.portfolio_value) * 100

    @property
    def daily_pl(self) -> float:
        """Total unrealized P&L across all positions."""
        return sum(p.unrealized_pl for p in self.positions)

    def summary(self) -> str:
        return (
            f"Portfolio: ${self.portfolio_value:.2f} | "
            f"Cash: ${self.cash:.2f} | "
            f"BP: ${self.buying_power:.2f} | "
            f"Positions: {len(self.positions)} | "
            f"Exposure: {self.total_exposure_pct:.1f}% | "
            f"Day P&L: ${self.daily_pl:.2f}"
        )


class PortfolioTracker:
    """Fetches and caches account and position data from Alpaca."""

    def __init__(self, config: AppConfig) -> None:
        self._client = TradingClient(
            api_key=config.alpaca.api_key,
            secret_key=config.alpaca.secret_key,
            paper=config.paper_mode,
        )
        self._config = config
        self._last_snapshot: Optional[PortfolioSnapshot] = None

    def get_snapshot(self) -> PortfolioSnapshot:
        """Fetch the latest account and position data from Alpaca."""
        try:
            account = self._client.get_account()
            positions_raw = self._client.get_all_positions()

            positions: List[PositionInfo] = []
            for pos in positions_raw:
                positions.append(
                    PositionInfo(
                        symbol=pos.symbol,
                        qty=float(pos.qty),
                        avg_entry_price=float(pos.avg_entry_price),
                        current_price=float(pos.current_price),
                        market_value=float(pos.market_value),
                        unrealized_pl=float(pos.unrealized_pl),
                        unrealized_pl_pct=float(pos.unrealized_pl_pct or 0.0),
                    )
                )

            snapshot = PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc),
                cash=float(account.cash),
                portfolio_value=float(account.portfolio_value),
                buying_power=float(account.buying_power),
                positions=positions,
                day_trade_count=int(account.day_trade_count),
            )

            self._last_snapshot = snapshot
            return snapshot

        except Exception as e:
            log.error(f"Failed to fetch portfolio snapshot: {e}")
            if self._last_snapshot:
                log.warning("Returning cached portfolio snapshot")
                return self._last_snapshot
            raise

    def get_position(self, symbol: str) -> Optional[PositionInfo]:
        """Get position info for a specific symbol, or None if not held."""
        snapshot = self.get_snapshot()
        for pos in snapshot.positions:
            if pos.symbol == symbol:
                return pos
        return None

    def has_position(self, symbol: str) -> bool:
        """Check if we currently hold a position in a given symbol."""
        return self.get_position(symbol) is not None

    def has_any_position(self) -> bool:
        """Check if there are any open positions."""
        snapshot = self.get_snapshot()
        return len(snapshot.positions) > 0

    def get_symbols_with_positions(self) -> List[str]:
        """Return list of symbols we currently hold positions in."""
        snapshot = self.get_snapshot()
        return [p.symbol for p in snapshot.positions]
