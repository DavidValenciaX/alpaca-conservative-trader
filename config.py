"""
config.py — Loads and validates all environment variables and strategy parameters.

All trading parameters are configurable here without touching any other code.
Secrets must be set in a .env file in the project root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Set it in your .env file or export it."
        )
    return value


def _optional_env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _optional_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw else default


def _optional_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw else default


def _optional_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    return default


@dataclass
class AlpacaConfig:
    api_key: str = field(default_factory=lambda: _required_env("ALPACA_API_KEY"))
    secret_key: str = field(default_factory=lambda: _required_env("ALPACA_SECRET_KEY"))
    base_url: str = field(
        default_factory=lambda: _optional_env(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        )
    )
    data_url: str = field(
        default_factory=lambda: _optional_env(
            "ALPACA_DATA_URL", "https://data.alpaca.markets"
        )
    )


@dataclass
class StrategyConfig:
    """All tunable parameters for the mean-reversion strategy."""

    assets: List[str] = field(
        default_factory=lambda: os.getenv("ASSETS", "SPY,QQQ,GLD,IWM").split(",")
    )

    # SMA periods
    sma_short: int = field(default_factory=lambda: _optional_int("SMA_SHORT", 20))
    sma_long: int = field(default_factory=lambda: _optional_int("SMA_LONG", 50))

    # RSI
    rsi_period: int = field(default_factory=lambda: _optional_int("RSI_PERIOD", 14))
    rsi_oversold: float = field(
        default_factory=lambda: _optional_float("RSI_OVERSOLD", 35.0)
    )
    rsi_overbought: float = field(
        default_factory=lambda: _optional_float("RSI_OVERBOUGHT", 65.0)
    )

    # Bollinger Bands
    bb_period: int = field(default_factory=lambda: _optional_int("BB_PERIOD", 20))
    bb_std_dev: float = field(
        default_factory=lambda: _optional_float("BB_STD_DEV", 2.0)
    )

    # Bar timeframe
    bar_timeframe: str = field(
        default_factory=lambda: _optional_env("BAR_TIMEFRAME", "15Min")
    )

    # Signal check interval in minutes
    check_interval_minutes: int = field(
        default_factory=lambda: _optional_int("CHECK_INTERVAL_MINUTES", 15)
    )

    # Entry execution: use a limit order (capping slippage) instead of a market
    # order. The limit is placed at the signal price plus a small buffer so
    # minor upticks still fill without chasing the price.
    use_limit_entry: bool = field(
        default_factory=lambda: _optional_bool("USE_LIMIT_ENTRY", True)
    )
    entry_limit_buffer_pct: float = field(
        default_factory=lambda: _optional_float("ENTRY_LIMIT_BUFFER_PCT", 0.1)
    )


@dataclass
class RiskConfig:
    """Hard limits enforced by risk_manager before any order."""

    max_position_size_pct: float = field(
        default_factory=lambda: _optional_float("MAX_POSITION_SIZE_PCT", 5.0)
    )
    max_total_exposure_pct: float = field(
        default_factory=lambda: _optional_float("MAX_TOTAL_EXPOSURE_PCT", 20.0)
    )
    stop_loss_pct: float = field(
        default_factory=lambda: _optional_float("STOP_LOSS_PCT", 1.5)
    )
    take_profit_pct: float = field(
        default_factory=lambda: _optional_float("TAKE_PROFIT_PCT", 2.5)
    )
    max_daily_loss_pct: float = field(
        default_factory=lambda: _optional_float("MAX_DAILY_LOSS_PCT", 3.0)
    )
    max_consecutive_losses: int = field(
        default_factory=lambda: _optional_int("MAX_CONSECUTIVE_LOSSES", 3)
    )
    consecutive_loss_cooldown_minutes: int = field(
        default_factory=lambda: _optional_int("CONSECUTIVE_LOSS_COOLDOWN_MINUTES", 120)
    )


@dataclass
class AppConfig:
    """Top-level configuration container."""

    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    paper_mode: bool = field(
        default_factory=lambda: _optional_bool("PAPER_MODE", True)
    )
    log_level: str = field(
        default_factory=lambda: _optional_env("LOG_LEVEL", "DEBUG").upper()
    )

    def validate(self) -> None:
        """Validate all configuration values, raising on invalid combos."""
        if self.risk.max_position_size_pct <= 0:
            raise ValueError("MAX_POSITION_SIZE_PCT must be > 0")
        if self.risk.max_total_exposure_pct <= 0:
            raise ValueError("MAX_TOTAL_EXPOSURE_PCT must be > 0")
        if self.risk.stop_loss_pct <= 0:
            raise ValueError("STOP_LOSS_PCT must be > 0")
        if self.risk.take_profit_pct <= 0:
            raise ValueError("TAKE_PROFIT_PCT must be > 0")
        if self.risk.max_daily_loss_pct <= 0:
            raise ValueError("MAX_DAILY_LOSS_PCT must be > 0")
        if self.risk.stop_loss_pct >= self.risk.take_profit_pct:
            raise ValueError(
                "STOP_LOSS_PCT must be less than TAKE_PROFIT_PCT "
                "(stop should be tighter than target)"
            )
        if self.strategy.sma_short >= self.strategy.sma_long:
            raise ValueError("SMA_SHORT must be less than SMA_LONG")
        if self.strategy.check_interval_minutes < 1:
            raise ValueError("CHECK_INTERVAL_MINUTES must be >= 1")
        if self.strategy.entry_limit_buffer_pct < 0:
            raise ValueError("ENTRY_LIMIT_BUFFER_PCT must be >= 0")
        if not self.paper_mode:
            import warnings

            warnings.warn(
                "LIVE MODE ENABLED — PAPER_MODE=False. "
                "Real orders will be placed. Ensure you understand the risks.",
                RuntimeWarning,
            )


def load_config() -> AppConfig:
    """Build and validate the application configuration."""
    cfg = AppConfig()
    cfg.validate()
    return cfg
