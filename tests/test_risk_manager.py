"""
Unit tests for RiskManager: risk gating, daily loss halt, cooldowns,
state persistence, and filled-exit reconciliation.

These tests avoid any Alpaca client by only exercising RiskManager, whose
constructor only reads ``config.risk``. A lightweight namespace stands in for
AppConfig, and a temp file is injected for persisted state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from config import RiskConfig
from risk_manager import RiskBlock, RiskManager


def make_config(**overrides) -> SimpleNamespace:
    risk = RiskConfig()
    for key, value in overrides.items():
        setattr(risk, key, value)
    return SimpleNamespace(risk=risk)


def make_rm(tmp_path, **overrides) -> RiskManager:
    return RiskManager(make_config(**overrides), state_file=tmp_path / "risk_state.json")


# ── Consecutive losses / cooldown ─────────────────────────────────────────


def test_consecutive_losses_trigger_cooldown(tmp_path):
    rm = make_rm(tmp_path, max_consecutive_losses=3)
    assert rm.can_trade

    rm.record_losing_trade()
    rm.record_losing_trade()
    assert rm.can_trade
    assert rm.consecutive_losses == 2

    rm.record_losing_trade()
    assert rm.consecutive_losses == 3
    assert not rm.can_trade
    assert rm.cooldown_remaining_minutes > 0


def test_winning_trade_resets_counter(tmp_path):
    rm = make_rm(tmp_path)
    rm.record_losing_trade()
    rm.record_losing_trade()
    rm.record_winning_trade()
    assert rm.consecutive_losses == 0


# ── Daily loss halt ───────────────────────────────────────────────────────


def test_daily_loss_halts_trading(tmp_path):
    rm = make_rm(tmp_path, max_daily_loss_pct=3.0)
    rm.check_daily_loss(current_portfolio_value=10_000.0)
    assert rm.can_trade

    # 4% drop exceeds the 3% limit → halt.
    rm.check_daily_loss(current_portfolio_value=9_600.0)
    assert not rm.can_trade


def test_daily_loss_within_limit_keeps_trading(tmp_path):
    rm = make_rm(tmp_path, max_daily_loss_pct=3.0)
    rm.check_daily_loss(current_portfolio_value=10_000.0)
    rm.check_daily_loss(current_portfolio_value=9_800.0)  # 2% drop
    assert rm.can_trade


def test_daily_baseline_uses_prev_close_not_start_value(tmp_path):
    # Bot starts midday already down 4% from the previous close. Using the
    # previous close as the baseline must trigger the halt immediately.
    rm = make_rm(tmp_path, max_daily_loss_pct=3.0)
    rm.check_daily_loss(current_portfolio_value=9_600.0, day_open_value=10_000.0)
    assert not rm.can_trade


def test_daily_baseline_falls_back_to_current_when_no_open_value(tmp_path):
    rm = make_rm(tmp_path, max_daily_loss_pct=3.0)
    rm.check_daily_loss(current_portfolio_value=9_600.0, day_open_value=0.0)
    # With no prior-close reference, baseline == current → no immediate halt.
    assert rm.can_trade


# ── State persistence across restarts ─────────────────────────────────────


def test_state_persists_across_restart(tmp_path):
    rm = make_rm(tmp_path, max_consecutive_losses=5)
    rm.record_losing_trade()
    rm.record_losing_trade()

    rm2 = make_rm(tmp_path, max_consecutive_losses=5)
    assert rm2.consecutive_losses == 2


def test_daily_halt_persists_across_restart(tmp_path):
    rm = make_rm(tmp_path, max_daily_loss_pct=3.0)
    rm.check_daily_loss(current_portfolio_value=10_000.0)
    rm.check_daily_loss(current_portfolio_value=9_500.0)  # 5% drop → halt
    assert not rm.can_trade

    rm2 = make_rm(tmp_path, max_daily_loss_pct=3.0)
    assert not rm2.can_trade


# ── Filled-exit reconciliation ────────────────────────────────────────────


def test_record_filled_exit_loss_increments_counter(tmp_path):
    rm = make_rm(tmp_path)
    rm.track_submitted_entry("SPY", qty=10, entry_price=100.0)
    filled_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    recorded = rm.record_filled_exit(
        order_id="o1", symbol="SPY", exit_price=98.0,
        filled_qty=10, filled_at=filled_at,
    )
    assert recorded is True
    assert rm.consecutive_losses == 1


def test_record_filled_exit_win_resets_counter(tmp_path):
    rm = make_rm(tmp_path)
    rm.record_losing_trade()
    rm.track_submitted_entry("QQQ", qty=5, entry_price=200.0)
    filled_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    recorded = rm.record_filled_exit(
        order_id="o2", symbol="QQQ", exit_price=205.0,
        filled_qty=5, filled_at=filled_at,
    )
    assert recorded is True
    assert rm.consecutive_losses == 0


def test_record_filled_exit_is_idempotent(tmp_path):
    rm = make_rm(tmp_path)
    rm.track_submitted_entry("SPY", qty=10, entry_price=100.0)
    filled_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    first = rm.record_filled_exit(
        order_id="dup", symbol="SPY", exit_price=98.0,
        filled_qty=10, filled_at=filled_at,
    )
    # Re-track and replay the same order id — must not double-count.
    rm.track_submitted_entry("SPY", qty=10, entry_price=100.0)
    second = rm.record_filled_exit(
        order_id="dup", symbol="SPY", exit_price=98.0,
        filled_qty=10, filled_at=filled_at,
    )
    assert first is True
    assert second is False
    assert rm.consecutive_losses == 1


def test_processed_exit_ids_persist_prevent_double_count(tmp_path):
    rm = make_rm(tmp_path)
    rm.track_submitted_entry("SPY", qty=10, entry_price=100.0)
    filled_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    rm.record_filled_exit(
        order_id="persisted", symbol="SPY", exit_price=98.0,
        filled_qty=10, filled_at=filled_at,
    )

    # Simulate a restart: same state file, position already closed so it gets
    # re-tracked, but the exit id was persisted and must be ignored.
    rm2 = make_rm(tmp_path)
    rm2.track_submitted_entry("SPY", qty=10, entry_price=100.0)
    replayed = rm2.record_filled_exit(
        order_id="persisted", symbol="SPY", exit_price=98.0,
        filled_qty=10, filled_at=filled_at,
    )
    assert replayed is False


def test_exit_before_entry_is_ignored(tmp_path):
    rm = make_rm(tmp_path)
    rm.track_submitted_entry("SPY", qty=10, entry_price=100.0)
    stale_fill = datetime.now(timezone.utc) - timedelta(hours=1)
    recorded = rm.record_filled_exit(
        order_id="stale", symbol="SPY", exit_price=98.0,
        filled_qty=10, filled_at=stale_fill,
    )
    assert recorded is False
    assert rm.consecutive_losses == 0


# ── Pre-order validation ──────────────────────────────────────────────────


def test_validate_order_blocks_oversized_position(tmp_path):
    rm = make_rm(tmp_path, max_position_size_pct=5.0)
    with pytest.raises(RiskBlock) as exc:
        rm.validate_order(
            symbol="SPY", quantity=100, estimated_price=100.0,
            portfolio_value=10_000.0, existing_positions={}, buying_power=100_000.0,
        )
    assert exc.value.rule == "MAX_POSITION_SIZE"


def test_validate_order_blocks_excess_exposure(tmp_path):
    rm = make_rm(tmp_path, max_position_size_pct=50.0, max_total_exposure_pct=20.0)
    existing = {"QQQ": {"market_value": 1_800.0}}
    with pytest.raises(RiskBlock) as exc:
        rm.validate_order(
            symbol="SPY", quantity=5, estimated_price=100.0,
            portfolio_value=10_000.0, existing_positions=existing, buying_power=100_000.0,
        )
    assert exc.value.rule == "MAX_TOTAL_EXPOSURE"


def test_validate_order_blocks_insufficient_buying_power(tmp_path):
    rm = make_rm(tmp_path, max_position_size_pct=100.0, max_total_exposure_pct=100.0)
    with pytest.raises(RiskBlock) as exc:
        rm.validate_order(
            symbol="SPY", quantity=10, estimated_price=100.0,
            portfolio_value=10_000.0, existing_positions={}, buying_power=500.0,
        )
    assert exc.value.rule == "INSUFFICIENT_BUYING_POWER"


def test_validate_order_passes_within_limits(tmp_path):
    rm = make_rm(tmp_path)
    # Should not raise.
    rm.validate_order(
        symbol="SPY", quantity=4, estimated_price=100.0,
        portfolio_value=10_000.0, existing_positions={}, buying_power=10_000.0,
    )


# ── Bracket price math ────────────────────────────────────────────────────


def test_bracket_prices_long(tmp_path):
    rm = make_rm(tmp_path, stop_loss_pct=1.5, take_profit_pct=2.5)
    stop, take = rm.get_bracket_prices(entry_price=100.0, side="BUY")
    assert stop == pytest.approx(98.5)
    assert take == pytest.approx(102.5)
