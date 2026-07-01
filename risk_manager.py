"""
risk_manager.py — Enforces all risk rules BEFORE any order is placed.

This is the gatekeeper. Every order must pass through here. If any rule is
violated, the order is BLOCKED and the reason is logged.

Risk rules:
  1. Max position size: 5% of portfolio per asset
  2. Max total exposure: 20% across all open positions
  3. Stop-loss: always placed as bracket 1.5% below entry
  4. Take-profit: always placed as bracket 2.5% above entry
  5. Max daily loss: 3% in one day → halt all trading
  6. Max consecutive losses: 3 → 2-hour cooldown
  7. No margin: cash account only
  8. Buying power check before order
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from config import AppConfig
from logger import get_logger

log = get_logger()
ET = ZoneInfo("America/New_York")


class RiskBlock(Exception):
    """Raised when a risk rule blocks an order."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"[{rule}] {detail}")


@dataclass
class TrackedTrade:
    """Local representation of a trade that should later produce a realized outcome."""

    symbol: str
    qty: float
    entry_price: float
    submitted_at: datetime
    confirmed: bool = False


class RiskManager:
    """Evaluates and enforces all risk limits before order execution."""

    # Cap on how many processed exit order ids we persist, newest kept.
    _MAX_PERSISTED_EXIT_IDS = 500

    def __init__(self, config: AppConfig, state_file: Optional[Path] = None) -> None:
        self._cfg = config.risk
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None
        self._daily_start_value: Optional[float] = None
        self._daily_loss_halted: bool = False
        self._last_daily_check_date: Optional[str] = None
        self._tracked_trades: Dict[str, TrackedTrade] = {}
        # Ordered for stable persistence (insertion order == recency).
        self._processed_exit_order_ids: Dict[str, None] = {}
        self._state_file = (
            Path(state_file)
            if state_file is not None
            else Path(__file__).resolve().parent / "logs" / "risk_state.json"
        )
        self._load_state()

    def _load_state(self) -> None:
        """Restore persisted risk state so restarts do not silently reset limits."""
        try:
            if not self._state_file.exists():
                return

            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._consecutive_losses = int(data.get("consecutive_losses", 0))
            cooldown_until = data.get("cooldown_until")
            if cooldown_until:
                self._cooldown_until = datetime.fromisoformat(cooldown_until)
            self._daily_start_value = data.get("daily_start_value")
            self._daily_loss_halted = bool(data.get("daily_loss_halted", False))
            self._last_daily_check_date = data.get("last_daily_check_date")
            processed_ids = data.get("processed_exit_order_ids", [])
            if isinstance(processed_ids, list):
                self._processed_exit_order_ids = {
                    str(oid): None for oid in processed_ids
                }
        except Exception as e:
            log.warning(f"Failed to load persisted risk state: {e}")

    def _save_state(self) -> None:
        """Persist the minimum state needed for daily/cooldown risk continuity."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "consecutive_losses": self._consecutive_losses,
                "cooldown_until": (
                    self._cooldown_until.isoformat() if self._cooldown_until else None
                ),
                "daily_start_value": self._daily_start_value,
                "daily_loss_halted": self._daily_loss_halted,
                "last_daily_check_date": self._last_daily_check_date,
                "processed_exit_order_ids": list(
                    self._processed_exit_order_ids.keys()
                ),
            }
            self._state_file.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"Failed to persist risk state: {e}")

    @staticmethod
    def _trading_day_str(at: Optional[datetime] = None) -> str:
        now = at.astimezone(ET) if at else datetime.now(ET)
        return now.strftime("%Y-%m-%d")

    def _mark_exit_processed(self, order_id: str) -> None:
        """Record an exit order id as processed, keeping only the newest ones."""
        self._processed_exit_order_ids.pop(order_id, None)
        self._processed_exit_order_ids[order_id] = None
        while len(self._processed_exit_order_ids) > self._MAX_PERSISTED_EXIT_IDS:
            oldest = next(iter(self._processed_exit_order_ids))
            self._processed_exit_order_ids.pop(oldest, None)

    # ── Trade outcome tracking ──────────────────────────────────────────

    def record_losing_trade(self) -> None:
        """Increment consecutive loss counter and set cooldown if threshold hit."""
        self._consecutive_losses += 1
        log.warning(f"Consecutive losses: {self._consecutive_losses}")

        if self._consecutive_losses >= self._cfg.max_consecutive_losses:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(
                minutes=self._cfg.consecutive_loss_cooldown_minutes
            )
            log.warning(
                f"Max consecutive losses ({self._cfg.max_consecutive_losses}) "
                f"reached. Trading paused until {self._cooldown_until}. "
                f"Cooldown: {self._cfg.consecutive_loss_cooldown_minutes} min"
            )
        self._save_state()

    def record_winning_trade(self) -> None:
        """Reset consecutive loss counter on a win."""
        if self._consecutive_losses > 0:
            log.info(f"Winning trade — resetting consecutive loss counter (was {self._consecutive_losses})")
        self._consecutive_losses = 0
        self._save_state()

    def reset_consecutive_losses(self) -> None:
        """Manually reset the consecutive loss counter."""
        self._consecutive_losses = 0
        self._save_state()

    def sync_existing_positions(self, existing_positions: Dict[str, Dict]) -> None:
        """
        Bootstrap or refresh tracked trades from live positions.

        This keeps risk accounting working across bot restarts and after entry fills.
        """
        now = datetime.now(timezone.utc)
        for symbol, position in existing_positions.items():
            tracked = self._tracked_trades.get(symbol)
            if tracked:
                tracked.qty = float(position.get("qty", tracked.qty))
                tracked.entry_price = float(position.get("avg_entry", tracked.entry_price))
                tracked.confirmed = True
                continue

            self._tracked_trades[symbol] = TrackedTrade(
                symbol=symbol,
                qty=float(position.get("qty", 0.0)),
                entry_price=float(position.get("avg_entry", 0.0)),
                submitted_at=now,
                confirmed=True,
            )

        # Discard stale submitted entries that never became real positions.
        stale_symbols = [
            symbol
            for symbol, trade in self._tracked_trades.items()
            if symbol not in existing_positions
            and not trade.confirmed
            and (now - trade.submitted_at) > timedelta(minutes=30)
        ]
        for symbol in stale_symbols:
            log.warning(f"Dropping stale unfilled tracked entry for {symbol}")
            self._tracked_trades.pop(symbol, None)

    def track_submitted_entry(self, symbol: str, qty: float, entry_price: float) -> None:
        """Track a newly submitted entry so later exit fills can be classified."""
        self._tracked_trades[symbol] = TrackedTrade(
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            submitted_at=datetime.now(timezone.utc),
            confirmed=False,
        )

    def record_filled_exit(
        self,
        order_id: str,
        symbol: str,
        exit_price: float,
        filled_qty: float,
        filled_at: datetime,
        source: str = "",
    ) -> bool:
        """
        Update risk state from an actual filled exit order reported by Alpaca.

        Returns True when a tracked trade outcome was recorded.
        """
        if not order_id or order_id in self._processed_exit_order_ids:
            return False

        trade = self._tracked_trades.get(symbol)
        if not trade:
            self._mark_exit_processed(order_id)
            self._save_state()
            return False

        if filled_at < trade.submitted_at:
            self._mark_exit_processed(order_id)
            self._save_state()
            return False

        self._mark_exit_processed(order_id)
        pnl_pct = 0.0
        if trade.entry_price > 0:
            pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price) * 100

        log.info(
            f"Reconciled exit for {symbol}: entry=${trade.entry_price:.4f}, "
            f"exit=${exit_price:.4f}, qty={filled_qty:.4f}, "
            f"P&L={pnl_pct:.2f}%"
            + (f" via {source}" if source else "")
        )

        if pnl_pct < 0:
            self.record_losing_trade()
        else:
            self.record_winning_trade()

        self._tracked_trades.pop(symbol, None)
        return True

    # ── Daily loss check ───────────────────────────────────────────────

    def check_daily_loss(
        self,
        current_portfolio_value: float,
        day_open_value: Optional[float] = None,
    ) -> None:
        """
        Track daily P&L. If the portfolio drops more than max_daily_loss_pct
        from the start-of-day value, halt all trading for the rest of the day.

        ``day_open_value`` should be the equity as of the previous trading
        day's close (Alpaca ``last_equity``). Using it makes the daily-loss
        baseline independent of when the bot happened to start — a midday
        launch no longer resets the reference to the current (already lower)
        value. Falls back to ``current_portfolio_value`` when unavailable.
        """
        today_str = self._trading_day_str()

        # Reset the daily baseline once per trading day and persist it so a
        # midday restart does not silently erase accumulated losses.
        if self._last_daily_check_date != today_str:
            baseline = (
                day_open_value
                if day_open_value and day_open_value > 0
                else current_portfolio_value
            )
            self._daily_start_value = baseline
            self._daily_loss_halted = False
            self._last_daily_check_date = today_str
            log.info(f"Daily P&L tracking reset. Baseline value: ${baseline:.2f}")
            self._save_state()

        if self._daily_loss_halted:
            return

        if self._daily_start_value and self._daily_start_value > 0:
            loss_pct = (
                (self._daily_start_value - current_portfolio_value)
                / self._daily_start_value
            ) * 100

            if loss_pct >= self._cfg.max_daily_loss_pct:
                self._daily_loss_halted = True
                log.warning(
                    f"MAX DAILY LOSS HIT: portfolio down {loss_pct:.2f}% "
                    f"(limit: {self._cfg.max_daily_loss_pct}%). "
                    f"All trading halted for the remainder of {today_str}."
                )
                self._save_state()

    # ── Pre-order validation ──────────────────────────────────────────

    def validate_order(
        self,
        symbol: str,
        quantity: float,
        estimated_price: float,
        portfolio_value: float,
        existing_positions: Dict[str, Dict],
        buying_power: float,
    ) -> None:
        """
        Validate a proposed order against ALL risk rules.

        Raises RiskBlock with the specific rule name and detail if any rule
        is violated. If no exception is raised, the order is allowed.
        """
        # Rule 1: Max position size per asset
        max_position_value = portfolio_value * (self._cfg.max_position_size_pct / 100.0)
        order_value = quantity * estimated_price

        if order_value > max_position_value:
            raise RiskBlock(
                rule="MAX_POSITION_SIZE",
                detail=(
                    f"Order ${order_value:.2f} exceeds {self._cfg.max_position_size_pct}% "
                    f"of portfolio (${max_position_value:.2f})"
                ),
            )

        # Rule 2: Max total exposure
        current_exposure = sum(
            p.get("market_value", 0.0) for p in existing_positions.values()
        )
        total_exposure = current_exposure + order_value
        max_exposure = portfolio_value * (self._cfg.max_total_exposure_pct / 100.0)

        if total_exposure > max_exposure:
            raise RiskBlock(
                rule="MAX_TOTAL_EXPOSURE",
                detail=(
                    f"Total exposure ${total_exposure:.2f} exceeds "
                    f"{self._cfg.max_total_exposure_pct}% of portfolio "
                    f"(${max_exposure:.2f})"
                ),
            )

        # Rule 3: Daily loss halt
        if self._daily_loss_halted:
            raise RiskBlock(
                rule="MAX_DAILY_LOSS",
                detail=(
                    f"Daily loss limit ({self._cfg.max_daily_loss_pct}%) "
                    f"was reached. Trading halted until next market day."
                ),
            )

        # Rule 4: Consecutive loss cooldown
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            remaining = (self._cooldown_until - datetime.now(timezone.utc)).total_seconds() / 60
            raise RiskBlock(
                rule="CONSECUTIVE_LOSS_COOLDOWN",
                detail=(
                    f"Consecutive loss cooldown active. "
                    f"{remaining:.0f} minutes remaining."
                ),
            )

        # Rule 5: Buying power check
        if order_value > buying_power:
            raise RiskBlock(
                rule="INSUFFICIENT_BUYING_POWER",
                detail=(
                    f"Order requires ${order_value:.2f} but only "
                    f"${buying_power:.2f} buying power available."
                ),
            )

        log.info(
            f"Risk check PASSED for {symbol}: "
            f"qty={quantity:.4f} @ ${estimated_price:.2f} "
            f"(order=${order_value:.2f}, portfolio=${portfolio_value:.2f})"
        )

    def get_bracket_prices(
        self, entry_price: float, side: str
    ) -> tuple[float, float]:
        """
        Return (stop_loss_price, take_profit_price) for a bracket order.

        For BUY (long): stop is below entry, take-profit is above.
        For SELL (short): stop is above entry, take-profit is below.
        """
        if side.upper() == "BUY":
            stop_price = entry_price * (1 - self._cfg.stop_loss_pct / 100.0)
            take_profit_price = entry_price * (1 + self._cfg.take_profit_pct / 100.0)
        else:
            # Short — reverse the bracket
            stop_price = entry_price * (1 + self._cfg.stop_loss_pct / 100.0)
            take_profit_price = entry_price * (1 - self._cfg.take_profit_pct / 100.0)

        return round(stop_price, 4), round(take_profit_price, 4)

    @property
    def can_trade(self) -> bool:
        """Global trading flag — can we place orders right now?"""
        if self._daily_loss_halted:
            return False
        if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
            return False
        return True

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def cooldown_remaining_minutes(self) -> float:
        if not self._cooldown_until:
            return 0.0
        remaining = (self._cooldown_until - datetime.now(timezone.utc)).total_seconds() / 60
        return max(0.0, remaining)
