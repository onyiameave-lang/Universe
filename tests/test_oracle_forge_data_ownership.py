from Oracle.agents.oracle_agent import OracleAgent


class _Evolution:
    def champion_info(self, symbol, regime=None):
        if regime is None:
            return {"regime": "trending_up"}
        return None

    def champion(self, symbol, regime=None):
        return None


class _Lab:
    def champion_info(self, symbol, regime=None):
        if regime == "trending_up":
            return {"genome": {"genome_id": "incumbent-1", "modules": {"trend": {}}}}
        return None


class _Forge:
    def __init__(self):
        self.request = None

    def act(self, task, context):
        assert task == "oracle.improvement.request"
        self.request = context["request"]
        return {"status": "accepted"}


class _Confidence:
    def get(self, symbol, regime):
        return {}


def test_forge_owned_research_does_not_fetch_oracle_market_data(monkeypatch):
    agent = OracleAgent.__new__(OracleAgent)
    agent.forge = _Forge()
    agent.evolution = _Evolution()
    agent.lab = _Lab()
    agent._champion_confidence = _Confidence()
    agent._research_bundle_cache = {}
    agent.risk = type("Risk", (), {"max_drawdown": 0.2})()

    def unexpected_oracle_fetch(_symbol):
        raise AssertionError("Oracle must not fetch market data for a Forge-owned review")

    monkeypatch.setattr(agent, "_get_ctrader_research_bundle", unexpected_oracle_fetch)

    result = agent.request_forge_improvement("EURUSD", research_data_owner="forge")

    assert result == {"status": "accepted"}
    request = agent.forge.request
    assert request["baseline_genome"]["genome_id"] == "incumbent-1"
    assert request["regime"] == "trending_up"
    assert "bars" not in request
    assert "bars_by_timeframe" not in request
