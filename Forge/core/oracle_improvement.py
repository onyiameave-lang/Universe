"""Forge-owned first-pass Oracle strategy research workflow.

This module only evaluates an isolated candidate. It never changes Oracle's
champion. Chronicle is the durable record boundary; active request coordination
is held in Forge memory.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import random
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

from Forge.core.evaluator import Evaluator
from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentData,
    ExperimentDomain,
    ExperimentEnvironment,
    ExperimentEvaluation,
    ExperimentRun,
    ExperimentStatus,
    MetricDirection,
    MetricSpec,
)

log = logging.getLogger("forge.oracle_improvement")


class OracleImprovementService:
    """Run one bounded, Forge-owned challenge for a flagged Oracle champion."""

    def __init__(self, forge_agent: Any):
        self._forge = forge_agent
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    def request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict):
            return {"status": "error", "message": "request must be an object"}
        symbol = str(request.get("symbol", "")).strip().upper()
        regime = str(request.get("regime", "")).strip()
        key = f"{symbol}::{regime}"
        if not symbol or not regime:
            return {"status": "error", "message": "symbol and regime are required"}
        with self._lock:
            if key in self._inflight:
                return {"status": "already_running", "symbol": symbol, "regime": regime}
            self._inflight.add(key)
        try:
            try:
                return self._run(request, symbol, regime)
            except Exception as exc:
                log.exception("Oracle improvement request failed for %s", key)
                return {"status": "error", "message": f"Forge Oracle review failed: {type(exc).__name__}"}
        finally:
            with self._lock:
                self._inflight.discard(key)

    def _run(self, request: Dict[str, Any], symbol: str, regime: str) -> Dict[str, Any]:
        from Forge.core.experimentation import fingerprint_dataset_file

        genome_data = request.get("baseline_genome")
        bars = request.get("bars")
        if not isinstance(genome_data, dict) or not isinstance(genome_data.get("modules"), dict):
            return {"status": "error", "message": "a serialized active Oracle genome is required"}
        if not isinstance(bars, list) or len(bars) < 75:
            return {"status": "blocked", "message": "at least 75 real OHLCV bars are required"}
        costs = request.get("execution_costs")
        if not isinstance(costs, dict):
            return {"status": "blocked", "message": "a versioned broker/account execution-cost profile is required"}
        commission = costs.get("commission_bps")
        slippage = costs.get("slippage_bps")
        spread = costs.get("spread_bps")
        stress_slippage = costs.get("stress_slippage_bps")
        stress_spread = costs.get("stress_spread_bps")
        for name, value in (("commission_bps", commission), ("slippage_bps", slippage),
                            ("spread_bps", spread), ("stress_slippage_bps", stress_slippage),
                            ("stress_spread_bps", stress_spread)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
                return {"status": "blocked", "message": f"valid explicit {name} is required"}
        if (stress_slippage < slippage or stress_spread < spread
                or (stress_slippage == slippage and stress_spread == spread)):
            return {"status": "blocked", "message": "stress spread/slippage must be no lower than base and worsen at least one cost"}
        if not costs.get("broker") or not costs.get("account_type") or not costs.get("profile_version"):
            return {"status": "blocked", "message": "broker, account type, and cost-profile version are required"}
        if costs.get("price_basis") not in {"mid", "bid", "ask"}:
            return {"status": "blocked", "message": "historical OHLC must be explicitly identified as mid, bid, or ask prices"}
        minimum_trades = request.get("minimum_executed_trades")
        if minimum_trades is None:
            minimum_trades = 100
        if isinstance(minimum_trades, bool) or not isinstance(minimum_trades, int) or minimum_trades < 100:
            return {"status": "blocked", "message": "the initial Oracle Forge policy requires at least 100 completed trades per holdout"}
        risk_policy = request.get("risk_policy")
        absolute_drawdown_cap = risk_policy.get("max_drawdown_fraction") if isinstance(risk_policy, dict) else None
        if (isinstance(absolute_drawdown_cap, bool) or not isinstance(absolute_drawdown_cap, (int, float))
                or not math.isfinite(float(absolute_drawdown_cap)) or not 0 < float(absolute_drawdown_cap) <= 1
                or not isinstance(risk_policy.get("source"), str) or not risk_policy["source"].strip()):
            return {"status": "blocked", "message": "Oracle's authoritative absolute drawdown limit and policy source are required"}
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}

        try:
            from Oracle.intelligence.strategy_genome import StrategyGenome
            from Forge.core.adapters.oracle_adapter import build_default_oracle_experiment_adapter

            baseline = StrategyGenome.from_dict(genome_data)
            canonical_bars = self._normalize_bars(bars)
        except Exception as exc:
            return {"status": "error", "message": f"invalid Oracle research input: {type(exc).__name__}: {exc}"}

        data_fingerprint = self._fingerprint(canonical_bars)
        holdout_start = max(40, int(len(canonical_bars) * 0.6))
        if holdout_start >= len(canonical_bars) - 30:
            return {"status": "blocked", "message": "the predeclared evaluation suffix needs at least 30 bars"}
        development_fingerprint = self._fingerprint(canonical_bars[:holdout_start])
        holdout_fingerprint = self._fingerprint(canonical_bars[holdout_start:])
        experiment_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"forge-oracle:{symbol}:{regime}:{baseline.genome_id}:{data_fingerprint}",
        ))
        candidate_seed = int(hashlib.sha256(
            f"forge-oracle-candidate-v1:{symbol}:{regime}:{baseline.genome_id}:{development_fingerprint}".encode()
        ).hexdigest()[:8], 16)
        ablation_targets = request.get("ablation_targets", [])
        if not isinstance(ablation_targets, list) or any(not isinstance(target, str) for target in ablation_targets):
            return {"status": "error", "message": "ablation_targets must be a list of component names"}
        candidate = (StrategyGenome.from_dict(baseline.to_dict()) if ablation_targets else
                     self._mutate_candidate(baseline, random.Random(candidate_seed), rate=0.15))
        candidate_identity = candidate.to_dict()
        candidate_identity["genome_id"] = ""
        candidate.genome_id = "forge-" + hashlib.sha256(
            json.dumps(candidate_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]

        # A holdout belongs to one family. Until Forge has a new, independent
        # suffix, Chronicle rejects another candidate search on the same suffix.
        holdout_key = f"forge.oracle.holdout.{holdout_fingerprint}"
        claim = chronicle.act("state.set", {
            "key": holdout_key,
            "value": {"experiment_id": experiment_id, "symbol": symbol, "regime": regime},
            "expected_revision": 0,
            "_sender": "forge",
        })
        if not isinstance(claim, dict) or claim.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle holdout claim failed; data may already have been evaluated"}

        metric_specs = {
            "period_sharpe": MetricSpec(direction=MetricDirection.HIGHER_IS_BETTER),
            "period_max_drawdown": MetricSpec(direction=MetricDirection.LOWER_IS_BETTER),
            "period_win_rate": MetricSpec(direction=MetricDirection.HIGHER_IS_BETTER),
        }
        contract = None
        run = None
        result = None
        report = None
        record_key = f"forge.oracle.experiment.{experiment_id}"
        record = {
            "schema": "forge.oracle.improvement.v1",
            "status": "running",
            "experiment_id": experiment_id,
            "symbol": symbol,
            "regime": regime,
            "trigger": str(request.get("reason", "performance_review")),
            "baseline_metrics": request.get("performance_metrics", {}),
            "baseline_genome": genome_data,
            "candidate_genome": candidate.to_dict(),
            "dataset_fingerprint": data_fingerprint,
            "holdout_fingerprint": holdout_fingerprint,
            "data_source": request.get("data_source", "unknown"),
            "execution_costs_bps": {
                "commission_per_side": float(commission), "slippage_per_side": float(slippage),
                "spread_full": float(spread),
                "stress_slippage_per_side": float(stress_slippage),
                "stress_spread_full": float(stress_spread),
            },
            "execution_cost_profile": {
                "broker": str(costs["broker"]), "account_type": str(costs["account_type"]),
                "profile_version": str(costs["profile_version"]),
                "profile_status": str(costs.get("profile_status", "unverified")),
                "price_basis": costs["price_basis"],
                "spread_model": "full spread used once to derive the opposite executable quote from the supplied price basis",
            },
            "oracle_risk_policy": {
                "source": str(risk_policy["source"]),
                "absolute_max_drawdown_fraction": float(absolute_drawdown_cap),
            },
            "minimum_executed_trades": minimum_trades,
            "minimum_trade_policy": "oracle_forge_initial_v1_user_set_100",
            "promotion_policy_version": "oracle_forge_first_review_v2",
            "evaluation_design": {
                "method": "chronological_suffix_holdout_v1",
                "start_index": holdout_start,
                "bootstrap_method": "paired circular block bootstrap",
                "bootstrap_replicates": 200,
                "confidence_level": 0.95,
                "candidate_family_size": 1,
                "guardrail_policy": "risk_adjusted_improvement_relative_noninferiority_absolute_drawdown_cap_and_minimum_trade_count_v2",
                "statistical_win_rate_estimand": "fraction_of_evaluation_bars_with_positive_net_return",
                "trade_win_rate_estimand": "winning_completed_trades_divided_by_completed_trades",
            },
            "candidate_generation": {
                "method": "registered_genome_component_ablation_v1" if ablation_targets else "forge_owned_genome_module_mutation_v1",
                "seed": candidate_seed,
                "mutation_rate": None if ablation_targets else 0.15,
                "parent_genome_id": baseline.genome_id,
                "seed_basis": None if ablation_targets else "development_prefix_only",
                "ablation_targets": list(ablation_targets),
            },
            "created_by": "forge",
        }
        stored = chronicle.act("state.set", {
            "key": record_key, "value": record, "expected_revision": 0,
            "_sender": "forge",
        })
        if not isinstance(stored, dict) or stored.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle could not persist the experiment record"}

        try:
            with tempfile.TemporaryDirectory(prefix="forge-oracle-") as directory:
                dataset_path = Path(directory) / "ohlcv.csv"
                with dataset_path.open("w", encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=("timestamp", "open", "high", "low", "close", "volume"))
                    writer.writeheader()
                    writer.writerows(canonical_bars)
                canonical_path, fingerprint = fingerprint_dataset_file(dataset_path)
                contract = ExperimentContract(
                    experiment_id=experiment_id,
                    domain=ExperimentDomain.ORACLE_STRATEGY,
                    baseline_config={"genome": baseline.to_dict()},
                    treatment_config={"genome": candidate.to_dict()},
                    variables={
                        "warmup": 10,
                        "evaluation_start_index": holdout_start,
                        "threshold": float((baseline.entry.params or {}).get("base_threshold", 0.25)),
                        "commission_bps": float(commission),
                        "slippage_bps": float(slippage),
                        "spread_bps": float(spread),
                        "stress_slippage_bps": float(stress_slippage),
                        "stress_spread_bps": float(stress_spread),
                        "price_basis": costs["price_basis"],
                    },
                    data=ExperimentData(
                        dataset_id=f"oracle:{symbol}:{regime}",
                        dataset_path=canonical_path,
                        data_fingerprint=fingerprint,
                    ),
                    environment=ExperimentEnvironment(random_seed=candidate_seed),
                    evaluation=ExperimentEvaluation(
                        metric_specs=metric_specs,
                        minimum_samples=30,
                        confidence_level=0.95,
                        bootstrap_replicates=200,
                    ),
                    created_by="forge",
                    ablation_targets=ablation_targets,
                )
                run = ExperimentRun(experiment_id=experiment_id)
                adapter = build_default_oracle_experiment_adapter()
                result = adapter.execute(contract, run)
            if result.status != ExperimentStatus.COMPLETED:
                return self._finish_record(chronicle, record_key, record, {
                    "status": "failed", "error": result.error_message,
                    "result": result.model_dump(mode="json"),
                })
            report = Evaluator().evaluate(contract, result)
            comparison = {item.metric: item.model_dump(mode="json") for item in report.comparisons}
            sharpe = comparison["period_sharpe"]
            win_rate = comparison["period_win_rate"]
            baseline_result = result.evidence.get("baseline_full_result", {})
            candidate_result = result.evidence.get("treatment_full_result", {})
            baseline_stress_result = result.evidence.get("baseline_stress_result", {})
            candidate_stress_result = result.evidence.get("treatment_stress_result", {})
            baseline_trades = baseline_result.get("trades") if isinstance(baseline_result, dict) else None
            candidate_trades = candidate_result.get("trades") if isinstance(candidate_result, dict) else None
            baseline_trade_win_rate = baseline_result.get("win_rate_raw") if isinstance(baseline_result, dict) else None
            candidate_trade_win_rate = candidate_result.get("win_rate_raw") if isinstance(candidate_result, dict) else None
            candidate_drawdown = candidate_result.get("max_drawdown_raw") if isinstance(candidate_result, dict) else None
            baseline_stress_trades = baseline_stress_result.get("trades") if isinstance(baseline_stress_result, dict) else None
            candidate_stress_trades = candidate_stress_result.get("trades") if isinstance(candidate_stress_result, dict) else None
            baseline_stress_win_rate = baseline_stress_result.get("win_rate_raw") if isinstance(baseline_stress_result, dict) else None
            candidate_stress_win_rate = candidate_stress_result.get("win_rate_raw") if isinstance(candidate_stress_result, dict) else None
            baseline_stress_drawdown = baseline_stress_result.get("max_drawdown_raw") if isinstance(baseline_stress_result, dict) else None
            candidate_stress_drawdown = candidate_stress_result.get("max_drawdown_raw") if isinstance(candidate_stress_result, dict) else None
            guardrails = {
                "sharpe_confidently_better": sharpe.get("confidently_better") is True,
                "relative_max_drawdown_not_worse": (
                    isinstance(baseline_result.get("max_drawdown_raw"), (int, float))
                    and isinstance(candidate_drawdown, (int, float))
                    and candidate_drawdown <= baseline_result["max_drawdown_raw"]
                ),
                "statistical_bar_positive_return_rate_not_worse": (
                    win_rate.get("treatment_value") is not None
                    and win_rate.get("baseline_value") is not None
                    and win_rate["treatment_value"] >= win_rate["baseline_value"]
                    and win_rate.get("confidently_worse") is not True
                ),
                "relative_trade_win_rate_not_worse": (
                    isinstance(baseline_trade_win_rate, (int, float))
                    and isinstance(candidate_trade_win_rate, (int, float))
                    and candidate_trade_win_rate >= baseline_trade_win_rate
                ),
                "candidate_profitable_after_costs": (
                    isinstance(candidate_result.get("total_return_raw"), (int, float))
                    and candidate_result["total_return_raw"] > 0
                ),
                "absolute_drawdown_cap_respected": (
                    isinstance(candidate_drawdown, (int, float))
                    and candidate_drawdown <= float(absolute_drawdown_cap)
                ),
                "stress_candidate_profitable_after_costs": (
                    isinstance(candidate_stress_result.get("total_return_raw"), (int, float))
                    and candidate_stress_result["total_return_raw"] > 0
                ),
                "stress_absolute_drawdown_cap_respected": (
                    isinstance(candidate_stress_drawdown, (int, float))
                    and candidate_stress_drawdown <= float(absolute_drawdown_cap)
                ),
                "stress_relative_max_drawdown_not_worse": (
                    isinstance(baseline_stress_drawdown, (int, float))
                    and isinstance(candidate_stress_drawdown, (int, float))
                    and candidate_stress_drawdown <= baseline_stress_drawdown
                ),
                "stress_relative_trade_win_rate_not_worse": (
                    isinstance(baseline_stress_win_rate, (int, float))
                    and isinstance(candidate_stress_win_rate, (int, float))
                    and candidate_stress_win_rate >= baseline_stress_win_rate
                ),
                "minimum_trade_sample_met": (
                    isinstance(baseline_trades, int) and baseline_trades >= minimum_trades
                    and isinstance(candidate_trades, int) and candidate_trades >= minimum_trades
                ),
                "stress_minimum_trade_sample_met": (
                    isinstance(baseline_stress_trades, int) and baseline_stress_trades >= minimum_trades
                    and isinstance(candidate_stress_trades, int) and candidate_stress_trades >= minimum_trades
                ),
                "statistical_evidence_complete": report.statistical_evidence_complete,
            }
            recommend = all(guardrails.values())
            return self._finish_record(chronicle, record_key, record, {
                "status": "complete",
                "recommendation": (
                    "ablation_review_required" if ablation_targets and recommend
                    else "ablation_failed_guardrails" if ablation_targets
                    else "candidate_for_governed_review" if recommend
                    else "keep_incumbent"
                ),
                "promotion_performed": False,
                "guardrails": guardrails,
                "trade_sample": {
                    "minimum_required": minimum_trades,
                    "baseline_trades": baseline_trades,
                    "candidate_trades": candidate_trades,
                    "stress_baseline_trades": baseline_stress_trades,
                    "stress_candidate_trades": candidate_stress_trades,
                },
                "drawdown_policy_check": {
                    "source": str(risk_policy["source"]),
                    "absolute_max_drawdown_fraction": float(absolute_drawdown_cap),
                    "candidate_max_drawdown_fraction": candidate_drawdown,
                },
                "evaluation": report.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "candidate_genome": candidate.to_dict(),
            })
        except Exception as exc:
            return self._finish_record(chronicle, record_key, record, {
                "status": "failed", "error": f"{type(exc).__name__}: {exc}",
            })

    @staticmethod
    def _finish_record(chronicle: Any, record_key: str, original: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
        current = chronicle.act("state.get", {"key": record_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete" or not current.get("found"):
            return {"status": "error", "message": "Chronicle experiment record could not be read back"}
        final = {**original, **update}
        saved = chronicle.act("state.set", {
            "key": record_key, "value": final,
            "expected_revision": current.get("revision"), "_sender": "forge",
        })
        if not isinstance(saved, dict) or saved.get("status") != "complete":
            return {"status": "error", "message": "Chronicle could not persist the final experiment evidence"}
        return {"status": update.get("status", "complete"), "experiment_id": original["experiment_id"],
                "symbol": original["symbol"], "regime": original["regime"],
                "recommendation": update.get("recommendation", "none"),
                "promotion_performed": False,
                "guardrails": update.get("guardrails"),
                "candidate_genome": update.get("candidate_genome", original.get("candidate_genome")),
                "evaluation": update.get("evaluation"),
                "result_metrics": (update.get("result") or {}).get("metrics"),
                "record_key": record_key,
                "message": update.get("error")}

    @staticmethod
    def _normalize_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for index, bar in enumerate(bars):
            if not isinstance(bar, dict):
                raise ValueError(f"bar {index} must be an object")
            item = {"timestamp": str(bar.get("timestamp", bar.get("ts", index))),
                    **{name: float(bar[name]) for name in ("open", "high", "low", "close")},
                    "volume": float(bar.get("volume", 0.0))}
            values = [item[name] for name in ("open", "high", "low", "close", "volume")]
            if any(not math.isfinite(value) for value in values):
                raise ValueError(f"bar {index} contains a non-finite value")
            if item["low"] <= 0 or item["high"] < max(item["open"], item["close"]) or item["low"] > min(item["open"], item["close"]) or item["volume"] < 0:
                raise ValueError(f"bar {index} has invalid OHLCV bounds")
            normalized.append(item)
        return normalized

    @staticmethod
    def _mutate_candidate(genome: Any, rng: random.Random, rate: float) -> Any:
        """Forge-owned deterministic mutation over Oracle's strategy schema."""
        child = type(genome).from_dict(genome.to_dict())
        child.generation += 1
        child.parents = [genome.genome_id]
        for module_name in (
            "market_regime", "trend", "momentum", "volatility", "entry", "exit",
            "risk", "position", "trade_management",
        ):
            module = getattr(child, module_name, None)
            mutate = getattr(module, "mutate", None)
            if not callable(mutate):
                raise ValueError(f"Oracle genome module {module_name!r} has no mutation contract")
            mutate(rng, rate)
        return child

    @staticmethod
    def _fingerprint(bars: list[dict[str, Any]]) -> str:
        payload = json.dumps(bars, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
