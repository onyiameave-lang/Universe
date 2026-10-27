import json
from pathlib import Path

from Oracle.tools import forge_experiment_runner as runner


def _record():
    profile = {
        "profile_status": "api_observed_costs",
        "account_id": 48034617,
        "broker_symbol": "EURUSD",
        "profile_version": "ctrader-cost-v2-test-eurusd",
        "price_basis": "bid",
        "commission_bps": 0.2,
        "slippage_bps": 0.1,
        "spread_bps": 0.08,
        "stress_slippage_bps": 0.2,
        "stress_spread_bps": 0.3,
        "evidence": {
            "symbol_filled_deals": 20,
            "matched_slippage_deals": 20,
            "matched_spread_quotes": 100,
        },
    }
    return {
        "schema": "forge.oracle.cost_profile_record.v1",
        "symbol": "EURUSD",
        "status": "complete",
        "account_id": 48034617,
        "profile": profile,
    }


def _mock_profile_file(monkeypatch, record):
    monkeypatch.setattr(runner, "ROOT", Path("."))
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith("oracle_cost_profiles/EURUSD/latest.json"):
            return json.dumps(record)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_saved_empirical_profile_is_reused_only_when_sample_qualified(monkeypatch):
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "48034617")
    _mock_profile_file(monkeypatch, _record())

    profile = runner._load_saved_empirical_profile("EURUSD", "EURUSD")

    assert profile["profile_version"] == "ctrader-cost-v2-test-eurusd"
    assert profile["evidence"]["matched_spread_quotes"] == 100


def test_saved_empirical_profile_with_insufficient_fills_is_rejected(monkeypatch):
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "48034617")
    record = _record()
    record["profile"]["evidence"]["symbol_filled_deals"] = 19
    _mock_profile_file(monkeypatch, record)

    assert runner._load_saved_empirical_profile("EURUSD", "EURUSD") is None


def test_saved_empirical_profile_from_another_account_is_rejected(monkeypatch):
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "48034617")
    record = _record()
    record["profile"]["account_id"] = 123456
    record["account_id"] = 123456
    _mock_profile_file(monkeypatch, record)

    assert runner._load_saved_empirical_profile("EURUSD", "EURUSD") is None


def test_v1_incumbent_is_forwarded_to_v2_forge_without_market_data(monkeypatch):
    incumbent = {
        "status": "complete", "symbol": "EURUSD", "regime": "trending_up",
        "baseline_genome": {"genome_id": "v1-incumbent", "modules": {"trend": {}}},
        "performance_metrics": {"observed_trades": 12, "wins": 7, "losses": 5},
        "risk_policy": {"source": "Oracle V1 RiskManager.max_drawdown", "max_drawdown_fraction": 0.2},
        "minimum_executed_trades": 100,
    }
    captured = {}
    monkeypatch.setattr(runner, "_v1_catalog_and_incumbent", lambda symbol: incumbent)

    def bridge(task, context):
        captured["task"] = task
        captured["request"] = context["request"]
        return {"status": "complete", "data": {"status": "complete"}}

    monkeypatch.setattr(runner, "_v2_bridge_act", bridge)
    result = runner._request_v1_improvement("EURUSD", "ablation", "volatility", _record()["profile"])

    assert result["status"] == "complete"
    assert captured["task"] == "oracle.improvement.request"
    assert captured["request"]["baseline_genome"]["genome_id"] == "v1-incumbent"
    assert captured["request"]["ablation_targets"] == ["volatility"]
    assert captured["request"]["execution_costs"]["profile_version"] == "ctrader-cost-v2-test-eurusd"
    assert "bars_by_timeframe" not in captured["request"]
