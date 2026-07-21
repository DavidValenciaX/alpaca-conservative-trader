"""
modes.py — Risk profiles (bot modes) that auto-configure trading parameters.

Each mode bundles a coherent set of risk limits and signal-aggressiveness
settings, so the bot's behavior can be switched with a single name instead
of retuning individual parameters.

Selection:
  - BOT_MODE env var sets the initial mode. "custom" means no profile is
    applied and every parameter comes from its own env var (legacy behavior).
  - BOT_MODE_FILE (e.g. mode.txt) is re-read every trading cycle and
    overrides BOT_MODE, allowing hot-switching without a restart.

Modes are applied in-place on the shared AppConfig objects: every component
(strategy, risk manager, executor) holds a live reference to them, so a mode
change takes effect on the next decision without a restart. Already-placed
bracket orders keep their original stop/take-profit legs; the new parameters
only affect new decisions.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict

from logger import get_logger

if TYPE_CHECKING:
    from config import AppConfig

log = get_logger()

# Mode that disables profiles entirely: all parameters come from env vars.
CUSTOM_MODE = "custom"


@dataclass(frozen=True)
class ModeProfile:
    """A named bundle of risk limits and signal-aggressiveness settings."""

    name: str
    description: str

    # Risk limits (RiskConfig)
    max_position_size_pct: float
    max_total_exposure_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    max_daily_loss_pct: float
    max_consecutive_losses: int
    consecutive_loss_cooldown_minutes: int

    # Signal aggressiveness (StrategyConfig thresholds)
    rsi_oversold: float
    rsi_overbought: float
    bb_std_dev: float
    buy_min_score: int
    rsi_near_oversold_margin: float
    require_uptrend: bool


PROFILES: Dict[str, ModeProfile] = {
    "preservacion": ModeProfile(
        name="preservacion",
        description="Capital preservation: minimal exposure, very tight stops, "
        "only the deepest oversold signals inside an uptrend.",
        max_position_size_pct=3.0,
        max_total_exposure_pct=10.0,
        stop_loss_pct=1.0,
        take_profit_pct=1.5,
        max_daily_loss_pct=1.0,
        max_consecutive_losses=2,
        consecutive_loss_cooldown_minutes=240,
        rsi_oversold=35.0,
        rsi_overbought=65.0,
        bb_std_dev=2.0,
        buy_min_score=6,
        rsi_near_oversold_margin=8.0,
        require_uptrend=True,
    ),
    "conservador": ModeProfile(
        name="conservador",
        description="Conservative: small steady gains with low risk. Matches "
        "the bot's original default tuning.",
        max_position_size_pct=7.5,
        max_total_exposure_pct=30.0,
        stop_loss_pct=2.0,
        take_profit_pct=3.5,
        max_daily_loss_pct=3.0,
        max_consecutive_losses=3,
        consecutive_loss_cooldown_minutes=120,
        rsi_oversold=40.0,
        rsi_overbought=70.0,
        bb_std_dev=1.8,
        buy_min_score=5,
        rsi_near_oversold_margin=10.0,
        require_uptrend=True,
    ),
    "balanceado": ModeProfile(
        name="balanceado",
        description="Balanced: even trade-off between risk and return.",
        max_position_size_pct=10.0,
        max_total_exposure_pct=45.0,
        stop_loss_pct=2.5,
        take_profit_pct=4.5,
        max_daily_loss_pct=4.0,
        max_consecutive_losses=3,
        consecutive_loss_cooldown_minutes=90,
        rsi_oversold=42.0,
        rsi_overbought=72.0,
        bb_std_dev=1.8,
        buy_min_score=4,
        rsi_near_oversold_margin=12.0,
        require_uptrend=True,
    ),
    "crecimiento": ModeProfile(
        name="crecimiento",
        description="Growth: prioritizes capital appreciation, tolerates "
        "moderate volatility and trades without requiring an uptrend.",
        max_position_size_pct=12.5,
        max_total_exposure_pct=60.0,
        stop_loss_pct=3.0,
        take_profit_pct=6.0,
        max_daily_loss_pct=5.0,
        max_consecutive_losses=4,
        consecutive_loss_cooldown_minutes=60,
        rsi_oversold=45.0,
        rsi_overbought=75.0,
        bb_std_dev=1.6,
        buy_min_score=4,
        rsi_near_oversold_margin=15.0,
        require_uptrend=False,
    ),
    "agresivo": ModeProfile(
        name="agresivo",
        description="Aggressive: maximizes growth, accepts significant losses "
        "and enters on much weaker pullbacks.",
        max_position_size_pct=15.0,
        max_total_exposure_pct=80.0,
        stop_loss_pct=3.5,
        take_profit_pct=8.0,
        max_daily_loss_pct=7.0,
        max_consecutive_losses=5,
        consecutive_loss_cooldown_minutes=45,
        rsi_oversold=48.0,
        rsi_overbought=78.0,
        bb_std_dev=1.5,
        buy_min_score=3,
        rsi_near_oversold_margin=15.0,
        require_uptrend=False,
    ),
    "especulativo": ModeProfile(
        name="especulativo",
        description="Speculative: maximum risk, full exposure allowed, very "
        "wide stops and loose entry conditions. Expect severe drawdowns.",
        max_position_size_pct=20.0,
        max_total_exposure_pct=100.0,
        stop_loss_pct=5.0,
        take_profit_pct=12.0,
        max_daily_loss_pct=10.0,
        max_consecutive_losses=6,
        consecutive_loss_cooldown_minutes=30,
        rsi_oversold=50.0,
        rsi_overbought=80.0,
        bb_std_dev=1.5,
        buy_min_score=3,
        rsi_near_oversold_margin=20.0,
        require_uptrend=False,
    ),
}

# English aliases → canonical (Spanish) profile names.
ALIASES: Dict[str, str] = {
    "preservation": "preservacion",
    "conservative": "conservador",
    "balanced": "balanceado",
    "moderate": "balanceado",
    "growth": "crecimiento",
    "aggressive": "agresivo",
    "speculative": "especulativo",
}

# Env var → ModeProfile field, used to warn when .env pins a value that the
# active mode overrides.
MANAGED_ENV_VARS: Dict[str, str] = {
    "MAX_POSITION_SIZE_PCT": "max_position_size_pct",
    "MAX_TOTAL_EXPOSURE_PCT": "max_total_exposure_pct",
    "STOP_LOSS_PCT": "stop_loss_pct",
    "TAKE_PROFIT_PCT": "take_profit_pct",
    "MAX_DAILY_LOSS_PCT": "max_daily_loss_pct",
    "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses",
    "CONSECUTIVE_LOSS_COOLDOWN_MINUTES": "consecutive_loss_cooldown_minutes",
    "RSI_OVERSOLD": "rsi_oversold",
    "RSI_OVERBOUGHT": "rsi_overbought",
    "BB_STD_DEV": "bb_std_dev",
    "BUY_MIN_SCORE": "buy_min_score",
    "RSI_NEAR_OVERSOLD_MARGIN": "rsi_near_oversold_margin",
    "REQUIRE_UPTREND": "require_uptrend",
}


def _normalize(name: str) -> str:
    """Lowercase, strip and remove accents so 'Preservación' resolves too."""
    decomposed = unicodedata.normalize("NFKD", name.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def resolve_mode(name: str) -> ModeProfile:
    """Resolve a mode name (or alias) to its profile, raising on unknown names."""
    key = _normalize(name)
    key = ALIASES.get(key, key)
    try:
        return PROFILES[key]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(
            f"Unknown BOT_MODE '{name}'. Valid modes: {valid} "
            f"(or '{CUSTOM_MODE}' to use individual env vars)"
        ) from None


def apply_mode(config: "AppConfig", profile: ModeProfile) -> None:
    """
    Apply a mode profile IN-PLACE on the shared config objects.

    The risk manager, strategy and executor all hold live references to
    config.risk / config.strategy, so mutating fields here propagates to
    every component on its next decision without a restart.
    """
    risk = config.risk
    risk.max_position_size_pct = profile.max_position_size_pct
    risk.max_total_exposure_pct = profile.max_total_exposure_pct
    risk.stop_loss_pct = profile.stop_loss_pct
    risk.take_profit_pct = profile.take_profit_pct
    risk.max_daily_loss_pct = profile.max_daily_loss_pct
    risk.max_consecutive_losses = profile.max_consecutive_losses
    risk.consecutive_loss_cooldown_minutes = (
        profile.consecutive_loss_cooldown_minutes
    )

    strategy = config.strategy
    strategy.rsi_oversold = profile.rsi_oversold
    strategy.rsi_overbought = profile.rsi_overbought
    strategy.bb_std_dev = profile.bb_std_dev
    strategy.buy_min_score = profile.buy_min_score
    strategy.rsi_near_oversold_margin = profile.rsi_near_oversold_margin
    strategy.require_uptrend = profile.require_uptrend

    config.bot_mode = profile.name


def warn_env_overrides(profile: ModeProfile) -> None:
    """Warn when .env explicitly pins a parameter that the active mode overrides."""
    for env_var, field_name in MANAGED_ENV_VARS.items():
        raw = os.getenv(env_var)
        if raw is None:
            continue
        profile_value = getattr(profile, field_name)
        try:
            env_value: object = float(raw)
        except ValueError:
            env_value = raw.strip().lower() in ("true", "1", "yes")
        if env_value != profile_value:
            log.warning(
                f"{env_var}={raw} is set but mode '{profile.name}' overrides "
                f"it → {profile_value}. Unset it or use BOT_MODE=custom."
            )
