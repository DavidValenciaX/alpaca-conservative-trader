"""
strategy.py — Mean-reversion signal generation.

Computes technical indicators on bar data and evaluates BUY/SELL/HOLD
signals for each tracked asset using configurable parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import AppConfig
from logger import get_logger

log = get_logger()


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    """Result of evaluating one asset at a point in time."""

    symbol: str
    signal: Signal
    price: float
    reason: str = ""
    indicators: Dict[str, float] = field(default_factory=dict)

    def to_log(self) -> str:
        indicators_str = ", ".join(
            f"{k}={v:.4f}" for k, v in sorted(self.indicators.items())
        )
        return (
            f"[{self.signal.value}] {self.symbol} @ {self.price:.4f} | "
            f"{indicators_str} | {self.reason}"
        )


class MeanReversionStrategy:
    """Conservative mean-reversion strategy using SMA, RSI, and Bollinger Bands."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config.strategy

    @staticmethod
    def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
        """Compute RSI using the Wilder smoothing method."""
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add technical indicator columns to a OHLCV DataFrame.

        Uses pure pandas/numpy — no external TA library needed.

        Adds:
          - sma_short, sma_long
          - rsi
          - bb_lower, bb_middle, bb_upper
        """
        if df.empty:
            return df

        df = df.copy()
        close = df["close"]

        # SMAs
        df["sma_short"] = close.rolling(window=self._cfg.sma_short).mean()
        df["sma_long"] = close.rolling(window=self._cfg.sma_long).mean()

        # RSI
        df["rsi"] = self._compute_rsi(close, self._cfg.rsi_period)

        # Bollinger Bands
        bb_middle = close.rolling(window=self._cfg.bb_period).mean()
        bb_std = close.rolling(window=self._cfg.bb_period).std(ddof=0)
        df["bb_middle"] = bb_middle
        df["bb_upper"] = bb_middle + (bb_std * self._cfg.bb_std_dev)
        df["bb_lower"] = bb_middle - (bb_std * self._cfg.bb_std_dev)

        return df

    def evaluate(
        self,
        symbol: str,
        indicators_row: pd.Series,
        has_position: bool,
    ) -> SignalResult:
        """
        Evaluate a single asset row and return a BUY, SELL, or HOLD signal.

        BUY — ALL conditions must be true:
          1. Price below lower Bollinger Band
          2. RSI < oversold threshold
          3. Price above 50-period SMA (macro uptrend intact)
          4. No open position in this asset

        SELL — ANY of these conditions:
          1. Price crosses above middle Bollinger Band
          2. RSI > overbought threshold
          3. Stop-loss hit (handled externally via bracket orders)
          4. Take-profit hit (handled externally via bracket orders)
        """
        price = indicators_row.get("close", 0.0)
        bb_lower = indicators_row.get("bb_lower", None)
        bb_middle = indicators_row.get("bb_middle", None)
        rsi = indicators_row.get("rsi", None)
        sma_long = indicators_row.get("sma_long", None)

        indicators = {
            "close": price,
            "sma_short": indicators_row.get("sma_short", 0.0),
            "sma_long": sma_long if pd.notna(sma_long) else 0.0,
            "rsi": rsi if pd.notna(rsi) else 0.0,
            "bb_lower": bb_lower if pd.notna(bb_lower) else 0.0,
            "bb_middle": bb_middle if pd.notna(bb_middle) else 0.0,
            "bb_upper": indicators_row.get("bb_upper", 0.0),
        }

        # Check that all required indicators are available
        if any(
            v is None or pd.isna(v)
            for v in [bb_lower, bb_middle, rsi, sma_long]
        ):
            return SignalResult(
                symbol=symbol,
                signal=Signal.HOLD,
                price=price,
                reason="Insufficient indicator data (waiting for enough bars)",
                indicators=indicators,
            )

        price = float(price)
        bb_lower = float(bb_lower)
        bb_middle = float(bb_middle)
        rsi = float(rsi)
        sma_long = float(sma_long)

        # SELL check (exit) — exit conditions use any-match logic
        if has_position:
            if rsi > self._cfg.rsi_overbought:
                return SignalResult(
                    symbol=symbol,
                    signal=Signal.SELL,
                    price=price,
                    reason=f"RSI overbought ({rsi:.2f} > {self._cfg.rsi_overbought})",
                    indicators=indicators,
                )
            if price > bb_middle:
                return SignalResult(
                    symbol=symbol,
                    signal=Signal.SELL,
                    price=price,
                    reason="Price crossed above middle Bollinger Band (mean reversion complete)",
                    indicators=indicators,
                )

        # BUY check — all conditions must be true and no position
        if not has_position:
            buy_reasons = []
            if price >= bb_lower:
                buy_reasons.append(
                    f"Price {price:.4f} not below lower BB {bb_lower:.4f}"
                )
            if rsi >= self._cfg.rsi_oversold:
                buy_reasons.append(
                    f"RSI {rsi:.2f} not oversold (< {self._cfg.rsi_oversold})"
                )
            if price <= sma_long:
                buy_reasons.append(
                    f"Price {price:.4f} below SMA({self._cfg.sma_long}) {sma_long:.4f} (macro downtrend)"
                )

            if not buy_reasons:
                return SignalResult(
                    symbol=symbol,
                    signal=Signal.BUY,
                    price=price,
                    reason=(
                        f"Mean reversion setup: price below BB, RSI oversold "
                        f"({rsi:.2f}), above SMA({self._cfg.sma_long})"
                    ),
                    indicators=indicators,
                )

        hold_reason = "No actionable signal"
        if not has_position and buy_reasons:
            hold_reason = "BUY blocked: " + "; ".join(buy_reasons)

        return SignalResult(
            symbol=symbol,
            signal=Signal.HOLD,
            price=price,
            reason=hold_reason,
            indicators=indicators,
        )
