"""Bridge Forge experiments to Oracle's existing strategy backtester."""

import copy
import csv
import hashlib
import inspect
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from Forge.core.ablation import ComponentManifest
from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentDomain,
    ExperimentResult,
    ExperimentRun,
    ExperimentStatus,
)
from Forge.core.metrics import (
    bootstrap_paired_bar_return_metrics,
    summarize_bar_return_metrics,
)


class OracleExperimentAdapter:
    """Execute isolated baseline and treatment Oracle backtests."""

    domain = ExperimentDomain.ORACLE_STRATEGY
    version = "1.0.0"
    supports_paired_statistical_evidence = True
    promotion_evidence_ready = True
    paired_statistical_metrics = (
        "period_total_return", "period_sharpe", "period_max_drawdown",
        "period_win_rate", "period_profit_factor", "period_expectancy",
    )

    def __init__(
        self,
        backtester: Any,
        genome_class: type,
        decision_factory: Callable,
        manifest: ComponentManifest,
        ablation_handlers: Dict[str, Callable[[Any], None]],
        data_loader: Callable[[str], Any],
        provenance_files: Optional[List[str]] = None,
    ):
        self._backtester = backtester
        self._Genome = genome_class
        self._decision_factory = decision_factory
        self._manifest = manifest
        self._ablation_handlers = ablation_handlers
        self._data_loader = data_loader
        self.provenance_files = list(provenance_files or ())

    def execute(self, contract: ExperimentContract, run: ExperimentRun) -> ExperimentResult:
        def failed(message: str, evidence: Optional[dict] = None) -> ExperimentResult:
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=message,
                evidence=evidence or {},
            )

        try:
            warmup = contract.variables["warmup"]
            evaluation_start_index = contract.variables["evaluation_start_index"]
            threshold = contract.variables["threshold"]
            commission_bps = contract.variables["commission_bps"]
            slippage_bps = contract.variables["slippage_bps"]
            spread_bps = contract.variables["spread_bps"]
            stress_slippage_bps = contract.variables["stress_slippage_bps"]
            stress_spread_bps = contract.variables["stress_spread_bps"]
            price_basis = contract.variables["price_basis"]
            dataset_path = contract.data.dataset_path
            baseline_config = contract.baseline_config["genome"]
            treatment_config = contract.treatment_config["genome"]
        except (KeyError, TypeError) as exc:
            return failed(f"Missing required configuration in contract: {exc}")
        if dataset_path is None:
            return failed("Missing required configuration: data.dataset_path")
        if (
            isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 1
            or isinstance(evaluation_start_index, bool)
            or not isinstance(evaluation_start_index, int)
            or evaluation_start_index < 40
        ):
            return failed(
                "Oracle experiments require a warmup and a predeclared evaluation_start_index "
                "of at least 40 bars"
            )
        for name, value in (("commission_bps", commission_bps), ("slippage_bps", slippage_bps),
                            ("spread_bps", spread_bps), ("stress_slippage_bps", stress_slippage_bps),
                            ("stress_spread_bps", stress_spread_bps)):
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 10000
            ):
                return failed(
                    f"Oracle experiments require finite {name} in [0, 10000] basis points"
                )
        if price_basis not in {"mid", "bid", "ask"}:
            return failed("Oracle spread evaluation requires OHLC explicitly identified as mid, bid, or ask prices")
        if (stress_slippage_bps < slippage_bps or stress_spread_bps < spread_bps
                or (stress_slippage_bps == slippage_bps and stress_spread_bps == spread_bps)):
            return failed("Oracle stress costs must be no lower than base costs and worsen at least one execution cost")

        validation_error = self._validate_ablation_targets(contract.ablation_targets)
        if validation_error:
            return failed(validation_error)

        try:
            series = self._data_loader(dataset_path)
            bars = getattr(series, "bars", None)
            if not isinstance(bars, list) or evaluation_start_index >= len(bars) - 30:
                return failed(
                    "Oracle out-of-sample suffix must contain at least 30 bars after its "
                    "predeclared evaluation_start_index"
                )
            effective_warmup = max(warmup, evaluation_start_index)
            development_fingerprint = self._fingerprint_bars(bars[:evaluation_start_index])
            evaluation_fingerprint = self._fingerprint_bars(bars[evaluation_start_index:])
            baseline_genome = self._build_genome(baseline_config, [])
            treatment_genome = self._build_genome(treatment_config, contract.ablation_targets)
        except Exception as exc:
            return failed(f"Oracle setup failed: {exc}")

        try:
            baseline_result = self._run_backtest(
                series, baseline_genome, effective_warmup, threshold,
                commission_bps, slippage_bps, spread_bps, price_basis,
            )
            if baseline_result.get("status") != "complete":
                return failed(
                    f"Baseline backtest failed: {baseline_result.get('message')}",
                    {"baseline_full_result": baseline_result},
                )
            treatment_result = self._run_backtest(
                series, treatment_genome, effective_warmup, threshold,
                commission_bps, slippage_bps, spread_bps, price_basis,
            )
            if treatment_result.get("status") != "complete":
                return failed(
                    f"Treatment backtest failed: {treatment_result.get('message')}",
                    {"baseline_full_result": baseline_result, "treatment_full_result": treatment_result},
                )
            baseline_stress_result = self._run_backtest(
                series, baseline_genome, effective_warmup, threshold,
                commission_bps, stress_slippage_bps, stress_spread_bps, price_basis,
            )
            treatment_stress_result = self._run_backtest(
                series, treatment_genome, effective_warmup, threshold,
                commission_bps, stress_slippage_bps, stress_spread_bps, price_basis,
            )
            if (baseline_stress_result.get("status") != "complete"
                    or treatment_stress_result.get("status") != "complete"):
                return failed("Oracle stress backtest failed", {
                    "baseline_full_result": baseline_result,
                    "treatment_full_result": treatment_result,
                    "baseline_stress_result": baseline_stress_result,
                    "treatment_stress_result": treatment_stress_result,
                })
            baseline_returns = baseline_result.get("bar_returns")
            treatment_returns = treatment_result.get("bar_returns")
            bootstrap = bootstrap_paired_bar_return_metrics(
                baseline_returns,
                treatment_returns,
                seed=contract.environment.random_seed,
                replicates=contract.evaluation.bootstrap_replicates,
                block_length=contract.evaluation.bootstrap_block_length,
            )
            baseline_period_metrics = summarize_bar_return_metrics(baseline_returns)
            treatment_period_metrics = summarize_bar_return_metrics(treatment_returns)
            baseline_metrics = self._extract_metrics(
                {**baseline_result, **baseline_period_metrics},
                contract.evaluation.metric_specs,
            )
            treatment_metrics = self._extract_metrics(
                {**treatment_result, **treatment_period_metrics},
                contract.evaluation.metric_specs,
            )
        except (ValueError, TypeError, AttributeError) as exc:
            return failed(
                f"Metric validation failed: {exc}",
                {"baseline_full_result": locals().get("baseline_result"),
                 "treatment_full_result": locals().get("treatment_result")},
            )
        except Exception as exc:
            return failed(f"Backtest execution failed: {exc}")

        return ExperimentResult(
            run_id=run.run_id,
            experiment_id=contract.experiment_id,
            status=ExperimentStatus.COMPLETED,
            metrics={
                "baseline": baseline_metrics,
                "treatment": treatment_metrics,
                "bootstrap": {
                    "method": bootstrap["method"],
                    "replicates": bootstrap["replicates"],
                    "sample_count": bootstrap["sample_count"],
                    "block_length": bootstrap["block_length"],
                    "random_seed": bootstrap["random_seed"],
                    "baseline": bootstrap["baseline"],
                    "treatment": bootstrap["treatment"],
                },
            },
            evidence={
                "baseline_full_result": baseline_result,
                "treatment_full_result": treatment_result,
                "baseline_stress_result": baseline_stress_result,
                "treatment_stress_result": treatment_stress_result,
                "dataset_fingerprint": contract.data.data_fingerprint,
                "random_seed": contract.environment.random_seed,
                "environment_fingerprint": run.environment_fingerprint,
                "ablation_targets_applied": list(contract.ablation_targets),
                "evaluation_partition": {
                    "method": "chronological_suffix_holdout_v1",
                    "start_index": evaluation_start_index,
                    "start_timestamp": bars[evaluation_start_index].ts,
                    "development_prefix_fingerprint": development_fingerprint,
                    "evaluation_suffix_fingerprint": evaluation_fingerprint,
                    "holdout_fingerprint": evaluation_fingerprint,
                },
                "fairness_check": {
                    "warmup": effective_warmup,
                    "threshold": threshold,
                    "dataset": dataset_path,
                    "evaluation_partition_method": "chronological_suffix_holdout_v1",
                    "evaluation_start_index": evaluation_start_index,
                    "evaluation_start_timestamp": bars[evaluation_start_index].ts,
                    "development_bar_count": evaluation_start_index,
                    "evaluation_bar_count": len(bars) - evaluation_start_index,
                    "development_prefix_fingerprint": development_fingerprint,
                    "evaluation_suffix_fingerprint": evaluation_fingerprint,
                    "paired_observation_unit": "market_bar_mark_to_market_return",
                    "baseline_and_treatment_use_same_circular_blocks": True,
                    "signal_information_cutoff": "completed_bar_close",
                    "entry_fill_policy": "next_bar_open",
                    "gap_stop_or_target_fill_policy": "current_bar_open",
                    "same_bar_stop_target_policy": "stop_first",
                    "commission_bps_per_side": float(commission_bps),
                    "slippage_bps_per_side": float(slippage_bps),
                    "spread_bps_full": float(spread_bps),
                    "stress_slippage_bps_per_side": float(stress_slippage_bps),
                    "stress_spread_bps_full": float(stress_spread_bps),
                    "spread_model": "full spread used once to derive the opposite executable quote from the supplied price basis",
                    "price_basis": price_basis,
                    "timestamp_order_policy": "strictly_increasing_iso8601_when_column_present",
                    "bootstrap_method": bootstrap["method"],
                    "bootstrap_block_length": bootstrap["block_length"],
                    "bootstrap_seed": bootstrap["random_seed"],
                },
            },
        )

    def _validate_ablation_targets(self, targets: List[str]) -> Optional[str]:
        unknown = [target for target in targets if target not in self._manifest.components]
        if not unknown:
            return None
        requested = set(targets)
        if any(set(group.component_ids) == requested for group in self._manifest.approved_groups):
            return None
        return f"Unknown or unapproved ablation target: {unknown[0]}"

    @staticmethod
    def _fingerprint_bars(bars) -> str:
        payload = [
            {
                "ts": bar.ts,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _build_genome(self, genome_dict: Dict[str, Any], ablation_targets: List[str]):
        genome = self._Genome.from_dict(copy.deepcopy(genome_dict))
        for target in ablation_targets:
            declaration = self._manifest.components.get(target)
            if declaration is None:
                raise ValueError(f"Unknown ablation target: {target}")
            handler = self._ablation_handlers.get(declaration.disable_capability)
            if handler is None:
                raise ValueError(f"No handler registered for capability: {declaration.disable_capability}")
            handler(genome)
        return genome

    def _run_backtest(self, series, genome, warmup, threshold, commission_bps, slippage_bps,
                      spread_bps, price_basis):
        decision = self._decision_factory(genome, {"warmup": warmup, "threshold": threshold})
        return self._backtester.run(
            series, decision, warmup=warmup, capture_bar_returns=True,
            execution_mode="next_open", commission_bps=commission_bps,
            slippage_bps=slippage_bps, spread_bps=spread_bps,
            price_basis=price_basis, strategy_genome=genome,
        )

    @staticmethod
    def _extract_metrics(backtest_result: Dict[str, Any], metric_specs: Dict[str, Any]) -> Dict[str, float]:
        metrics = {}
        for metric_name in metric_specs:
            if metric_name not in backtest_result:
                raise ValueError(f"Missing required metric declared in contract: {metric_name}")
            value = backtest_result[metric_name]
            if value is None:
                raise ValueError(f"Metric {metric_name} is None")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Metric {metric_name} is not numeric: {value}") from exc
            if not math.isfinite(numeric):
                raise ValueError(f"Metric {metric_name} is non-finite: {numeric}")
            metrics[metric_name] = numeric
        return metrics


def build_default_oracle_experiment_adapter() -> OracleExperimentAdapter:
    """Build an isolated adapter from Oracle's strategy and backtest code.

    This factory never starts OracleAgent or fetches live market data. Contracts
    must point to a fingerprinted local OHLCV CSV file. Paired inference is
    computed over aligned bar-level mark-to-market returns; the metric names
    explicitly carry the ``period_`` prefix to distinguish these estimands
    from Oracle's legacy trade-level metrics.
    """
    from Oracle.core.backtester import Backtester
    from Oracle.core.market_data import Bar, Series
    from Oracle.intelligence.strategy_genome import StrategyGenome
    from Oracle.intelligence.technicals import analyze as analyze_regime
    from Forge.core.ablation import ComponentDeclaration, ComponentManifest, ComponentType
    class _DecisionSeries:
        def __init__(self, closes, highs, lows, symbol):
            self.closes = list(closes)
            self.highs = list(highs)
            self.lows = list(lows)
            self.symbol = symbol

    def load_series(dataset_path: str):
        path = Path(dataset_path)
        required = {"open", "high", "low", "close"}
        bars = []
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise ValueError("Oracle dataset CSV must have a header row")
            headers = {name.strip().lower(): name for name in reader.fieldnames if name}
            missing = sorted(required - headers.keys())
            if missing:
                raise ValueError(f"Oracle dataset is missing OHLC columns: {missing}")
            timestamp_header = next(
                (headers[key] for key in ("timestamp", "time", "date") if key in headers),
                None,
            )
            previous_timestamp = None
            for row_number, row in enumerate(reader, start=2):
                try:
                    timestamp = row.get(timestamp_header) if timestamp_header else None
                    if timestamp_header:
                        raw_timestamp = str(timestamp or "").strip()
                        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                        if parsed.tzinfo is None or parsed.utcoffset() is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        else:
                            parsed = parsed.astimezone(timezone.utc)
                        if previous_timestamp is not None and parsed <= previous_timestamp:
                            raise ValueError("timestamps must be strictly increasing and unique")
                        previous_timestamp = parsed
                        timestamp = parsed.isoformat()
                    bars.append(Bar(
                        ts=str(timestamp or row_number),
                        open=float(row[headers["open"]]),
                        high=float(row[headers["high"]]),
                        low=float(row[headers["low"]]),
                        close=float(row[headers["close"]]),
                        volume=float(row.get(headers.get("volume", ""), 0) or 0),
                    ))
                except (TypeError, ValueError, KeyError) as exc:
                    raise ValueError(f"Invalid Oracle OHLCV row {row_number}: {exc}") from exc
        if len(bars) < 40:
            raise ValueError("Oracle dataset needs at least 40 OHLCV bars")
        for index, bar in enumerate(bars):
            values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            if any(not math.isfinite(value) for value in values):
                raise ValueError(f"Oracle dataset row {index + 2} has a non-finite value")
            if bar.low <= 0 or bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
                raise ValueError(f"Oracle dataset row {index + 2} has invalid OHLC bounds")
            if bar.volume < 0:
                raise ValueError(f"Oracle dataset row {index + 2} has negative volume")
        return Series(symbol="CONTRACT_DATA", bars=bars, source="fingerprinted_csv")

    def make_decision(genome, parameters):
        threshold = parameters["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("Oracle experiment threshold must be numeric")
        threshold = float(threshold)
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Oracle experiment threshold must be finite and between 0 and 1")

        def decide(closes, highs, lows, **kwargs):
            decision_series = _DecisionSeries(closes, highs, lows, "CONTRACT_DATA")
            vote = genome.vote(decision_series)
            if not math.isfinite(vote):
                raise ValueError("Oracle genome produced a non-finite decision score")
            if kwargs.get("current_stop") is not None:
                updated_stop = genome.trade_management.update_stop(
                    closes[-1], kwargs["current_stop"], kwargs["direction"], kwargs["atr_val"]
                )
                return {"updated_stop": updated_stop}
            if not genome.volatility.filter(decision_series):
                return {"call": "hold"}
            regime_info = analyze_regime(decision_series)
            if not genome.market_regime.is_allowed(
                regime_info.get("regime", "unknown"),
                float(regime_info.get("volatility", 0.0)),
            ):
                return {"call": "hold"}
            genome_threshold = float(genome.entry.params.get("threshold", threshold))
            if not math.isfinite(genome_threshold) or not 0 <= genome_threshold <= 1:
                raise ValueError("Oracle genome entry threshold must be finite and between 0 and 1")
            if not genome.entry.should_enter(vote):
                return {"call": "hold"}
            if abs(vote) <= 1e-12:
                return {"call": "hold"}
            return {"call": "buy" if vote > 0 else "sell"}

        return decide

    dependency_paths = [
        inspect.getsourcefile(Backtester),
        inspect.getsourcefile(Bar),
        inspect.getsourcefile(StrategyGenome),
    ]
    components = {
        name: ComponentDeclaration(
            component_id=name, component_type=ComponentType.MODEL_MODULE,
            disable_capability=f"oracle.{name}",
        )
        for name in ("trend", "momentum", "volatility", "entry", "exit", "risk", "position", "trade_management", "market_regime")
    }
    manifest = ComponentManifest(
        agent_name="oracle", domain="ORACLE_STRATEGY", version="oracle_genome_ablation_v1",
        components=components,
    )

    def neutralize(module_name, values):
        def handler(genome):
            module = getattr(genome, module_name)
            module.logic_type = values.get("logic_type", module.logic_type)
            module.params = dict(values.get("params", {}))
        return handler

    handlers = {
        "oracle.trend": neutralize("trend", {"logic_type": "forge_ablation_neutral"}),
        "oracle.momentum": neutralize("momentum", {"logic_type": "forge_ablation_neutral"}),
        "oracle.volatility": neutralize("volatility", {"logic_type": "default", "params": {}}),
        "oracle.entry": neutralize("entry", {"params": {"threshold": 0.0}}),
        "oracle.exit": neutralize("exit", {"params": {"sl_mult": 2.0, "tp_mult": 3.0}}),
        "oracle.risk": neutralize("risk", {"params": {"max_dd_limit": 1.0}}),
        "oracle.position": neutralize("position", {"params": {"risk_fraction": 0.01}}),
        "oracle.trade_management": neutralize("trade_management", {"logic_type": "default", "params": {}}),
        "oracle.market_regime": neutralize("market_regime", {"params": {"allowed_regimes": ["trending_up", "trending_down", "ranging", "high_volatility"], "volatility_limit": 1.0}}),
    }

    return OracleExperimentAdapter(
        backtester=Backtester(),
        genome_class=StrategyGenome,
        decision_factory=make_decision,
        manifest=manifest,
        ablation_handlers=handlers,
        data_loader=load_series,
        provenance_files=[path for path in dependency_paths if path],
    )
