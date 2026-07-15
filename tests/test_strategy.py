"""Unit tests for scored and strict mean-reversion signals."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from config import StrategyConfig
from strategy import MeanReversionStrategy, Signal


def make_strategy(**overrides) -> MeanReversionStrategy:
    cfg = StrategyConfig()
    cfg.buy_signal_mode = "score"
    cfg.buy_min_score = 5
    cfg.rsi_oversold = 40.0
    cfg.rsi_near_oversold_margin = 10.0
    cfg.rsi_overbought = 70.0
    cfg.require_uptrend = True
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return MeanReversionStrategy(SimpleNamespace(strategy=cfg))


def row(
    *,
    close=100.0,
    bb_lower=95.0,
    bb_middle=100.0,
    rsi=50.0,
    sma_long=99.0,
):
    return pd.Series(
        {
            "close": close,
            "sma_short": 100.0,
            "sma_long": sma_long,
            "rsi": rsi,
            "bb_lower": bb_lower,
            "bb_middle": bb_middle,
            "bb_upper": 105.0,
        }
    )


def test_score_mode_accepts_strong_rsi_pullback_in_uptrend():
    strategy = make_strategy()

    result = strategy.evaluate(
        "SPY",
        row(close=100.0, bb_lower=90.0, bb_middle=95.0, rsi=35.0, sma_long=99.0),
        has_position=False,
    )

    assert result.signal == Signal.BUY
    assert result.indicators["buy_score"] == 5.0


def test_score_mode_accepts_lower_band_pullback_in_uptrend():
    strategy = make_strategy()

    result = strategy.evaluate(
        "SPY",
        row(close=94.0, bb_lower=95.0, bb_middle=100.0, rsi=55.0, sma_long=93.0),
        has_position=False,
    )

    assert result.signal == Signal.BUY
    assert result.indicators["buy_score"] == 5.0


def test_score_mode_keeps_macro_downtrend_block_by_default():
    strategy = make_strategy()

    result = strategy.evaluate(
        "SPY",
        row(close=90.0, bb_lower=95.0, bb_middle=100.0, rsi=30.0, sma_long=99.0),
        has_position=False,
    )

    assert result.signal == Signal.HOLD
    assert "below SMA" in result.reason


def test_strict_mode_preserves_original_all_conditions_rule():
    strategy = make_strategy(buy_signal_mode="strict")

    result = strategy.evaluate(
        "SPY",
        row(close=100.0, bb_lower=90.0, bb_middle=95.0, rsi=35.0, sma_long=99.0),
        has_position=False,
    )

    assert result.signal == Signal.HOLD
    assert "not below lower BB" in result.reason


def test_open_position_exits_after_mean_reversion():
    strategy = make_strategy()

    result = strategy.evaluate(
        "SPY",
        row(close=101.0, bb_lower=95.0, bb_middle=100.0, rsi=55.0, sma_long=99.0),
        has_position=True,
    )

    assert result.signal == Signal.SELL
