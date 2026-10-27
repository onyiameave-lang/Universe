import json
import hashlib
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone

import pytest

from Forge.core.oracle_improvement import (
    ORACLE_ABLATION_TARGETS,
    OracleImprovementService,
    ORACLE_DIAGNOSTIC_TIMEFRAMES_MS,
    _point_in_time_oracle_timeframe_votes,
    _summarize_oracle_no_trade_evidence,
)


class _Chronicle:
    def __init__(self):
        self.records = {}

    def act(self, task, context):
        if task == "state.list":
            prefix = context.get("prefix", "")
            return {"status": "complete", "keys": sorted(
                key for key in self.records if key.startswith(prefix))}
        key = context["key"]
        if task == "state.get":
            record = self.records.get(key)
            return {"status": "complete", "found": record is not None,
                    "value": record["value"] if record else None,
                    "revision": record["revision"] if record else 0}
        if task == "state.set":
            current = self.records.get(key)
            expected = context["expected_revision"]
            if (current is None and expected != 0) or (
                    current is not None and current["revision"] != expected):
                return {"status": "conflict"}
            revision = 1 if current is None else current["revision"] + 1
            self.records[key] = {"value": context["value"], "revision": revision}
            return {"status": "complete", "revision": revision}
        raise AssertionError(task)


def test_point_in_time_timeframe_replay_excludes_bars_not_closed_at_decision():
    decision = datetime(2026, 1, 1, tzinfo=timezone.utc)
    decision_unix = decision.timestamp()
    bars = {}
    for period, interval_ms in ORACLE_DIAGNOSTIC_TIMEFRAMES_MS.items():
        interval_sec = interval_ms / 1000
        rows = []
        for index in range(151):
            opened = datetime.fromtimestamp(
                decision_unix + (index - 150) * interval_sec, tz=timezone.utc)
            close = 1.0 + index * 0.0001
            rows.append({
                "timestamp": opened.isoformat(), "open": close - 0.00005,
                "high": close + 0.0001, "low": close - 0.0001,
                "close": close, "volume": 1.0,
            })
        bars[period] = rows

    genome = {
        "genome_id": "incumbent-1",
        "modules": {
            "market_regime": {"logic_type": "default", "params": {}},
            "trend": {"logic_type": "ema_slope", "params": {"period": 20}},
            "momentum": {"logic_type": "rsi", "params": {"period": 14}},
            "volatility": {"logic_type": "default", "params": {}},
            "entry": {"logic_type": "default", "params": {}},
        },
    }
    replay = _point_in_time_oracle_timeframe_votes(
        genome, bars, [{"event_id": "decision-1", "at_unix": decision_unix,
                        "outcome_code": "no_setup"}],
    )

    assert replay["status"] == "complete"
    event = replay["events"][0]
    assert set(event["raw_genome_votes"]) == set(ORACLE_DIAGNOSTIC_TIMEFRAMES_MS)
    assert set(event["completed_bar_counts"].values()) == {150}
    assert replay["bar_availability_rule"] == (
        "bar_open_time + timeframe_duration <= Oracle decision time")
    assert "not a live V1 signal" in replay["interpretation"]


def test_atlas_no_trade_prompt_receives_labeled_timeframe_replay():
    forge = _Forge()
    service = OracleImprovementService(forge)
    captured = {}

    class _Atlas:
        def handle(self, request):
            captured["query"] = request["context"]["query"]
            return {"status": "complete", "report": {
                "summary": "Research returned with a source.",
                "sources": [{"title": "Source", "url": "https://example.test"}],
            }}

    forge.atlas = _Atlas()
    timeframe_context = {
        "schema": "forge.oracle.no_trade_timeframe_context.v1",
        "status": "complete", "bar_availability_rule": "bar close <= decision time",
        "events": [{"event_id": "event-1", "raw_genome_votes": {"W1": 1.0}}],
    }
    result = service._research_oracle_no_trade_with_atlas({
        "symbol": "EURUSD", "strategy_id": "incumbent-1", "regime": "trending_up",
        "threshold_days": 14, "evaluation_count_since_last_trade": 12,
        "outcome_counts": {"no_setup": 12}, "recent_decisions": [],
        "timeframe_context": timeframe_context,
    })

    assert result["status"] == "complete"
    assert "forge.oracle.no_trade_timeframe_context.v1" in captured["query"]
    assert "not the deployed V1 multi-timeframe policy" in captured["query"]


class _Forge:
    def __init__(self):
        self.chronicle = _Chronicle()
        self.atlas = None
        from Forge.intelligence.hypothesis_queue import HypothesisQueue
        temp_root = Path(__file__).resolve().parents[2] / "pytest-temp"
        temp_root.mkdir(exist_ok=True)
        self.hypothesis_queue = HypothesisQueue(store_path=Path(
            tempfile.mkdtemp(dir=temp_root)) / "hypotheses.json")


def _observation(event_id, outcome, at):
    return {
        "schema": "oracle.decision.observation.v1",
        "event_id": event_id,
        "symbol": "EURUSD",
        "evaluated_at_unix": at,
        "outcome_code": outcome,
        "execution_environment": "configured",
        "details": ({"reasons": ["signal_below_threshold"],
                     "message": "hold: confidence was below the signal threshold",
                     "confidence": 0.31, "regime": "ranging",
                     "strategy_id": "strategy-test"} if outcome == "no_setup" else {}),
    }


def test_decision_observation_is_persisted_and_summarized_idempotently():
    forge = _Forge()
    service = OracleImprovementService(forge)
    event = _observation("event-1", "no_setup", 1_800_000_000.0)

    first = service.record_decision_observation(event)
    repeated = service.record_decision_observation(event)

    assert first["status"] == "complete"
    assert first["diagnosis"]["latest_outcome"] == "no_setup"
    assert first["diagnosis"]["evaluations_since_last_trade"] == 1
    assert first["diagnosis"]["strategy_evaluations_since_last_trade"] == 1
    assert repeated["status"] == "complete"
    assert repeated["duplicate"] is True
    summary = forge.chronicle.records[first["summary_key"]]["value"]
    assert summary["evaluations"] == 1
    assert summary["outcome_counts"] == {"no_setup": 1}
    assert summary["outcome_counts_since_last_trade"] == {"no_setup": 1}
    assert summary["strategy_outcome_counts_since_last_trade"] == {"no_setup": 1}
    assert summary["recent_events"][0]["details"]["message"].startswith("hold:")
    assert summary["recent_events"][0]["details"]["confidence"] == 0.31
    assert event["event_id"] in {
        item["event_id"] for item in summary["recent_events"]
    }


def test_decision_context_is_projected_to_chronicle_allowlist():
    forge = _Forge()
    service = OracleImprovementService(forge)
    observation = _observation("context-event", "risk_rejected", 1_800_000_000.0)
    observation["details"]["decision_context"] = {
        "schema": "oracle.decision_context.v1",
        "fused": {"call": "buy", "direction": 0.7, "confidence": 0.72,
                  "entry_threshold": 0.15, "learned_weights": {"technical": 0.6}},
        "market": {"regime": "trending_up", "last_price": 1.08,
                   "atr_14": 0.002, "rsi_14": 58},
        "streams": {"technical": {"direction": 0.8, "confidence": 0.75,
                                    "source": "evolved_champion"},
                    "news": {"direction": 0.1, "confidence": 0.3,
                             "evidence_state": "available"}},
        "risk": {"mode": "paper", "confidence_floor": 0.5},
        "provider_payload": {"article_text": "must not be retained"},
    }

    result = service.record_decision_observation(observation)

    assert result["status"] == "complete"
    event = forge.chronicle.records[result["event_key"]]["value"]
    stored_context = event["details"]["decision_context"]
    assert stored_context["fused"]["entry_threshold"] == 0.15
    assert stored_context["streams"]["technical"]["source"] == "evolved_champion"
    assert "provider_payload" not in stored_context
    assert "must not be retained" not in repr(event)
    summary = forge.chronicle.records[result["summary_key"]]["value"]
    assert summary["recent_events"][0]["details"]["decision_context"] == stored_context


def test_malformed_decision_context_is_omitted_without_losing_observation():
    forge = _Forge()
    service = OracleImprovementService(forge)
    observation = _observation("bad-context-event", "no_setup", 1_800_000_000.0)
    observation["details"]["decision_context"] = {
        "schema": "wrong.schema", "provider_payload": "not allowlisted"}

    result = service.record_decision_observation(observation)

    assert result["status"] == "complete"
    event = forge.chronicle.records[result["event_key"]]["value"]
    assert "decision_context" not in event["details"]


def test_no_trade_diagnostics_are_descriptive_and_compare_signal_to_its_threshold():
    decisions = [
        {"outcome_code": "no_setup", "details": {"decision_context": {
            "fused": {"call": "hold", "direction": 0.10, "entry_threshold": 0.15},
            "market": {"regime": "ranging"},
            "streams": {"news": {"evidence_state": "degraded"}},
        }}},
        {"outcome_code": "risk_rejected", "details": {"decision_context": {
            "fused": {"call": "buy", "effective_confidence": 0.4},
            "risk": {"confidence_floor": 0.5},
            "market": {"regime": "trending_up"},
            "streams": {"technical": {"evidence_state": "available"}},
        }}},
        {"outcome_code": "risk_rejected", "details": {"decision_context": {
            "fused": {"call": "sell", "effective_confidence": 0.7},
            "risk": {"confidence_floor": 0.5},
        }}},
    ]

    summary = _summarize_oracle_no_trade_evidence(decisions)

    assert summary["decision_context_coverage"] == 1.0
    assert summary["call_counts"] == {"hold": 1, "buy": 1, "sell": 1}
    assert summary["mean_hold_threshold_gap"] == pytest.approx(0.05)
    assert summary["risk_rejections_with_confidence_below_floor"] == 1
    assert summary["stream_evidence_states"]["news"]["degraded"] == 1
    assert "not causal attribution" in summary["interpretation"]


def test_trade_opened_resets_since_trade_evaluation_count():
    forge = _Forge()
    service = OracleImprovementService(forge)
    service.record_decision_observation(_observation("event-1", "risk_rejected", 1_800_000_000.0))
    result = service.record_decision_observation(_observation("event-2", "trade_opened", 1_800_000_060.0))

    assert result["diagnosis"]["evaluations_since_last_trade"] == 0
    assert result["diagnosis"]["strategy_evaluations_since_last_trade"] == 0
    assert result["diagnosis"]["seconds_since_last_trade"] == 0.0
    summary = forge.chronicle.records[result["summary_key"]]["value"]
    assert summary["outcome_counts"] == {"risk_rejected": 1, "trade_opened": 1}
    assert summary["outcome_counts_since_last_trade"] == {}


def test_decision_observation_rejects_unknown_outcome():
    service = OracleImprovementService(_Forge())
    result = service.record_decision_observation(
        _observation("event-1", "perhaps_trade", 1_800_000_000.0))

    assert result["status"] == "error"


def test_demo_trade_feedback_persists_contextual_loss_evidence_idempotently():
    forge = _Forge()
    service = OracleImprovementService(forge)
    context = {
        "schema": "oracle.demo_trade_context.v1", "direction": "buy",
        "entry_confidence": 0.72, "duration_sec": 1800.0,
        "stop_distance_atr": 2.4, "target_distance_atr": 3.6,
        "target_to_stop_ratio": 1.5, "technical_vote": 0.66,
        "technical_confidence": 0.72,
    }
    outcome = {
        "schema": "oracle.strategy.demo_outcome.v1",
        "close_event_id": "demo-close-1", "strategy_id": "genome-1",
        "timeframe_policy_id": "policy-1", "symbol": "EURUSD",
        "regime": "trending_down", "execution_environment": "demo",
        "pnl_r": -0.8, "won": False, "closed_at_unix": 1_800_000_000.0,
        "exit_reason": "stop_loss", "feedback_source": "oracle_demo_execution",
        "pnl_basis": "price_return_in_initial_stop_r; broker costs not normalized",
        "cost_status": "unavailable_provisional", "trade_context": context,
    }

    first = service.record_demo_outcome(outcome)
    duplicate = service.record_demo_outcome(outcome)
    scope = "genome-1::EURUSD::trending_down"
    scope_hash = __import__("hashlib").sha256(scope.encode()).hexdigest()
    aggregate = forge.chronicle.records[
        f"forge.oracle.strategy_feedback.{scope_hash}"]["value"]
    event_hash = __import__("hashlib").sha256(b"demo-close-1").hexdigest()
    event = forge.chronicle.records[
        f"forge.oracle.demo_trade.{event_hash}"]["value"]

    assert first["status"] == "complete"
    assert first["mean_pnl_r"] == -0.8
    assert duplicate["duplicate"] is True
    assert aggregate["trades"] == 1
    assert aggregate["exit_reason_feedback"]["stop_loss"]["losses"] == 1
    assert aggregate["timeframe_policy_feedback"]["policy-1"]["total_pnl_r"] == -0.8
    assert aggregate["recent_outcomes"][0]["trade_context"] == context
    assert event["trade_context"]["stop_distance_atr"] == 2.4
    assert event["cost_status"] == "unavailable_provisional"


def test_durable_no_trade_job_is_enqueued_before_worker_dispatch(monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_DAYS", "1")
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "1")
    forge = _Forge()
    service = OracleImprovementService(forge)
    summary = {
        "symbol": "EURUSD", "strategy_id": "strategy-1", "regime": "ranging",
        "evaluations": 1, "evaluations_since_last_trade": 1,
        "strategy_evaluations_since_last_trade": 1,
        "first_strategy_evaluation_at_unix": 1_800_000_000.0,
        "last_strategy_evaluation_at_unix": 1_800_000_000.0,
        "first_observation_at_unix": 1_800_000_000.0,
        "outcome_counts_since_last_trade": {"no_setup": 1},
    }
    dispatched = {}

    def start_job(_chronicle, job_key, job, _now):
        persisted = forge.chronicle.records[job_key]["value"]
        assert persisted["status"] == "queued"
        assert persisted["summary_snapshot"]["symbol"] == "EURUSD"
        dispatched["job_key"] = job_key
        return True

    service._start_no_trade_job = start_job
    result = service._schedule_no_trade_diagnosis(
        forge.chronicle, "summary-key", summary, 1_800_172_800.0)

    assert result["status"] == "scheduled"
    assert result["job_key"] == dispatched["job_key"]
    assert forge.chronicle.records[dispatched["job_key"]]["value"]["episode_index"] == 2


def test_market_closed_observations_do_not_count_as_strategy_evaluations(monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_DAYS", "1")
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "1")
    forge = _Forge()
    service = OracleImprovementService(forge)
    start = 1_800_000_000.0
    result = service.record_decision_observation(
        _observation("closed-1", "market_closed", start))
    assert result["diagnosis"]["evaluations_since_last_trade"] == 1
    assert result["diagnosis"]["strategy_evaluations_since_last_trade"] == 0

    # Time passing with only a market-hours blocker must not launch strategy research.
    later = _observation("closed-2", "market_closed", start + 15 * 86400)
    result = service.record_decision_observation(later)
    assert result["diagnosis"]["research_case"]["status"] == "not_due"
    assert result["diagnosis"]["research_case"]["reason"] == (
        "minimum_strategy_evaluation_count_not_met")


def test_heartbeat_marks_per_symbol_evaluation_gaps_and_queues_operational_diagnosis(
        monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_NO_EVALUATION_MIN_CYCLES", "2")
    monkeypatch.setenv("FORGE_ORACLE_NO_EVALUATION_HOURS", "0")
    forge = _Forge()
    service = OracleImprovementService(forge)
    queued = []
    service._start_no_trade_job = lambda _chronicle, key, job, _now: queued.append(
        (key, job)) or True
    base = {
        "schema": "oracle.trader.heartbeat.v1", "instance_id": "oracle-runtime",
        "symbols": ["EURUSD", "GBPUSD"], "cycle_interval_sec": 300,
    }
    first = {**base, "event_id": "hb-1", "cycle": 1,
             "observed_at_unix": 1_800_000_000.0,
             "summary": {"scanned": 2, "per_symbol_outcomes": {
                 "EURUSD": {"no_setup": 1}}}}
    second = {**base, "event_id": "hb-2", "cycle": 2,
              "observed_at_unix": 1_800_000_300.0,
              "summary": {"scanned": 2, "per_symbol_outcomes": {
                  "EURUSD": {"no_setup": 1}}}}

    assert service.record_trader_heartbeat(first)["status"] == "complete"
    response = service.record_trader_heartbeat(second)
    assert response["status"] == "complete"
    assert response["symbol_coverage"]["evaluated_symbols"] == ["EURUSD"]
    assert response["symbol_coverage"]["missing_symbols"][0]["symbol"] == "GBPUSD"
    assert len(queued) == 1
    assert queued[0][1]["diagnosis_type"] == "symbol_evaluation_missing"
    assert queued[0][1]["summary_snapshot"]["symbol"] == "GBPUSD"

    # A missing per-symbol event becomes a durable operational hypothesis only;
    # it cannot invoke a strategy experiment or alter risk.
    forge.atlas = SimpleNamespace(handle=lambda _request: {
        "status": "complete", "report": {"summary": "Check symbol mapping and scan path.",
                                           "sources": [{"title": "System diagnostic guide",
                                                        "url": "https://example.test/diagnostics"}]}})
    result = service._research_symbol_evaluation_gap(
        forge.chronicle, queued[0][1], queued[0][1]["summary_snapshot"])
    assert result["status"] == "complete"
    assert result["strategy_experiment_performed"] is False
    cases = [record["value"] for key, record in forge.chronicle.records.items()
             if key.startswith("forge.oracle.no_evaluation_case.")]
    assert len(cases) == 1
    assert cases[0]["risk_policy_changed"] is False
    assert cases[0]["hypothesis"]["id"] == queued[0][1]["case_id"]


def test_kill_switch_aborted_scans_do_not_trigger_missing_evaluation_research(monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_NO_EVALUATION_MIN_CYCLES", "1")
    monkeypatch.setenv("FORGE_ORACLE_NO_EVALUATION_HOURS", "0")
    forge = _Forge()
    service = OracleImprovementService(forge)
    queued = []
    service._start_no_trade_job = lambda *_args: queued.append(True) or True
    for cycle in (1, 2):
        result = service.record_trader_heartbeat({
            "schema": "oracle.trader.heartbeat.v1", "event_id": f"kill-{cycle}",
            "instance_id": "oracle-kill-switch", "observed_at_unix": 1_800_000_000.0 + cycle,
            "cycle": cycle, "symbols": ["EURUSD"],
            "summary": {"scanned": 0, "trades": 0, "errors": 0,
                        "kill_switch": True, "per_symbol_outcomes": {}},
        })
        assert result["status"] == "complete"
        assert result["symbol_coverage"]["missing_symbols"] == []
    assert queued == []


def test_lockbox_evidence_attaches_only_to_the_registered_ablation(tmp_path):
    forge = _Forge()
    from Forge.intelligence.hypothesis_queue import HypothesisQueue
    forge.hypothesis_queue = HypothesisQueue(
        store_path=tmp_path / "lockbox-flow.json",
        chronicle=forge.chronicle, require_chronicle=True)
    service = OracleImprovementService(forge)
    hypothesis = forge.hypothesis_queue.add(
        "Test whether the registered entry component explains low activity.",
        "forge_oracle_no_trade_diagnosis", "HYP-lockbox-flow")
    forge.hypothesis_queue.link_experiment(hypothesis.id, "development-1")
    forge.hypothesis_queue.record_evidence(hypothesis.id, "oracle_entry_development", {
        "evidence_id": "development-1", "experiment_id": "development-1",
        "evidence_role": "exploratory_development_screen_not_lockbox",
        "ablation_target": "oracle.entry", "supported": None,
    }, 20)
    forge.chronicle.records["forge.oracle.experiment.development-1"] = {
        "revision": 1, "value": {
            "status": "complete", "evaluation_mode": "development_screen",
            "candidate_generation": {"ablation_targets": ["oracle.entry"]},
        }}

    assert service._validate_hypothesis_lockbox_request(
        hypothesis.id, ["oracle.risk"])["status"] == "blocked"
    assert service._validate_hypothesis_lockbox_request(
        hypothesis.id, ["oracle.entry"]) is None

    record_key = "forge.oracle.experiment.lockbox-1"
    forge.chronicle.records[record_key] = {"revision": 1, "value": {
        "status": "complete", "experiment_id": "lockbox-1",
        "symbol": "EURUSD", "regime": "trending_up",
        "evaluation_mode": "lockbox_confirmation",
        "holdout_claim_status": "claimed_once",
        "evaluation_design": {"evidence_role": "single_lockbox_confirmation"},
        "candidate_generation": {"ablation_targets": ["oracle.entry"]},
        "dataset_fingerprint": "a" * 64, "holdout_fingerprint": "b" * 64,
        "evaluation": {"statistical_evidence_complete": True},
        "trade_sample": {"minimum_required": 100, "baseline_trades": 120,
                          "candidate_trades": 110, "stress_baseline_trades": 115,
                          "stress_candidate_trades": 105},
        "guardrails": {"risk_cap": True, "net_profit": True},
        "recommendation": "candidate_for_governed_review",
    }}
    forge.chronicle.records["forge.oracle.holdout." + "b" * 64] = {
        "revision": 1, "value": {"experiment_id": "lockbox-1",
                                  "symbol": "EURUSD", "regime": "trending_up"}}

    result = service._attach_hypothesis_lockbox_evidence(
        forge.chronicle, {"status": "complete", "experiment_id": "lockbox-1",
                          "record_key": record_key}, hypothesis.id)
    assert result["status"] == "complete"
    assert result["hypothesis_status"] == "confirmed"
    assert result["promotion_performed"] is not True
    assert result["hypothesis_evidence"]["evidence_role"] == "single_lockbox_confirmation"


def test_lockbox_context_must_match_registered_development_screen(tmp_path):
    forge = _Forge()
    service = OracleImprovementService(forge)
    queue = forge.hypothesis_queue
    hypothesis = queue.add("Keep the lockbox tied to its registered screen.",
                           "test", "HYP-lockbox-context")
    queue.link_experiment(hypothesis.id, "development-context")
    queue.record_evidence(hypothesis.id, "development_screen", {
        "evidence_id": "development-context", "experiment_id": "development-context",
        "evidence_role": "exploratory_development_screen_not_lockbox",
        "ablation_target": "oracle.entry",
    }, sample_size=50)
    baseline = {"genome_id": "incumbent-1", "modules": {"entry": {"period": 14}}}
    costs = {
        "broker": "Pepperstone", "account_type": "cTrader Demo Razor",
        "profile_version": "observed-v1", "price_basis": "bid",
        "commission_bps": 0.1, "slippage_bps": 0.2, "spread_bps": 0.3,
        "stress_slippage_bps": 0.4, "stress_spread_bps": 0.5,
    }
    risk = {"source": "oracle-risk-v1", "max_drawdown_fraction": 0.2}
    development = {
        "status": "complete", "experiment_id": "development-context",
        "symbol": "EURUSD", "regime": "trending_up",
        "evaluation_mode": "development_screen", "dataset_fingerprint": "a" * 64,
        "baseline_genome": baseline,
        "candidate_generation": {"ablation_targets": ["oracle.entry"]},
        "execution_cost_profile": {key: costs[key] for key in
            ("broker", "account_type", "profile_version", "price_basis")},
        "execution_costs_bps": {
            "commission_per_side": 0.1, "slippage_per_side": 0.2,
            "spread_full": 0.3, "stress_slippage_per_side": 0.4,
            "stress_spread_full": 0.5,
        },
        "oracle_risk_policy": {
            "source": "oracle-risk-v1", "absolute_max_drawdown_fraction": 0.2},
    }
    forge.chronicle.records["forge.oracle.experiment.development-context"] = {
        "revision": 1, "value": development}
    request = {
        "symbol": "EURUSD", "regime": "trending_up", "data_fingerprint": "a" * 64,
        "baseline_genome": baseline, "execution_costs": costs, "risk_policy": risk,
    }

    assert service._validate_hypothesis_lockbox_request(
        hypothesis.id, ["oracle.entry"], request, require_exact_context=True) is None
    for field, changed in (("data_fingerprint", "c" * 64),
                           ("baseline_genome", {"genome_id": "other", "modules": {"entry": {}}}),
                           ("execution_costs", {**costs, "spread_bps": 9.0}),
                           ("risk_policy", {**risk, "max_drawdown_fraction": 0.5})):
        altered = {**request, field: changed}
        assert service._validate_hypothesis_lockbox_request(
            hypothesis.id, ["oracle.entry"], altered,
            require_exact_context=True)["status"] == "blocked"


def test_lockbox_replay_loader_checks_chronicle_dataset_fingerprints(tmp_path):
    periods = ("W1", "D1", "H4", "H1", "M45", "M30", "M15")
    datasets = {}
    fingerprints = {}
    for index, period in enumerate(periods):
        relative = f"data/EURUSD/{period}.csv"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        bars = [{"timestamp": f"2025-01-0{n}T00:00:00+00:00",
                 "open": 1.0 + index * 0.01, "high": 1.1 + index * 0.01,
                 "low": 0.9 + index * 0.01, "close": 1.02 + index * 0.01,
                 "volume": 100.0} for n in (1, 2)]
        import csv
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=(
                "timestamp", "open", "high", "low", "close", "volume"))
            writer.writeheader()
            writer.writerows(bars)
        datasets[period] = relative
        fingerprints[period] = OracleImprovementService._fingerprint(bars)
    data_fingerprint = __import__("hashlib").sha256(json.dumps({
        "symbol": "EURUSD", "period_fingerprints": fingerprints,
        "price_basis": "bid", "schema": "oracle.topdown_ohlcv.v1",
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    manifest = {
        "schema": "oracle.topdown_dataset_manifest.v1", "symbol": "EURUSD",
        "history_target": "5y", "source": "ctrader_open_api_bid",
        "price_basis": "bid", "data_as_of": "2026-01-01T00:00:00+00:00",
        "data_fingerprint": data_fingerprint,
        "timeframe_fingerprints": fingerprints,
        "timeframe_coverage": {period: {"bars": 2} for period in periods},
        "datasets": datasets,
    }
    manifest_fingerprint = __import__("hashlib").sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode()).hexdigest()
    manifest["manifest_fingerprint"] = manifest_fingerprint
    (tmp_path / "data/manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    experiment = {"dataset_fingerprint": data_fingerprint,
                  "dataset_manifest": {"status": "complete",
                      "manifest_path": "data/manifest.json",
                      "manifest_fingerprint": manifest_fingerprint,
                      "data_fingerprint": data_fingerprint}}

    loaded = OracleImprovementService._load_chronicle_topdown_bundle(
        experiment, repository_root=tmp_path)
    assert set(loaded["bars_by_timeframe"]) == set(periods)
    assert loaded["data_fingerprint"] == data_fingerprint

    with (tmp_path / datasets["M15"]).open("a", encoding="utf-8") as output:
        output.write("2025-01-03T00:00:00+00:00,1,1.1,0.9,1.02,100\n")
    with pytest.raises(ValueError, match="no longer matches its fingerprint"):
        OracleImprovementService._load_chronicle_topdown_bundle(
            experiment, repository_root=tmp_path)


def test_lockbox_claim_is_idempotent_for_the_same_experiment_only():
    chronicle = _Chronicle()
    first = OracleImprovementService._claim_lockbox_once(
        chronicle, "b" * 64, "experiment-1", "EURUSD", "trending_up")
    retry = OracleImprovementService._claim_lockbox_once(
        chronicle, "b" * 64, "experiment-1", "EURUSD", "trending_up")
    competing = OracleImprovementService._claim_lockbox_once(
        chronicle, "b" * 64, "experiment-2", "EURUSD", "trending_up")

    assert first["status"] == "complete"
    assert retry["status"] == "complete" and retry["recovered"] is True
    assert competing["status"] == "blocked"


def test_startup_recovers_durable_queued_diagnosis_job():
    forge = _Forge()
    service = OracleImprovementService(forge)
    job_key = "forge.oracle.no_trade_job.test"
    forge.chronicle.records[job_key] = {
        "value": {
            "schema": "forge.oracle.no_trade_job.v1", "status": "queued",
            "summary_key": "summary-key",
            "summary_snapshot": {"symbol": "EURUSD", "evaluations_since_last_trade": 12},
            "attempts": 0,
        },
        "revision": 1,
    }
    recovered = {}

    def start_job(_chronicle, recovered_key, job, _now):
        recovered["key"] = recovered_key
        recovered["job"] = job
        return True

    service._start_no_trade_job = start_job
    result = service.resume_pending_no_trade_jobs()

    assert result == {"status": "complete", "resumed": 1, "skipped": 0}
    assert recovered["key"] == job_key
    assert recovered["job"]["summary_snapshot"]["symbol"] == "EURUSD"


def test_existing_no_trade_case_retries_incomplete_atlas_research(monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_DAYS", "14")
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "10")
    forge = _Forge()
    service = OracleImprovementService(forge)
    baseline = 1_800_000_000.0
    now = baseline + 15 * 86400
    symbol, strategy_id, regime = "EURUSD", "strategy-1", "trending_up"
    case_id = "HYP-ORACLE-NO-TRADE-" + __import__("hashlib").sha256(
        f"{symbol}::{strategy_id}::{int(baseline)}:1".encode()).hexdigest()[:20]
    case_key = "forge.oracle.no_trade_case." + __import__("hashlib").sha256(
        case_id.encode()).hexdigest()
    hypothesis = forge.hypothesis_queue.add(
        "Recheck the no-trade research.", "test", case_id)
    saved_case = {
        "case_id": case_id, "symbol": symbol, "strategy_id": strategy_id,
        "regime": regime, "baseline_at_unix": baseline, "threshold_days": 14,
        "evaluation_count_since_last_trade": 20,
        "outcome_counts": {"no_setup": 20},
        "recent_decisions": [{"event_id": "retry-event-1", "outcome_code": "no_setup",
                              "reason_codes": ["signal_below_threshold"],
                              "details": {"message": "below threshold"}}],
        "primary_observed_outcome": "no_setup",
        "atlas_research": {"status": "partial", "summary": ""},
        "hypothesis": hypothesis.to_dict(),
        "experiment": {"status": "blocked"}, "experiment_attempts": 1,
        "last_experiment_attempt_at_unix": now - 3601,
    }
    forge.chronicle.records[case_key] = {"value": saved_case, "revision": 1}

    class _Atlas:
        def __init__(self):
            self.calls = 0

        def handle(self, request):
            self.calls += 1
            assert request["context"]["domain"] == "forex strategy diagnostics"
            return {"status": "complete", "report": {
                "summary": "Cited market evidence supports testing entry sensitivity.",
                "sources": [{"title": "Research source", "url": "https://example.test"}],
            }}

    atlas = _Atlas()
    forge.atlas = atlas
    captured = {}

    def fake_experiment(_chronicle, case):
        captured["research"] = case["atlas_research"]
        return {"status": "complete", "experiment_id": "experiment-retry"}

    service._run_no_trade_hypothesis_experiment = fake_experiment
    summary = {
        "symbol": symbol, "strategy_id": strategy_id, "regime": regime,
        "last_trade_at_unix": baseline,
        "strategy_evaluations_since_last_trade": 20,
        "last_strategy_evaluation_at_unix": now - 60,
        "strategy_outcome_counts_since_last_trade": {"no_setup": 20},
    }
    result = service._maybe_research_no_trade_case(
        forge.chronicle, "summary-key", summary, now)

    assert result["experiment_status"] == "complete"
    assert atlas.calls == 1
    assert captured["research"]["status"] == "complete"
    assert captured["research"]["sources"]
    assert forge.chronicle.records[case_key]["value"]["lifecycle_status"] == "experiment_recorded"
    research_items = [item for item in forge.hypothesis_queue.get(case_id).evidence
                      if item.get("result", {}).get("evidence_role")
                      == "atlas_research_context"]
    assert len(research_items) == 1
    assert research_items[0]["result"]["sources"]
    assert len(research_items[0]["result"]["atlas_query_fingerprint"]) == 64
    assert len(research_items[0]["result"]["oracle_observation_fingerprint"]) == 64
    assert research_items[0]["result"]["recent_decisions"][0]["event_id"]


def test_persistent_no_trade_creates_a_new_case_for_each_elapsed_review_episode(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_DAYS", "14")
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "1")
    monkeypatch.setenv("FORGE_ORACLE_ABLATION_MEMORY_DIR", str(tmp_path / "ablation-memory"))
    forge = _Forge()
    service = OracleImprovementService(forge)

    class _Atlas:
        def handle(self, _request):
            return {"status": "complete", "report": {
                "summary": "Review the recurring low activity pattern.",
                "sources": [{"title": "Research source", "url": "https://example.test"}],
            }}

    forge.atlas = _Atlas()
    service._run_no_trade_hypothesis_experiment = lambda _chronicle, _case: {
        "status": "complete", "experiment_id": "development-screen"}
    baseline = 1_800_000_000.0
    summary = {
        "symbol": "EURUSD", "strategy_id": "strategy-1", "regime": "ranging",
        "first_strategy_evaluation_at_unix": baseline,
        "last_strategy_evaluation_at_unix": baseline + 15 * 86400 - 60,
        "strategy_evaluations_since_last_trade": 20,
        "strategy_outcome_counts_since_last_trade": {"no_setup": 20},
    }

    first = service._maybe_research_no_trade_case(
        forge.chronicle, "summary-key", summary,
        baseline + 15 * 86400)
    second_summary = {**summary,
                      "last_strategy_evaluation_at_unix": baseline + 29 * 86400 - 60}
    second = service._maybe_research_no_trade_case(
        forge.chronicle, "summary-key", second_summary,
        baseline + 29 * 86400)

    cases = [record["value"] for key, record in forge.chronicle.records.items()
             if key.startswith("forge.oracle.no_trade_case.")]
    assert first["status"] == "complete"
    assert second["status"] == "complete"
    assert len(cases) == 2
    assert {case["episode_index"] for case in cases} == {1, 2}
    assert len({case["case_id"] for case in cases}) == 2
    assert all(case["promotion_performed"] is False for case in cases)


def test_diagnosis_retry_uses_current_time_not_original_schedule(monkeypatch):
    import Forge.core.oracle_improvement as module

    forge = _Forge()
    service = OracleImprovementService(forge)
    job_key = "forge.oracle.no_trade_job.retry-clock"
    forge.chronicle.records[job_key] = {
        "value": {
            "schema": "forge.oracle.no_trade_job.v1", "status": "running",
            "summary_key": "summary-key", "scheduled_at_unix": 1000.0,
            "summary_snapshot": {"symbol": "EURUSD"},
            "attempts": 2,
        },
        "revision": 1,
    }
    now = 9000.0
    monkeypatch.setattr(module.time, "time", lambda: now)
    observed = {}

    def research(_chronicle, summary_key, summary, now_unix):
        observed["summary_key"] = summary_key
        observed["summary"] = summary
        observed["now_unix"] = now_unix
        return {"status": "not_due", "reason": "no_trade_window_not_elapsed"}

    service._maybe_research_no_trade_case = research
    service.resume_pending_no_trade_jobs = lambda: {"status": "complete"}
    service._run_no_trade_diagnosis(
        job_key, forge.chronicle, forge.chronicle.records[job_key]["value"])

    assert observed["now_unix"] == now
    assert observed["now_unix"] != 1000.0
    assert forge.chronicle.records[job_key]["value"]["status"] == "retryable"


def test_trader_heartbeat_distinguishes_and_recovers_stale_runtime(monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_HEARTBEAT_TIMEOUT_SEC", "60")
    forge = _Forge()
    service = OracleImprovementService(forge)
    queued = []
    service._start_no_trade_job = lambda _chronicle, key, job, _now: queued.append(
        (key, job)) or True
    at = 1_800_000_000.0
    heartbeat = {
        "schema": "oracle.trader.heartbeat.v1", "event_id": "heartbeat-1",
        "instance_id": "oracle-process-1", "observed_at_unix": at,
        "cycle": 1, "symbols": ["EURUSD"],
        "summary": {"scanned": 1, "trades": 0, "errors": 0},
    }

    first = service.record_trader_heartbeat(heartbeat)
    repeated = service.record_trader_heartbeat(heartbeat)
    stale = service.check_stale_trader_heartbeats(now_unix=at + 61)
    incident_key = next(key for key in forge.chronicle.records
                        if key.startswith("forge.oracle.trader_heartbeat.incident."))
    assert first["status"] == "complete"
    assert repeated["heartbeat_count"] == 1
    assert stale["stale_instances"] == 1
    assert forge.chronicle.records[incident_key]["value"]["status"] == "stale"
    assert len(queued) == 1
    assert queued[0][1]["diagnosis_type"] == "trader_heartbeat_stale"

    class _Atlas:
        def handle(self, request):
            assert "cannot distinguish" in request["context"]["query"]
            return {"status": "complete", "report": {
                "summary": "Check process liveness, bridge delivery, and local outbox state.",
                "sources": [{"title": "Operations reference", "url": "https://example.test"}],
            }}

    forge.atlas = _Atlas()
    diagnosis = service._research_stale_trader_heartbeat(
        forge.chronicle, queued[0][1], queued[0][1]["summary_snapshot"])
    assert diagnosis["status"] == "complete"
    assert diagnosis["strategy_experiment_performed"] is False
    case_key = diagnosis["case_key"]
    case = forge.chronicle.records[case_key]["value"]
    assert case["atlas_research"]["sources"]
    assert case["promotion_performed"] is False

    recovered_heartbeat = {**heartbeat, "event_id": "heartbeat-2",
                           "observed_at_unix": at + 62, "cycle": 2}
    service.record_trader_heartbeat(recovered_heartbeat)
    assert forge.chronicle.records[incident_key]["value"]["status"] == "recovered"
    assert forge.chronicle.records[case_key]["value"]["runtime_status"] == "recovered"
    status = service.get_oracle_training_status()
    assert status["status"] == "complete"
    assert status["traders"][0]["status"] == "online"
    assert status["traders"][0]["incident"]["status"] == "recovered"


def test_sustained_no_trade_pattern_opens_atlas_research_and_hypothesis(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_ORACLE_ABLATION_MEMORY_DIR", str(tmp_path / "ablation-memory"))
    class _Atlas:
        def __init__(self):
            self.query = None

        def handle(self, request):
            self.query = request["context"]["query"]
            return {"status": "complete", "report": {
                "summary": "A volatility contraction may reduce qualifying entries.",
                "confidence": 0.62,
                "sources": [{"title": "Example source", "url": "https://example.test"}],
                "key_terms": ["volatility contraction"],
            }}

    forge = _Forge()
    forge.atlas = _Atlas()
    service = OracleImprovementService(forge)
    start = time.time() - 15 * 86400
    result = None
    for index in range(15):
        event = _observation(f"event-{index}", "no_setup", start + index * 86400)
        event["details"]["decision_context"] = {
            "schema": "oracle.decision_context.v1",
            "fused": {"call": "hold", "direction": 0.1,
                      "confidence": 0.3, "entry_threshold": 0.15},
            "market": {"regime": "ranging"},
            "streams": {"technical": {"direction": 0.4, "confidence": 0.7,
                                        "source": "evolved_champion"}},
        }
        result = service.record_decision_observation(event)

    assert result["diagnosis"]["research_case"]["status"] == "scheduled"
    deadline = time.monotonic() + 5.0
    case_key = None
    while time.monotonic() < deadline:
        case_key = next((key for key in forge.chronicle.records
                         if key.startswith("forge.oracle.no_trade_case.")), None)
        if case_key:
            break
        time.sleep(0.01)
    assert case_key is not None
    assert "no trade" in forge.atlas.query
    assert "signal_below_threshold" in forge.atlas.query
    assert "mean_hold_threshold_gap" in forge.atlas.query
    record = forge.chronicle.records[case_key]["value"]
    assert record["promotion_performed"] is False
    assert record["risk_policy_changed"] is False
    assert record["atlas_research"]["sources"][0]["title"] == "Example source"
    hypothesis = forge.hypothesis_queue.get(record["hypothesis"]["id"])
    assert hypothesis is not None
    assert any(item.get("result", {}).get("evidence_role") == "atlas_research_context"
               for item in hypothesis.evidence)


@pytest.mark.parametrize(("outcome", "target"), [
    ("no_setup", "entry"),
    ("risk_rejected", "risk"),
])
def test_strategy_blocker_hypothesis_links_development_screen_and_keeps_it_inconclusive(
        monkeypatch, tmp_path, outcome, target):
    monkeypatch.setenv("ORACLE_FORGE_BRIDGE_TOKEN", "unit-test-token")
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "480")
    monkeypatch.setenv("FORGE_ORACLE_ABLATION_MEMORY_DIR", str(tmp_path / "ablation-memory"))
    forge = _Forge()
    service = OracleImprovementService(forge)
    hypothesis = forge.hypothesis_queue.add(
        "Test whether Oracle's entry gate suppresses EURUSD setups.",
        "forge_oracle_no_trade_diagnosis", "HYP-test-entry")
    profile = {
        "profile_status": "api_observed_costs", "broker": "Pepperstone",
        "account_type": "cTrader Demo Razor", "profile_version": "test-v1",
            "account_id": "480",
        "price_basis": "bid", "commission_bps": 0.1, "slippage_bps": 0.2,
        "spread_bps": 0.3, "stress_slippage_bps": 0.4,
        "stress_spread_bps": 0.5,
        "evidence": {"symbol_filled_deals": 20, "matched_slippage_deals": 20,
                     "matched_spread_quotes": 100},
    }
    forge.chronicle.records["forge.oracle.cost_profile.latest.EURUSD"] = {
        "value": {"version_key": "cost-version"}, "revision": 1}
    forge.chronicle.records["cost-version"] = {
        "value": {"status": "complete", "symbol": "EURUSD",
                  "account_id": "480", "profile": profile}, "revision": 1}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "status": "complete", "regime": "trending_up",
                "baseline_genome": {"genome_id": "strategy-1", "modules": {}},
                "performance_metrics": {}, "risk_policy": {
                    "source": "Oracle V1", "max_drawdown_fraction": 0.2},
                "minimum_executed_trades": 100,
            }).encode("utf-8")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    captured = []

    def fake_request(request):
        captured.append(dict(request))
        if request["evaluation_mode"] == "development_screen":
            return {
                "status": "complete", "experiment_id": "experiment-1",
                "record_key": "forge.oracle.experiment.experiment-1",
                "evaluation_mode": "development_screen",
                "dataset_fingerprint": "a" * 64,
                "dataset_manifest": {"status": "complete", "manifest_path": "manifest.json"},
                "recommendation": "development_screen_only",
                "interpretation": "exploratory only", "promotion_performed": False,
                "guardrails": {"absolute_drawdown_cap_respected": True},
                "trade_sample": {"baseline_trades": 40, "candidate_trades": 50},
                "result_metrics": {"period_sharpe": 0.4},
            }
        return {
            "status": "complete", "experiment_id": "experiment-2",
            "record_key": "forge.oracle.experiment.experiment-2",
            "evaluation_mode": "lockbox_confirmation",
            "recommendation": "ablation_inconclusive_insufficient_trade_sample",
            "promotion_performed": False,
            "hypothesis_status": "inconclusive",
            "trade_sample": {"baseline_trades": 40, "candidate_trades": 50},
        }

    monkeypatch.setattr(service, "request", fake_request)
    monkeypatch.setattr(service, "_load_chronicle_topdown_bundle", lambda _experiment: {
        "bars_by_timeframe": {period: [] for period in
                              ("W1", "D1", "H4", "H1", "M45", "M30", "M15")},
        "timeframe_fingerprints": {}, "timeframe_coverage": {},
        "data_fingerprint": "a" * 64, "data_as_of": "2026-01-01T00:00:00+00:00",
        "history_target": "5y", "data_source": "ctrader_open_api_bid",
        "research_timeframes": ["W1", "D1", "H4", "H1", "M45", "M30", "M15"],
    })
    case = {
        "case_id": "HYP-test-entry", "symbol": "EURUSD",
        "strategy_id": "strategy-1", "regime": "trending_up",
        "primary_observed_outcome": outcome,
        "atlas_research": {"status": "complete", "summary": "Observed blocker warrants a controlled test.",
                           "sources": [{"title": "Atlas citation", "url": "https://example.test"}]},
        "hypothesis": {"id": hypothesis.id},
    }
    scope = {"strategy_id": "strategy-1", "symbol": "EURUSD", "regime": "trending_up"}
    scope_hash = hashlib.sha256(
        "strategy-1::EURUSD::trending_up".encode()).hexdigest()
    learning_key = f"forge.oracle.ablation_learning.v2.{scope_hash}"
    target_history = {
        candidate: {
            "qualified_screens": 30, "inconclusive_screens": 0,
            "reward_sum": 30.0 if candidate == target else 0.0,
            "trade_win_rate_delta_sum": 0.30 if candidate == target else -0.30,
            "seen_experiment_ids": [],
        }
        for candidate in ORACLE_ABLATION_TARGETS
    }
    forge.chronicle.records[learning_key] = {
        "value": {"schema": "forge.oracle.ablation_learning.v2", "scope": scope,
                  "targets": target_history},
        "revision": 1,
    }
    case["ablation_selection"] = service.select_oracle_ablation({
        "symbol": "EURUSD", "strategy_id": "strategy-1", "regime": "trending_up",
        "selection_id": "oracle-no-trade:HYP-test-entry",
        "candidate_targets": list(ORACLE_ABLATION_TARGETS),
    })

    result = service._run_no_trade_hypothesis_experiment(forge.chronicle, case)

    assert result["status"] == "complete", result
    assert result["promotion_performed"] is False
    assert len(captured) == 2
    assert captured[0]["evaluation_mode"] == "development_screen"
    assert captured[0]["ablation_targets"] == [target]
    assert captured[1]["evaluation_mode"] == "lockbox_confirmation"
    assert captured[1]["ablation_targets"] == [target]
    assert captured[1]["hypothesis_id"] == hypothesis.id
    assert len(captured[1]["bars_by_timeframe"]) == 7
    assert result["ablation_target"] == target
    assert result["ablation_selection"]["selected_target"] == target
    assert result["risk_policy_changed"] is False
    learned = forge.hypothesis_queue.get(hypothesis.id)
    assert learned.linked_experiment_ids == ["experiment-1"]
    assert learned.status == "inconclusive"
    assert learned.evidence[0]["result"]["evidence_role"] == "exploratory_development_screen_not_lockbox"


def test_persistent_execution_failures_schedule_an_operational_diagnosis(monkeypatch):
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_DAYS", "14")
    monkeypatch.setenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "10")
    forge = _Forge()
    service = OracleImprovementService(forge)
    baseline = 1_800_000_000.0
    dispatched = []
    service._start_no_trade_job = lambda _chronicle, key, job, _now: (
        dispatched.append((key, job)) or True)

    for index in range(10):
        timestamp = baseline + (14 * 86400 * index / 9)
        result = service.record_decision_observation({
            "schema": "oracle.decision.observation.v1",
            "event_id": f"execution-reject-{index}",
            "symbol": "EURUSD", "evaluated_at_unix": timestamp,
            "outcome_code": "execution_rejected", "execution_environment": "demo",
            "details": {"strategy_id": "strategy-test", "regime": "ranging",
                        "broker_status": "rejected", "reason": "minimum volume"},
        })
        assert result["status"] == "complete"

    assert len(dispatched) == 1
    job_key, job = dispatched[0]
    assert job["diagnosis_type"] == "persistent_operational_no_trade"
    assert job["summary_snapshot"]["primary_observed_outcome"] == "execution_rejected"
    assert job["summary_snapshot"]["operational_outcome_counts"]["execution_rejected"] == 10
    assert job_key.startswith("forge.oracle.no_trade_job.")


def test_operational_no_trade_research_persists_atlas_hypothesis_without_strategy_experiment():
    forge = _Forge()
    service = OracleImprovementService(forge)
    captured = {}

    class _Atlas:
        def handle(self, request):
            captured["query"] = request["context"]["query"]
            return {"status": "complete", "report": {
                "summary": "Broker reports repeated minimum-volume rejections.",
                "sources": [{"title": "Broker documentation", "url": "https://example.test"}],
            }}

    forge.atlas = _Atlas()
    result = service._research_persistent_operational_no_trade(
        forge.chronicle,
        {"case_id": "CASE-ops-1"},
        {"symbol": "EURUSD", "strategy_id": "strategy-test", "regime": "ranging",
         "threshold_days": 14, "primary_observed_outcome": "execution_rejected",
         "operational_outcome_counts": ["legacy-malformed-counts"],
         "recent_decisions": {"legacy-malformed-events": True}},
    )

    assert result["status"] == "complete"
    assert result["strategy_experiment_performed"] is False
    assert "Do not run strategy ablations" in captured["query"]
    persisted = forge.chronicle.act("state.get", {"key": result["case_key"]})["value"]
    assert persisted["schema"] == "forge.oracle.operational_no_trade_case.v1"
    assert persisted["promotion_performed"] is False
    assert persisted["operational_outcome_counts"] == {}
    assert persisted["recent_decisions"] == []
    assert forge.hypothesis_queue.get(result["hypothesis_id"]) is not None
    status = service.get_oracle_training_status()
    assert status["status"] == "complete"
    assert status["operational_no_trade_cases"][0]["case_id"] == "CASE-ops-1"


def test_training_status_exposes_compact_latest_decision_receipt():
    forge = _Forge()
    service = OracleImprovementService(forge)
    forge.chronicle.act("state.set", {
        "key": "forge.oracle.decision_summary.test",
        "value": {
            "schema": "forge.oracle.decision_summary.v1",
            "symbol": "EURUSD", "strategy_id": "champion-1", "regime": "ranging",
            "last_event_at_unix": 1_800_000_001.0, "last_trade_at_unix": None,
            "evaluations": 12, "evaluations_since_last_trade": 12,
            "outcome_counts_since_last_trade": {"risk_rejected": 12},
            "recent_events": [{
                "event_id": "decision-1", "at_unix": 1_800_000_001.0,
                "outcome_code": "risk_rejected",
                "details": {"decision_context": {
                    "fused": {"call": "hold", "confidence": 0.42},
                    "market": {"regime": "ranging"},
                }},
            }],
        },
        "expected_revision": 0,
    })

    status = service.get_oracle_training_status()

    assert status["status"] == "complete"
    receipt = status["decision_summaries"][0]
    assert receipt["symbol"] == "EURUSD"
    assert receipt["recent_events"][0]["has_decision_context"] is True
    assert receipt["recent_events"][0]["decision_call"] == "hold"
    assert receipt["recent_events"][0]["confidence"] == 0.42
