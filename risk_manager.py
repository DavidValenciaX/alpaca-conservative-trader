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

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from config import AppConfig
from logger import get_logger

log = get_logger()


class RiskBlock(Exception):
    """Raised when a risk rule blocks an order."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"[{rule}] {detail}")


class RiskManager:
    """Evaluates and enforces all risk limits before order execution."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config.risk
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None
        self._daily_start_value: Optional[float] = None
        self._daily_loss_halted: bool = False
        self._last_daily_check_date: Optional[str] = None

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

    def record_winning_trade(self) -> None:
        """Reset consecutive loss counter on a win."""
        if self._consecutive_losses > 0:
            log.info(f"Winning trade — resetting consecutive loss counter (was {self._consecutive_losses})")
        self._consecutive_losses = 0

    def reset_consecutive_losses(self) -> None:
        """Manually reset the consecutive loss counter."""
        self._consecutive_losses = 0

    # ── Daily loss check ───────────────────────────────────────────────

    def check_daily_loss(
        self, current_portfolio_value: float, portfolio_start_value: float
    ) -> None:
        """
        Track daily P&L. If the portfolio drops more than max_daily_loss_pct
        from the start-of-day value, halt all trading for the rest of the day.
        """
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Reset daily tracking at start of new day
        if self._last_daily_check_date != today_str:
            self._daily_start_value = portfolio_start_value
            self._daily_loss_halted = False
            self._last_daily_check_date = today_str
            log.info(f"Daily P&L tracking reset. Start value: ${portfolio_start_value:.2f}")

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
