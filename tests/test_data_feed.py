"""
Unit tests for DataFeed timeframe parsing.

These tests lock in compatibility with alpaca-py's TimeFrame constructor API,
which expects TimeFrame(amount, unit) instead of arithmetic on helpers like
TimeFrame.Minute.
"""

from __future__ import annotations

from alpaca.data.timeframe import TimeFrameUnit

from data_feed import DataFeed


def test_resolve_timeframe_minute_interval():
    timeframe = DataFeed._resolve_timeframe("15Min")
    assert timeframe.amount == 15
    assert timeframe.unit == TimeFrameUnit.Minute
    assert str(timeframe) == "15Min"


def test_resolve_timeframe_hour_interval():
    timeframe = DataFeed._resolve_timeframe("1Hour")
    assert timeframe.amount == 1
    assert timeframe.unit == TimeFrameUnit.Hour
    assert str(timeframe) == "1Hour"


def test_resolve_timeframe_unknown_falls_back_to_15min():
    timeframe = DataFeed._resolve_timeframe("weird")
    assert timeframe.amount == 15
    assert timeframe.unit == TimeFrameUnit.Minute
    assert str(timeframe) == "15Min"
