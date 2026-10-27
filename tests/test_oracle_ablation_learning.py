import hashlib
import pytest

from Forge.core.oracle_improvement import OracleImprovementService


@pytest.fixture(autouse=True)
def isolated_ablation_checkpoint_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_ORACLE_ABLATION_MEMORY_DIR",
                       str(tmp_path / "oracle_ablation_learning"))


class _Chronicle:
    def __init__(self):
        self.records = {}

    def act(self, task, context):
        key = context["key"]
        if task == "state.get":
            record = self.records.get(key)
            return {"status": "complete", "found": record is not None,
                    "value": record["value"] if record else None,
                    "revision": record["revision"] if record else 0}
        if task == "state.set":
            current = self.records.get(key)
            expected = context.get("expected_revision", 0)
            if ((current is None and expected != 0)
                    or (current is not None and current["revision"] != expected)):
                return {"status": "conflict"}
            revision = 1 if current is None else current["revision"] + 1
            self.records[key] = {"value": context["value"], "revision": revision}
            return {"status": "complete", "revision": revision}
        raise AssertionError(task)


class _Forge:
    def __init__(self):
        self.chronicle = _Chronicle()


def _screen(experiment_id, *, trades=100, sharpe_delta=0.15, safe=True):
    original = {
        "evaluation_mode": "development_screen", "experiment_id": experiment_id,
        "symbol": "EURUSD", "regime": "trending_down",
        "baseline_genome": {"genome_id": "oracle-genome-7"},
        "candidate_generation": {"ablation_targets": ["risk"]},
    }
    guardrails = {
        "statistical_evidence_complete": True,
        "relative_max_drawdown_not_worse": True,
        "relative_trade_win_rate_not_worse": True,
        "stress_relative_max_drawdown_not_worse": True,
        "stress_relative_trade_win_rate_not_worse": True,
        "candidate_profitable_after_costs": safe,
        "absolute_drawdown_cap_respected": safe,
        "stress_candidate_profitable_after_costs": safe,
        "stress_absolute_drawdown_cap_respected": safe,
    }
    update = {
        "status": "complete", "guardrails": guardrails,
        "trade_sample": {
            "minimum_required": 100, "baseline_trades": trades,
            "candidate_trades": trades, "stress_baseline_trades": trades,
            "stress_candidate_trades": trades,
        },
        "evaluation": {"comparisons": [
            {"metric": "period_sharpe", "absolute_change": sharpe_delta},
            {"metric": "period_max_drawdown", "absolute_change": -0.01},
            {"metric": "period_win_rate", "absolute_change": 0.01},
        ]},
        "result": {"evidence": {
            "baseline_full_result": {"win_rate_raw": 0.50},
            "treatment_full_result": {"win_rate_raw": 0.56},
            "baseline_stress_result": {"win_rate_raw": 0.48},
            "treatment_stress_result": {"win_rate_raw": 0.52},
        }},
    }
    return original, update


def test_ablation_lesson_ignores_inconclusive_and_deduplicates_qualified_screens():
    chronicle = _Chronicle()
    original, update = _screen("exp-qualified")
    first = OracleImprovementService._record_ablation_screen_lesson(
        chronicle, original, update)
    repeated = OracleImprovementService._record_ablation_screen_lesson(
        chronicle, original, update)
    low_sample_original, low_sample_update = _screen("exp-low-sample", trades=12)
    low_sample = OracleImprovementService._record_ablation_screen_lesson(
        chronicle, low_sample_original, low_sample_update)

    scope_hash = hashlib.sha256(
        b"oracle-genome-7::EURUSD::trending_down").hexdigest()
    record = chronicle.records[
        f"forge.oracle.ablation_learning.v2.{scope_hash}"]["value"]
    risk = record["targets"]["risk"]

    assert first["qualified"] is True
    assert repeated["duplicate"] is True
    assert low_sample["qualified"] is False
    assert risk["qualified_screens"] == 1
    assert risk["inconclusive_screens"] == 1
    assert abs(risk["latest_trade_win_rate_delta"] - 0.05) < 1e-12
    assert len(risk["seen_experiment_ids"]) == 2


def test_missing_trade_win_rate_evidence_is_inconclusive():
    chronicle = _Chronicle()
    original, update = _screen("exp-no-trade-rate")
    update.pop("result")

    result = OracleImprovementService._record_ablation_screen_lesson(
        chronicle, original, update)

    assert result["qualified"] is False
    scope_hash = hashlib.sha256(
        b"oracle-genome-7::EURUSD::trending_down").hexdigest()
    learning = chronicle.records[
        f"forge.oracle.ablation_learning.v2.{scope_hash}"]["value"]
    assert learning["targets"]["risk"]["qualified_screens"] == 0
    assert learning["targets"]["risk"]["inconclusive_screens"] == 1


def test_ablation_selection_is_chronicle_scoped_exploratory_and_idempotent():
    forge = _Forge()
    service = OracleImprovementService(forge)
    request = {
        "symbol": "EURUSD", "strategy_id": "oracle-genome-7",
        "regime": "trending_down", "selection_id": "cycle-42-eurusd",
        "candidate_targets": ["trend", "risk", "exit"],
    }
    first = service.select_oracle_ablation(request)
    repeated = service.select_oracle_ablation(request)

    assert first["status"] == "complete"
    assert first["selected_target"] in request["candidate_targets"]
    assert first["selection_policy"] == "chronicle_trade_win_rate_screen_ucb_v2"
    assert first["promotion_performed"] is False
    assert repeated["selected_target"] == first["selected_target"]
    assert repeated["reused"] is True
    assert len(forge.chronicle.records) == 1

    # GitHub Actions starts with a fresh Chronicle state store each run; the
    # Chronicle-owned research checkpoint must recover the same selection.
    fresh_forge = _Forge()
    recovered = OracleImprovementService(fresh_forge).select_oracle_ablation(request)
    assert recovered["status"] == "complete"
    assert recovered["selected_target"] == first["selected_target"]
    assert recovered["reused"] is True


def test_ablation_selector_explores_under_tested_components():
    forge = _Forge()
    service = OracleImprovementService(forge)
    scope_hash = hashlib.sha256(
        b"oracle-genome-7::EURUSD::trending_down").hexdigest()
    learning_key = f"forge.oracle.ablation_learning.v2.{scope_hash}"
    forge.chronicle.records[learning_key] = {
        "revision": 1,
        "value": {
            "schema": "forge.oracle.ablation_learning.v2",
            "scope": {"strategy_id": "oracle-genome-7", "symbol": "EURUSD",
                      "regime": "trending_down"},
            "targets": {"trend": {"qualified_screens": 100,
                                     "inconclusive_screens": 0,
                                     "reward_sum": 99.0}},
        },
    }
    result = service.select_oracle_ablation({
        "symbol": "EURUSD", "strategy_id": "oracle-genome-7",
        "regime": "trending_down", "selection_id": "cycle-43-eurusd",
        "candidate_targets": ["trend", "risk", "exit"],
    })

    assert result["status"] == "complete"
    assert result["selected_target"] in {"risk", "exit"}
    assert result["selection_scores"]["risk"]["exploration_bonus"] > 0
    assert result["selection_scores"]["exit"]["exploration_bonus"] > 0


def test_ablation_learning_checkpoint_restores_qualified_lessons():
    forge = _Forge()
    original, update = _screen("exp-durable")
    learned = OracleImprovementService._record_ablation_screen_lesson(
        forge.chronicle, original, update)
    assert learned["qualified"] is True

    fresh_forge = _Forge()
    selection = OracleImprovementService(fresh_forge).select_oracle_ablation({
        "symbol": "EURUSD", "strategy_id": "oracle-genome-7",
        "regime": "trending_down", "selection_id": "cycle-after-restart",
        "candidate_targets": ["risk", "exit"],
    })
    scope_hash = hashlib.sha256(
        b"oracle-genome-7::EURUSD::trending_down").hexdigest()

    assert selection["status"] == "complete"
    restored = fresh_forge.chronicle.records[
        f"forge.oracle.ablation_learning.v2.{scope_hash}"]["value"]
    assert restored["targets"]["risk"]["qualified_screens"] == 1
    assert abs(restored["targets"]["risk"]["latest_trade_win_rate_delta"] - 0.05) < 1e-12
