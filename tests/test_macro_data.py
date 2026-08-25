"""Unit tests for structured macro snapshots."""

import pytest

from macro_data import FredMacroData, parse_fred_response


def test_fred_parser_ignores_missing_values_and_computes_change():
    observation = parse_fred_response(
        "UNRATE",
        {
            "observations": [
                {"date": "2026-08-01", "value": "."},
                {"date": "2026-07-01", "value": "4.2"},
                {"date": "2026-06-01", "value": "4.1"},
            ]
        },
    )
    assert observation is not None
    assert observation.value == 4.2
    assert observation.previous_value == 4.1
    assert observation.change == pytest.approx(0.1)


def test_fred_without_key_is_unavailable():
    snapshot = FredMacroData(api_key="").fetch_snapshot(["CPIAUCSL"])
    assert snapshot.available is False
    assert snapshot.observations == {}
