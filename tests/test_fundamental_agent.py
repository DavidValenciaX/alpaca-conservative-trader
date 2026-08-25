"""Strict schema tests for the action-free LLM adapter."""

from datetime import datetime, timezone

import pytest

from fundamental_agent import FundamentalAgent, FundamentalAssessment
from fundamental_types import FundamentalAgentError
from macro_data import MacroSnapshot


def valid_payload():
    view = {
        "stance": "neutral",
        "confidence": 0.4,
        "event_risk": "low",
        "horizon": "swing",
        "reasons": ["No decisive evidence"],
        "sources": ["item-1"],
    }
    return {
        "regime": "neutral",
        "asset_views": {symbol: view for symbol in ("SPY", "QQQ")},
        "source_ids": ["item-1"],
    }


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, system_prompt, user_prompt):
        self.prompts.append((system_prompt, user_prompt))
        return self.response


def make_agent(response):
    return FundamentalAgent(FakeClient(response), ("SPY", "QQQ"), model="test-model")


def test_agent_accepts_valid_json_and_includes_context():
    agent = make_agent(__import__("json").dumps(valid_payload()))
    assessment = agent.analyze(
        news=[],
        macro=MacroSnapshot({}, datetime.now(timezone.utc), available=False),
        technical_context={"SPY": {"signal": "BUY"}},
    )
    assert isinstance(assessment, FundamentalAssessment)
    assert assessment.asset_views["SPY"].stance == "neutral"
    assert "untrusted" in agent._client.prompts[0][1]


@pytest.mark.parametrize(
    "response",
    ["not json", '{"regime":"neutral","asset_views":{}}'],
)
def test_agent_rejects_invalid_or_incomplete_json(response):
    with pytest.raises(FundamentalAgentError):
        make_agent(response).analyze(
            [], MacroSnapshot({}, datetime.now(timezone.utc), False), {}
        )


def test_agent_rejects_confidence_out_of_range():
    payload = valid_payload()
    payload["asset_views"]["SPY"]["confidence"] = 1.1
    with pytest.raises(FundamentalAgentError):
        make_agent(__import__("json").dumps(payload)).analyze(
            [], MacroSnapshot({}, datetime.now(timezone.utc), False), {}
        )
