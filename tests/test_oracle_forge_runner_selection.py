from Oracle.tools import forge_experiment_runner as runner


def test_learned_target_uses_v1_incumbent_scope_and_v2_selector(monkeypatch):
    incumbent = {
        "status": "complete", "symbol": "EURUSD", "regime": "trending_down",
        "baseline_genome": {"genome_id": "oracle-v1-genome-9"},
    }
    captured = {}
    monkeypatch.setattr(runner, "_v1_catalog_and_incumbent",
                        lambda symbol: incumbent if symbol == "EURUSD" else {})

    def bridge(task, context):
        captured["task"] = task
        captured["context"] = context
        return {"status": "complete", "data": {
            "status": "complete", "selected_target": "risk",
            "selection_policy": "chronicle_trade_win_rate_screen_ucb_v2",
        }}

    monkeypatch.setattr(runner, "_v2_bridge_act", bridge)
    selected = runner._select_v1_ablation_target("EURUSD", 83020, "a" * 64)

    assert selected["status"] == "complete"
    assert selected["selected_target"] == "risk"
    assert selected["incumbent"] is incumbent
    assert captured["task"] == "oracle.ablation.select"
    request = captured["context"]["request"]
    assert request["strategy_id"] == "oracle-v1-genome-9"
    assert request["regime"] == "trending_down"
    assert request["candidate_targets"] == list(runner.ABLATIONS)
    assert len(request["selection_id"]) == 64


def test_learned_target_rejects_unknown_selector_output(monkeypatch):
    monkeypatch.setattr(runner, "_v1_catalog_and_incumbent", lambda _symbol: {
        "status": "complete", "regime": "ranging",
        "baseline_genome": {"genome_id": "oracle-v1-genome-9"},
    })
    monkeypatch.setattr(runner, "_v2_bridge_act", lambda *_args: {
        "status": "complete", "data": {"status": "complete", "selected_target": "invented"},
    })

    result = runner._select_v1_ablation_target("EURUSD", 83021, "b" * 64)

    assert result["status"] == "blocked"
    assert "unregistered" in result["data"]["message"]
