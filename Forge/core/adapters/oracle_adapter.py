"""Bridge Forge experiments to Oracle's existing strategy backtester."""

import copy
import csv
import hashlib
import inspect
import json
import math
from bisect import bisect_right
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
        policy_trainer: Optional[Callable] = None,
    ):
        self._backtester = backtester
        self._Genome = genome_class
        self._decision_factory = decision_factory
        self._manifest = manifest
        self._ablation_handlers = ablation_handlers
        self._data_loader = data_loader
        self.provenance_files = list(provenance_files or ())
        self._policy_trainer = policy_trainer

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
            timeframe_datasets = contract.variables.get("timeframe_datasets")
            if not isinstance(timeframe_datasets, dict) or set(timeframe_datasets) != {
                "W1", "D1", "H4", "H1", "M45", "M30", "M15",
            }:
                return failed("Oracle top-down experiments require all seven timeframe datasets")
            timeframe_series = {
                period: self._data_loader(path)
                for period, path in timeframe_datasets.items()
            }
            for period, context_series in timeframe_series.items():
                if not isinstance(getattr(context_series, "bars", None), list) or len(context_series.bars) < 40:
                    return failed(f"Oracle {period} timeframe dataset is incomplete")
            if self._fingerprint_bars(timeframe_series["M15"].bars) != self._fingerprint_bars(bars):
                return failed("M15 execution dataset does not match the declared top-down dataset")
            if self._policy_trainer is None:
                return failed("Forge timeframe policy trainer is unavailable")
            entry_threshold = float(baseline_genome.entry.params.get(
                "threshold", baseline_genome.entry.params.get("base_threshold", threshold),
            ))
            baseline_timeframe_policy = self._policy_trainer(
                baseline_genome, timeframe_series, evaluation_start_index, entry_threshold,
                commission_bps, slippage_bps, spread_bps,
            )
            if contract.ablation_targets:
                # Keep the learned context policy fixed during a component
                # ablation so the changed component is the treatment.
                treatment_timeframe_policy = copy.deepcopy(baseline_timeframe_policy)
            else:
                treatment_threshold = float(treatment_genome.entry.params.get(
                    "threshold", treatment_genome.entry.params.get("base_threshold", threshold),
                ))
                treatment_timeframe_policy = self._policy_trainer(
                    treatment_genome, timeframe_series, evaluation_start_index, treatment_threshold,
                    commission_bps, slippage_bps, spread_bps,
                )
        except Exception as exc:
            return failed(f"Oracle setup or Forge timeframe training failed: {exc}")

        try:
            baseline_result = self._run_backtest(
                series, baseline_genome, effective_warmup, threshold,
                commission_bps, slippage_bps, spread_bps, price_basis, timeframe_series, baseline_timeframe_policy,
            )
            if baseline_result.get("status") != "complete":
                return failed(
                    f"Baseline backtest failed: {baseline_result.get('message')}",
                    {"baseline_full_result": baseline_result},
                )
            treatment_result = self._run_backtest(
                series, treatment_genome, effective_warmup, threshold,
                commission_bps, slippage_bps, spread_bps, price_basis, timeframe_series, treatment_timeframe_policy,
            )
            if treatment_result.get("status") != "complete":
                return failed(
                    f"Treatment backtest failed: {treatment_result.get('message')}",
                    {"baseline_full_result": baseline_result, "treatment_full_result": treatment_result},
                )
            baseline_stress_result = self._run_backtest(
                series, baseline_genome, effective_warmup, threshold,
                commission_bps, stress_slippage_bps, stress_spread_bps, price_basis, timeframe_series, baseline_timeframe_policy,
            )
            treatment_stress_result = self._run_backtest(
                series, treatment_genome, effective_warmup, threshold,
                commission_bps, stress_slippage_bps, stress_spread_bps, price_basis, timeframe_series, treatment_timeframe_policy,
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
                "learned_timeframe_policies": {
                    "baseline": baseline_timeframe_policy,
                    "treatment": treatment_timeframe_policy,
                },
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
                    "timeframe_periods": ["W1", "D1", "H4", "H1", "M45", "M30", "M15"],
                    "timeframe_decision_policy": "forge_learned_ridge_policy_v1",
                    "timeframe_policy_training": "development_prefix_only_with_chronological_validation",
                    "higher_timeframe_information_cutoff": "bar_close_at_or_before_M15_signal_close",
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
                      spread_bps, price_basis, timeframe_series, timeframe_policy):
        decision = self._decision_factory(genome, {
            "warmup": warmup, "threshold": threshold,
            "timeframe_series": timeframe_series,
            "timeframe_policy": timeframe_policy,
        })
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
    from Forge.core.timeframe_policy import fit_timeframe_policy, score_timeframe_policy
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

        timeframe_series = parameters.get("timeframe_series", {})
        timeframe_policy = parameters.get("timeframe_policy")
        timeframe_end_times = {}
        timeframe_intervals = {
            "W1": 7 * 24 * 60, "D1": 24 * 60, "H4": 4 * 60,
            "H1": 60, "M45": 45, "M30": 30, "M15": 15,
        }
        module_periods = []
        for module_name in ("market_regime", "trend", "momentum", "volatility", "entry",
                            "exit", "risk", "position", "trade_management"):
            module = getattr(genome, module_name, None)
            params = getattr(module, "params", {}) or {}
            if isinstance(params, dict):
                module_periods.extend(
                    value for key, value in params.items()
                    if key.lower() in {"period", "fast", "slow", "signal", "k_period", "d_period"}
                    and isinstance(value, int) and not isinstance(value, bool) and value > 0
                )
        context_window = max(100, min(2000, max(module_periods or [50]) + 50))
        cached_votes = {}

        for period, context_series in timeframe_series.items():
            ends = []
            for bar in context_series.bars:
                opened = datetime.fromisoformat(str(bar.ts).replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                ends.append(opened.timestamp() + timeframe_intervals[period] * 60)
            timeframe_end_times[period] = ends

        def decide(closes, highs, lows, **kwargs):
            decision_series = _DecisionSeries(
                closes[-context_window:], highs[-context_window:], lows[-context_window:],
                "CONTRACT_DATA_M15",
            )
            if kwargs.get("current_stop") is not None:
                updated_stop = genome.trade_management.update_stop(
                    closes[-1], kwargs["current_stop"], kwargs["direction"], kwargs["atr_val"]
                )
                return {"updated_stop": updated_stop}
            # A signal is evaluated after the current M15 bar closes. For each
            # context timeframe, select only bars whose close is no later than
            # that decision instant; this prevents higher-timeframe lookahead.
            execution_index = len(closes) - 1
            execution_series = timeframe_series.get("M15")
            signal_close = None
            if execution_series is not None and execution_index < len(execution_series.bars):
                signal_open = datetime.fromisoformat(
                    str(execution_series.bars[execution_index].ts).replace("Z", "+00:00")
                )
                if signal_open.tzinfo is None:
                    signal_open = signal_open.replace(tzinfo=timezone.utc)
                signal_close = signal_open.timestamp() + 15 * 60
            if signal_close is None:
                return {"call": "hold"}

            votes = {}
            for period, context_series in timeframe_series.items():
                if period == "M15":
                    end_index = execution_index + 1
                    context_closes = closes[-context_window:]
                    context_highs = highs[-context_window:]
                    context_lows = lows[-context_window:]
                else:
                    end_index = bisect_right(timeframe_end_times[period], signal_close)
                    if end_index < 40:
                        return {"call": "hold"}
                    context_bars = context_series.bars[max(0, end_index - context_window):end_index]
                    context_closes = [bar.close for bar in context_bars]
                    context_highs = [bar.high for bar in context_bars]
                    context_lows = [bar.low for bar in context_bars]
                if cached_votes.get(period, (None, None))[0] == end_index:
                    votes[period] = cached_votes[period][1]
                    continue
                context = _DecisionSeries(context_closes, context_highs, context_lows,
                                          f"CONTRACT_DATA_{period}")
                current_vote = float(genome.vote(context))
                if not math.isfinite(current_vote):
                    raise ValueError(f"Oracle genome produced a non-finite {period} decision score")
                votes[period] = current_vote
                cached_votes[period] = (end_index, current_vote)

            if not isinstance(timeframe_policy, dict):
                raise ValueError("Forge learned timeframe policy is required for Oracle evaluation")
            vote = score_timeframe_policy(timeframe_policy, votes)

            if not genome.volatility.filter(decision_series):
                return {"call": "hold"}
            regime_info = analyze_regime(decision_series)
            regime = regime_info.get("regime", {}) if isinstance(regime_info, dict) else {}
            regime_name = regime.get("regime", "unknown") if isinstance(regime, dict) else "unknown"
            regime_volatility = regime.get("volatility", 0.0) if isinstance(regime, dict) else 0.0
            if not genome.market_regime.is_allowed(
                regime_name,
                float(regime_volatility),
            ):
                return {"call": "hold"}
            if not genome.entry.should_enter(vote):
                return {"call": "hold"}
            if abs(vote) <= 1e-12:
                return {"call": "hold"}
            return {"call": "buy" if vote > 0 else "sell"}

        # Oracle's backtester can pass zero-copy history prefixes to this
        # decision callback; it only reads the trailing context window and
        # uses the prefix length as the global M15 index.
        decide.supports_prefix_views = True
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
        policy_trainer=fit_timeframe_policy,
    )
