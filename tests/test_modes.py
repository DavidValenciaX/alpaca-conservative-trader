"""
Unit tests for bot modes: profile integrity, name resolution and the
in-place application that lets every component see a mode change through
its already-captured config reference.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import RiskConfig, StrategyConfig
from modes import PROFILES, apply_mode, resolve_mode

MODE_ORDER = [
    "preservacion",
    "conservador",
    "balanceado",
    "crecimiento",
    "agresivo",
    "especulativo",
]


def _config_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        risk=RiskConfig(),
        strategy=StrategyConfig(),
        bot_mode="custom",
    )


def test_profiles_cover_the_six_risk_levels():
    assert set(PROFILES) == set(MODE_ORDER)


def test_all_profiles_are_internally_valid():
    for profile in PROFILES.values():
        assert 0 < profile.stop_loss_pct < profile.take_profit_pct
        assert 1 <= profile.buy_min_score <= 8
        assert profile.max_position_size_pct <= profile.max_total_exposure_pct
        assert profile.max_total_exposure_pct <= 100.0
        assert profile.max_consecutive_losses >= 1
        assert profile.consecutive_loss_cooldown_minutes >= 1
        assert profile.rsi_oversold < profile.rsi_overbought


def test_risk_grows_monotonically_across_levels():
    exposures = [PROFILES[name].max_total_exposure_pct for name in MODE_ORDER]
    assert exposures == sorted(exposures)


def test_conservador_matches_original_default_tuning():
    conservador = PROFILES["conservador"]
    assert conservador.max_position_size_pct == RiskConfig().max_position_size_pct
    assert conservador.max_total_exposure_pct == RiskConfig().max_total_exposure_pct
    assert conservador.stop_loss_pct == RiskConfig().stop_loss_pct
    assert conservador.take_profit_pct == RiskConfig().take_profit_pct
    assert conservador.rsi_oversold == StrategyConfig().rsi_oversold
    assert conservador.buy_min_score == StrategyConfig().buy_min_score


def test_resolve_mode_is_case_and_accent_insensitive():
    assert resolve_mode("Conservador").name == "conservador"
    assert resolve_mode("PRESERVACIÓN").name == "preservacion"
    assert resolve_mode(" agresivo ").name == "agresivo"


def test_resolve_mode_accepts_english_aliases():
    assert resolve_mode("preservation").name == "preservacion"
    assert resolve_mode("conservative").name == "conservador"
    assert resolve_mode("balanced").name == "balanceado"
    assert resolve_mode("growth").name == "crecimiento"
    assert resolve_mode("aggressive").name == "agresivo"
    assert resolve_mode("speculative").name == "especulativo"


def test_resolve_mode_rejects_unknown_names():
    with pytest.raises(ValueError, match="Unknown BOT_MODE"):
        resolve_mode("yolo")


def test_apply_mode_mutates_config_in_place():
    config = _config_namespace()
    risk_ref = config.risk
    strategy_ref = config.strategy

    apply_mode(config, resolve_mode("agresivo"))

    # Same objects: components holding self._cfg references see the change.
    assert config.risk is risk_ref
    assert config.strategy is strategy_ref
    assert risk_ref.stop_loss_pct == 3.5
    assert risk_ref.take_profit_pct == 8.0
    assert risk_ref.max_total_exposure_pct == 80.0
    assert risk_ref.max_consecutive_losses == 5
    assert strategy_ref.rsi_oversold == 48.0
    assert strategy_ref.buy_min_score == 3
    assert strategy_ref.require_uptrend is False
    assert config.bot_mode == "agresivo"


def test_apply_mode_is_visible_through_captured_references():
    config = _config_namespace()
    captured_risk = config.risk  # what RiskManager does at construction

    apply_mode(config, resolve_mode("preservacion"))

    assert captured_risk.max_total_exposure_pct == 10.0
    assert captured_risk.stop_loss_pct == 1.0
