"""
data_feed.py — Fetches real-time bars and historical OHLCV from Alpaca.

All calls are wrapped with exponential backoff retry (max 3 attempts).
If data cannot be fetched, the error is logged and the caller handles the gap.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import List, Optional

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import AppConfig
from logger import get_logger

log = get_logger()


class DataFeedError(Exception):
    """Raised when market data cannot be fetched after all retries."""


def _retry(max_attempts: int = 3, base_delay: float = 1.0):
    """Decorator for exponential backoff retry."""

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
            log.error(f"{func.__name__} failed after {max_attempts} attempts.")
            raise DataFeedError(str(last_exc)) from last_exc

        return wrapper

    return decorator


class DataFeed:
    """Provides historical and recent bar data from Alpaca."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = StockHistoricalDataClient(
            api_key=config.alpaca.api_key,
            secret_key=config.alpaca.secret_key,
        )
        self._timeframe = self._resolve_timeframe(config.strategy.bar_timeframe)

    @staticmethod
    def _resolve_timeframe(bar_timeframe: str) -> TimeFrame:
        """Convert a string like '15Min' or '1Hour' to an Alpaca TimeFrame."""
        bar_timeframe = bar_timeframe.strip().lower()

        if bar_timeframe.endswith("min"):
            minutes = int(bar_timeframe.replace("min", ""))
            if minutes == 1:
                return TimeFrame.Minute
            elif minutes == 5:
                return TimeFrame.Minute * 5
            elif minutes == 15:
                return TimeFrame.Minute * 15
            else:
                return TimeFrame.Minute * minutes
        elif bar_timeframe.endswith("hour"):
            hours = int(bar_timeframe.replace("hour", ""))
            return TimeFrame.Hour * hours
        elif bar_timeframe.endswith("day"):
            days = int(bar_timeframe.replace("day", ""))
            return TimeFrame.Day * days

        log.warning(f"Unknown timeframe '{bar_timeframe}', falling back to 15Min.")
        return TimeFrame.Minute * 15

    @_retry(max_attempts=3)
    def get_historical_bars(
        self,
        symbols: List[str],
        days_back: int = 10,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV bars for the given symbols.

        Returns a MultiIndex DataFrame (symbol, timestamp) with columns:
        open, high, low, close, volume, trade_count, vwap.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)

        log.info(f"Fetching historical bars for {symbols} from {start.date()} to {end.date()}")

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=self._timeframe,
            start=start,
            end=end,
        )

        bars = self._client.get_stock_bars(request)
        df = bars.df

        if df.empty:
            log.warning(f"No historical data returned for {symbols}")
            return pd.DataFrame()

        log.info(f"Received {len(df)} bars for {df.index.get_level_values('symbol').nunique()} symbols")
        return df

    @_retry(max_attempts=3)
    def get_latest_bars(self, symbols: List[str]) -> pd.DataFrame:
        """
        Fetch the most recent bar for each symbol.

        Returns a DataFrame with symbols as index, columns: open, high, low,
        close, volume, timestamp.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=2)

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=self._timeframe,
            start=start,
            end=end,
            limit=2,  # grab 2 to ensure we have a complete bar
        )

        bars = self._client.get_stock_bars(request)
        df = bars.df

        if df.empty:
            log.warning(f"No latest bars for {symbols}")
            return pd.DataFrame()

        # Group by symbol and take the last row per symbol
        latest = df.groupby(level="symbol").tail(1)
        return latest
