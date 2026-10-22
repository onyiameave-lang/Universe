"""Forge-owned first-pass Oracle strategy research workflow.

This module only evaluates an isolated candidate. It never changes Oracle's
champion. Chronicle is the durable record boundary; active request coordination
is held in Forge memory.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import random
import tempfile
import threading
import uuid
from datetime import datetime
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

    @staticmethod
    def _persist_topdown_dataset(symbol: str, bars_by_timeframe: Dict[str, list],
                                 fingerprints: Dict[str, str], coverage: Dict[str, Any],
                                 combined_fingerprint: str, data_as_of: str) -> Dict[str, Any]:
        """Persist the replayable OHLCV bundle inside Chronicle's data area.

        GitHub Actions checks out the research branch for each fresh runner. The
        stable per-symbol/timeframe files are updated atomically and the
        content-addressed manifest ties every experiment to the exact bar set.
        Git history preserves earlier file snapshots until Chronicle's external
        dataset store is available.
        """
        import re

        safe_symbol = str(symbol).strip().upper()
        if not re.fullmatch(r"[A-Z0-9._-]{1,24}", safe_symbol):
            return {"status": "blocked", "message": "symbol is not safe for a Chronicle dataset path"}
        root = Path(__file__).resolve().parents[2] / "Chronicle" / "research" / "oracle_market_data"
        fieldnames = ("timestamp", "open", "high", "low", "close", "volume")
        dataset_paths = {}
        try:
            for period, bars in bars_by_timeframe.items():
                directory = root / safe_symbol
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"{period}.csv"
                buffer = io.StringIO(newline="")
                writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(bars)
                encoded = buffer.getvalue().encode("utf-8")
                if not target.exists() or target.read_bytes() != encoded:
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="wb", dir=directory, prefix=f".{period}.", suffix=".tmp", delete=False
                        ) as output:
                            temporary = Path(output.name)
                            output.write(encoded)
                            output.flush()
                        temporary.replace(target)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                dataset_paths[period] = str(target.relative_to(root.parent.parent.parent)).replace("\\", "/")
        except OSError as exc:
            return {"status": "blocked", "message": f"Chronicle dataset write failed: {type(exc).__name__}"}

        manifest = {
            "schema": "oracle.topdown_dataset_manifest.v1",
            "symbol": safe_symbol,
            "history_target": "5y",
            "source": "ctrader_open_api_bid",
            "price_basis": "bid",
            "data_as_of": data_as_of,
            "data_fingerprint": combined_fingerprint,
            "timeframe_fingerprints": fingerprints,
            "timeframe_coverage": coverage,
            "datasets": dataset_paths,
            "storage_backend": "chronicle_research_branch_snapshot",
            "future_backend_target": "chronicle_approved_dataset_store",
        }
        manifest_fingerprint = hashlib.sha256(json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        manifest["manifest_fingerprint"] = manifest_fingerprint
        manifest_dir = root / "manifests"
        encoded_manifest = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = manifest_dir / f"{safe_symbol}-{combined_fingerprint}.json"
            if not manifest_path.exists() or manifest_path.read_bytes() != encoded_manifest:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=manifest_dir, prefix=".manifest.", suffix=".tmp", delete=False
                    ) as output:
                        temporary = Path(output.name)
                        output.write(encoded_manifest)
                        output.flush()
                    temporary.replace(manifest_path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        except OSError as exc:
            return {"status": "blocked", "message": f"Chronicle dataset manifest write failed: {type(exc).__name__}"}
        return {
            "status": "complete", "data_fingerprint": combined_fingerprint,
            "manifest_fingerprint": manifest_fingerprint,
            "manifest_path": str(manifest_path.relative_to(root.parent.parent.parent)).replace("\\", "/"),
            "datasets": dataset_paths,
        }

    def sync_strategy_catalog(self, catalog: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an immutable Oracle strategy snapshot and pointer in Chronicle.

        Forge keeps no durable catalog copy: its working set is reconstructed
        from Chronicle after restart. Identical snapshots are idempotent.
        """
        if not isinstance(catalog, dict) or catalog.get("schema") != "oracle.strategy_catalog.snapshot.v1":
            return {"status": "error", "message": "a supported Oracle strategy catalog snapshot is required"}
        fingerprint = catalog.get("catalog_fingerprint")
        records = catalog.get("records")
        templates = catalog.get("family_templates")
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or not isinstance(records, list) or not isinstance(templates, list)):
            return {"status": "error", "message": "strategy catalog snapshot is incomplete"}
        try:
            import hashlib
            canonical = {key: value for key, value in catalog.items()
                         if key not in {"catalog_fingerprint", "record_count",
                                        "family_template_count", "source_error_count"}}
            encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
            actual_fingerprint = hashlib.sha256(encoded).hexdigest()
        except (TypeError, ValueError):
            return {"status": "error", "message": "strategy catalog contains non-canonical values"}
        if fingerprint != actual_fingerprint:
            return {"status": "error", "message": "strategy catalog fingerprint does not match its contents"}
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}

        version_key = f"forge.oracle.strategy_catalog.version.{fingerprint}"
        current_key = "forge.oracle.strategy_catalog.current"
        version_state = chronicle.act("state.get", {"key": version_key, "_sender": "forge"})
        if not isinstance(version_state, dict) or version_state.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle strategy catalog lookup failed"}
        if not version_state.get("found"):
            saved = chronicle.act("state.set", {
                "key": version_key, "value": catalog,
                "expected_revision": 0, "_sender": "forge",
            })
            if not isinstance(saved, dict) or saved.get("status") != "complete":
                # A concurrent identical sync is safe; confirm that it won.
                version_state = chronicle.act("state.get", {"key": version_key, "_sender": "forge"})
                existing = version_state.get("value") if isinstance(version_state, dict) else None
                if not isinstance(existing, dict) or existing.get("catalog_fingerprint") != fingerprint:
                    return {"status": "blocked", "message": "Chronicle could not persist the strategy catalog version"}

        current_state = chronicle.act("state.get", {"key": current_key, "_sender": "forge"})
        if not isinstance(current_state, dict) or current_state.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle strategy catalog pointer lookup failed"}
        current_value = current_state.get("value") if current_state.get("found") else None
        if isinstance(current_value, dict) and current_value.get("catalog_fingerprint") == fingerprint:
            return {
                "status": "complete", "catalog_fingerprint": fingerprint,
                "record_count": len(records), "family_template_count": len(templates),
                "already_current": True, "chronicle_key": version_key,
            }
        pointer_value = {
            "schema": "forge.oracle.strategy_catalog.pointer.v1",
            "catalog_fingerprint": fingerprint,
            "version_key": version_key,
            "record_count": len(records),
            "family_template_count": len(templates),
        }
        updated = chronicle.act("state.set", {
            "key": current_key, "value": pointer_value,
            "expected_revision": current_state.get("revision", 0), "_sender": "forge",
        })
        if not isinstance(updated, dict) or updated.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle could not update the current strategy catalog pointer"}
        return {
            "status": "complete", "catalog_fingerprint": fingerprint,
            "record_count": len(records), "family_template_count": len(templates),
            "already_current": False, "chronicle_key": version_key,
        }

    def record_demo_outcome(self, outcome: Dict[str, Any]) -> Dict[str, Any]:
        """Persist idempotent demo feedback against one strategy/symbol/regime."""
        if not isinstance(outcome, dict) or outcome.get("schema") != "oracle.strategy.demo_outcome.v1":
            return {"status": "error", "message": "a supported Oracle demo outcome is required"}
        strategy_id = str(outcome.get("strategy_id", "")).strip()
        symbol = str(outcome.get("symbol", "")).strip().upper()
        regime = str(outcome.get("regime", "")).strip()
        close_event_id = str(outcome.get("close_event_id", "")).strip()
        try:
            pnl_r = float(outcome["pnl_r"])
            closed_at_unix = float(outcome.get("closed_at_unix", 0.0))
        except (KeyError, TypeError, ValueError):
            return {"status": "error", "message": "demo outcome pnl_r must be finite"}
        if (not strategy_id or len(strategy_id) > 160 or not symbol or len(symbol) > 32
                or not regime or len(regime) > 64 or not close_event_id
                or len(close_event_id) > 240 or not math.isfinite(pnl_r)
                or not math.isfinite(closed_at_unix)):
            return {"status": "error", "message": "demo outcome identity or pnl_r is invalid"}
        if (outcome.get("execution_environment") != "demo"
                or outcome.get("feedback_source") not in {
                    "oracle_demo_execution", "oracle_v1_demo_import",
                }):
            return {"status": "blocked", "message": "only identified demo outcomes may update strategy fitness evidence"}
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}

        scope = f"{strategy_id}::{symbol}::{regime}"
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        event_hash = hashlib.sha256(close_event_id.encode("utf-8")).hexdigest()
        event_key = f"forge.oracle.demo_trade.{event_hash}"
        aggregate_key = f"forge.oracle.strategy_feedback.{scope_hash}"
        event_record = {
            **outcome, "strategy_scope_hash": scope_hash,
            "schema": "forge.oracle.demo_trade_outcome.v1",
            "strategy_id": strategy_id, "symbol": symbol, "regime": regime,
            "pnl_r": pnl_r, "observed_at_unix": closed_at_unix,
        }
        saved_event = chronicle.act("state.set", {
            "key": event_key, "value": event_record, "expected_revision": 0,
            "_sender": "forge",
        })
        if not isinstance(saved_event, dict) or saved_event.get("status") != "complete":
            existing = chronicle.act("state.get", {"key": event_key, "_sender": "forge"})
            existing_value = existing.get("value") if isinstance(existing, dict) else None
            if not isinstance(existing_value, dict) or existing_value.get("close_event_id") != close_event_id:
                return {"status": "blocked", "message": "Chronicle could not persist the demo trade event"}

        for _ in range(5):
            current = chronicle.act("state.get", {"key": aggregate_key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete":
                return {"status": "blocked", "message": "Chronicle strategy feedback lookup failed"}
            value = current.get("value") if current.get("found") else {
                "schema": "forge.oracle.strategy_demo_feedback.v1",
                "strategy_id": strategy_id, "symbol": symbol, "regime": regime,
                "trades": 0, "wins": 0, "losses": 0, "total_pnl_r": 0.0,
                "negative_r_total": 0.0, "seen_close_event_ids": [],
                "cost_normalized_trades": 0,
            }
            if not isinstance(value, dict):
                return {"status": "blocked", "message": "Chronicle strategy feedback record is malformed"}
            seen = list(value.get("seen_close_event_ids", []))
            duplicate = close_event_id in seen
            if not duplicate:
                value["trades"] = int(value.get("trades", 0)) + 1
                value["wins"] = int(value.get("wins", 0)) + int(pnl_r > 0)
                value["losses"] = int(value.get("losses", 0)) + int(pnl_r <= 0)
                value["total_pnl_r"] = float(value.get("total_pnl_r", 0.0)) + pnl_r
                value["negative_r_total"] = float(value.get("negative_r_total", 0.0)) + max(0.0, -pnl_r)
                cost_normalized = outcome.get("cost_status") == "verified_net"
                value["cost_normalized_trades"] = int(value.get("cost_normalized_trades", 0)) + int(cost_normalized)
                value["pnl_basis"] = str(outcome.get("pnl_basis", "unspecified_R_basis"))
                value["cost_status"] = (
                    "verified_net" if value["cost_normalized_trades"] == value["trades"]
                    else "unavailable_provisional"
                )
                seen.append(close_event_id)
                value["seen_close_event_ids"] = seen
                n = value["trades"]
                mean_r = value["total_pnl_r"] / n
                value.update({
                    "mean_pnl_r": mean_r,
                    "demo_penalty_r": max(0.0, -mean_r) * math.sqrt(n / (n + 10.0)),
                    "confidence_status": (
                        "observed" if n >= 10 and value["cost_status"] == "verified_net"
                        else "provisional"
                    ),
                    "minimum_trades_for_status": 10,
                    "last_close_event_id": close_event_id,
                    "last_closed_at_unix": event_record["observed_at_unix"],
                })
            stored = chronicle.act("state.set", {
                "key": aggregate_key, "value": value,
                "expected_revision": current.get("revision", 0), "_sender": "forge",
            })
            if isinstance(stored, dict) and stored.get("status") == "complete":
                return {
                    "status": "complete", "strategy_id": strategy_id,
                    "symbol": symbol, "regime": regime, "trades": value["trades"],
                    "mean_pnl_r": value.get("mean_pnl_r"),
                    "demo_penalty_r": value.get("demo_penalty_r", 0.0),
                    "confidence_status": value.get("confidence_status", "provisional"),
                    "duplicate": duplicate,
                    "chronicle_key": aggregate_key,
                }
        return {"status": "blocked", "message": "Chronicle strategy feedback changed concurrently; retry the outcome"}

    @staticmethod
    def _persist_learned_timeframe_policies(chronicle: Any, original: Dict[str, Any],
                                             update: Dict[str, Any]) -> Dict[str, Any]:
        result = update.get("result") or {}
        evidence = result.get("evidence") or {}
        policies = evidence.get("learned_timeframe_policies") or {}
        if not isinstance(policies, dict) or not policies:
            return {}
        saved = {}
        candidate = update.get("candidate_genome") or original.get("candidate_genome") or {}
        strategy_ids = {"baseline": (original.get("baseline_genome") or {}).get("genome_id"),
                        "treatment": candidate.get("genome_id")}
        for role, policy in policies.items():
            if not isinstance(policy, dict):
                continue
            strategy_id = strategy_ids.get(role)
            if not strategy_id:
                continue
            record = {
                "schema": "forge.oracle.strategy_timeframe_policy.v1",
                "policy": policy, "strategy_id": strategy_id,
                "symbol": original.get("symbol"), "regime": original.get("regime"),
                "experiment_id": original.get("experiment_id"), "policy_role": role,
                "dataset_fingerprint": original.get("dataset_fingerprint"),
                "fit_boundary": "development_prefix_only; holdout excluded",
                "promotion_performed": False,
            }
            fingerprint = hashlib.sha256(json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")).hexdigest()
            version_key = f"forge.oracle.timeframe_policy.version.{fingerprint}"
            existing = chronicle.act("state.get", {"key": version_key, "_sender": "forge"})
            if not isinstance(existing, dict) or existing.get("status") != "complete":
                continue
            if not existing.get("found"):
                version_saved = chronicle.act("state.set", {
                    "key": version_key, "value": {**record, "policy_fingerprint": fingerprint},
                    "expected_revision": 0, "_sender": "forge",
                })
                if not isinstance(version_saved, dict) or version_saved.get("status") != "complete":
                    continue
            pointer_id = hashlib.sha256(
                f"{strategy_id}::{original.get('symbol')}::{original.get('regime')}".encode("utf-8")
            ).hexdigest()
            pointer_key = f"forge.oracle.timeframe_policy.latest_experiment.{pointer_id}"
            pointer = chronicle.act("state.get", {"key": pointer_key, "_sender": "forge"})
            if not isinstance(pointer, dict) or pointer.get("status") != "complete":
                continue
            pointer_value = {"schema": "forge.oracle.timeframe_policy_experiment_pointer.v1",
                              "strategy_id": strategy_id, "symbol": original.get("symbol"),
                              "regime": original.get("regime"), "policy_fingerprint": fingerprint,
                              "version_key": version_key, "experiment_id": original.get("experiment_id")}
            pointed = chronicle.act("state.set", {
                "key": pointer_key, "value": pointer_value,
                "expected_revision": pointer.get("revision", 0), "_sender": "forge",
            })
            if isinstance(pointed, dict) and pointed.get("status") == "complete":
                saved[role] = {"policy_fingerprint": fingerprint, "version_key": version_key,
                               "pointer_key": pointer_key, "strategy_id": strategy_id}
        return saved

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
        from datetime import timedelta

        genome_data = request.get("baseline_genome")
        timeframe_bars = request.get("bars_by_timeframe")
        if not isinstance(genome_data, dict) or not isinstance(genome_data.get("modules"), dict):
            return {"status": "error", "message": "a serialized active Oracle genome is required"}
        periods = {"W1": 7 * 24 * 60, "D1": 24 * 60, "H4": 4 * 60,
                   "H1": 60, "M45": 45, "M30": 30, "M15": 15}
        if not isinstance(timeframe_bars, dict) or set(timeframe_bars) != set(periods):
            return {"status": "blocked", "message": "all seven top-down timeframe datasets are required"}
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

        strategy_scope_hash = hashlib.sha256(
            f"{genome_data.get('genome_id', '')}::{symbol}::{regime}".encode("utf-8")
        ).hexdigest()
        demo_feedback_state = chronicle.act("state.get", {
            "key": f"forge.oracle.strategy_feedback.{strategy_scope_hash}", "_sender": "forge",
        })
        if not isinstance(demo_feedback_state, dict) or demo_feedback_state.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle demo strategy feedback lookup failed"}
        demo_feedback_record = demo_feedback_state.get("value") if demo_feedback_state.get("found") else {
            "trades": 0, "wins": 0, "losses": 0, "mean_pnl_r": None,
            "demo_penalty_r": 0.0, "confidence_status": "no_demo_evidence",
        }
        demo_feedback = {key: demo_feedback_record.get(key) for key in (
            "strategy_id", "symbol", "regime", "trades", "wins", "losses",
            "total_pnl_r", "mean_pnl_r", "demo_penalty_r", "confidence_status",
            "minimum_trades_for_status", "last_closed_at_unix", "cost_status",
            "cost_normalized_trades", "pnl_basis",
        ) if key in demo_feedback_record}

        try:
            from Oracle.intelligence.strategy_genome import StrategyGenome
            from Forge.core.adapters.oracle_adapter import build_default_oracle_experiment_adapter

            baseline = StrategyGenome.from_dict(genome_data)
            canonical_by_timeframe = {
                period: self._normalize_bars(timeframe_bars[period])
                for period in periods
            }
            as_of = datetime.fromisoformat(str(request.get("data_as_of", "")).replace("Z", "+00:00"))
            if as_of.tzinfo is None:
                raise ValueError("timeframe bundle must declare a timezone-aware data_as_of")
            target_start = as_of - timedelta(days=5 * 365)
            parsed_by_timeframe = {}
            for period, items in canonical_by_timeframe.items():
                if len(items) < 40:
                    return {"status": "blocked", "message": f"{period} has fewer than 40 completed bars"}
                parsed = []
                for index, item in enumerate(items):
                    timestamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        raise ValueError(f"{period} bar {index} has no timezone")
                    if parsed and timestamp <= parsed[-1][0]:
                        raise ValueError(f"{period} timestamps must be unique and increasing")
                    parsed.append((timestamp, item))
                coverage_tolerance = max(timedelta(days=4), timedelta(minutes=periods[period] * 3))
                if parsed[0][0] > target_start + coverage_tolerance:
                    return {"status": "blocked", "message": f"{period} does not cover the five-year target"}
                if parsed[-1][0] < as_of - timedelta(days=7):
                    return {"status": "blocked", "message": f"{period} data is stale"}
                parsed_by_timeframe[period] = parsed

            # Keep M15 execution rows within the dates covered by all seven
            # datasets. Higher-timeframe context is then sampled as-of each
            # completed M15 close by the adapter.
            common_start = max(items[0][0] for items in parsed_by_timeframe.values())
            common_end = min(items[-1][0] + timedelta(minutes=periods[period])
                             for period, items in parsed_by_timeframe.items())
            evaluation_by_timeframe = {
                period: [item for timestamp, item in parsed_by_timeframe[period]
                         if timestamp >= common_start
                         and timestamp + timedelta(minutes=periods[period]) <= common_end]
                for period in periods
            }
            canonical_bars = evaluation_by_timeframe["M15"]
            if len(canonical_bars) < 75:
                return {"status": "blocked", "message": "shared timeframe overlap contains fewer than 75 M15 bars"}
            timeframe_fingerprints = {
                period: self._fingerprint(items)
                for period, items in canonical_by_timeframe.items()
            }
            combined_fingerprint = hashlib.sha256(json.dumps(
                {"symbol": symbol.upper(), "period_fingerprints": timeframe_fingerprints,
                 "price_basis": "bid", "schema": "oracle.topdown_ohlcv.v1"},
                sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
            if request.get("data_fingerprint") != combined_fingerprint:
                raise ValueError("top-down bundle fingerprint does not match its timeframe bars")
            dataset_artifact = self._persist_topdown_dataset(
                symbol, canonical_by_timeframe,
                timeframe_fingerprints, request.get("timeframe_coverage", {}),
                combined_fingerprint, str(request.get("data_as_of")),
            )
            if dataset_artifact.get("status") != "complete":
                return {"status": "blocked", "message": dataset_artifact.get("message", "Chronicle dataset persistence failed")}
        except Exception as exc:
            return {"status": "error", "message": f"invalid Oracle research input: {type(exc).__name__}: {exc}"}

        data_fingerprint = combined_fingerprint
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
            "baseline_demo_feedback": demo_feedback,
            "candidate_genome": candidate.to_dict(),
            "dataset_fingerprint": data_fingerprint,
            "execution_dataset_fingerprint": self._fingerprint(canonical_bars),
            "timeframe_fingerprints": timeframe_fingerprints,
            "timeframe_coverage": request.get("timeframe_coverage", {}),
            "history_target": "5y",
            "dataset_manifest": dataset_artifact,
            "shared_overlap": {
                "start": common_start.isoformat(), "end": common_end.isoformat(),
                "execution_period": "M15", "execution_bars": len(canonical_bars),
            },
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
                "commission_source": costs.get("commission_source"),
                "spread_source": costs.get("spread_source"),
                "slippage_source": costs.get("slippage_source"),
                "manual_cost_data_version": costs.get("manual_cost_data_version"),
                "manual_cost_source_period": costs.get("manual_cost_source_period"),
                "manual_cost_sources": costs.get("manual_cost_sources"),
                "manual_cost_broker_symbol": costs.get("manual_cost_broker_symbol"),
                "manual_cost_unit_conversion": costs.get("manual_cost_unit_conversion"),
                "promotion_eligible": costs.get("promotion_eligible", True),
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
                timeframe_datasets = {}
                for period, period_bars in evaluation_by_timeframe.items():
                    dataset_path = Path(directory) / f"ohlcv-{period.lower()}.csv"
                    with dataset_path.open("w", encoding="utf-8", newline="") as output:
                        writer = csv.DictWriter(output, fieldnames=("timestamp", "open", "high", "low", "close", "volume"))
                        writer.writeheader()
                        writer.writerows(period_bars)
                    timeframe_datasets[period] = str(dataset_path)
                dataset_path = Path(timeframe_datasets["M15"])
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
                        "timeframe_datasets": timeframe_datasets,
                        "timeframe_fingerprints": timeframe_fingerprints,
                        "history_target": "5y",
                        "shared_overlap_start": common_start.isoformat(),
                        "shared_overlap_end": common_end.isoformat(),
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
            minimum_sample_met = (
                guardrails["minimum_trade_sample_met"]
                and guardrails["stress_minimum_trade_sample_met"]
            )
            if ablation_targets and not minimum_sample_met:
                recommendation = "ablation_inconclusive_insufficient_trade_sample"
                interpretation = "inconclusive_insufficient_trade_sample"
            elif ablation_targets and recommend:
                recommendation = (
                    "ablation_research_pass_review_only"
                    if costs.get("profile_status") == "published_spread_estimated_slippage_research_only"
                    else "ablation_review_required"
                )
                interpretation = (
                    "research_only_estimated_execution_costs"
                    if costs.get("profile_status") == "published_spread_estimated_slippage_research_only"
                    else "guardrails_passed_requires_separate_review"
                )
            elif ablation_targets:
                recommendation = "ablation_failed_guardrails"
                interpretation = "guardrails_failed"
            elif recommend and costs.get("profile_status") == "published_spread_estimated_slippage_research_only":
                recommendation = "candidate_for_research_review_only"
                interpretation = "research_only_estimated_execution_costs"
            elif recommend:
                recommendation = "candidate_for_governed_review"
                interpretation = "guardrails_passed_requires_separate_review"
            else:
                recommendation = "keep_incumbent"
                interpretation = "guardrails_failed_or_insufficient_evidence"
            return self._finish_record(chronicle, record_key, record, {
                "status": "complete",
                "recommendation": recommendation,
                "interpretation": interpretation,
                "promotion_performed": False,
                "promotion_block_reason": (
                    "manual profile includes modeled slippage and cannot authorize promotion"
                    if costs.get("profile_status") == "published_spread_estimated_slippage_research_only"
                    else None
                ),
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
        persisted_policies = OracleImprovementService._persist_learned_timeframe_policies(
            chronicle, original, update
        )
        final = {**original, **update, "persisted_timeframe_policies": persisted_policies}
        saved = chronicle.act("state.set", {
            "key": record_key, "value": final,
            "expected_revision": current.get("revision"), "_sender": "forge",
        })
        if not isinstance(saved, dict) or saved.get("status") != "complete":
            return {"status": "error", "message": "Chronicle could not persist the final experiment evidence"}
        return {"status": update.get("status", "complete"), "experiment_id": original["experiment_id"],
                "symbol": original["symbol"], "regime": original["regime"],
                "dataset_fingerprint": original.get("dataset_fingerprint"),
                "timeframe_fingerprints": original.get("timeframe_fingerprints"),
                "timeframe_coverage": original.get("timeframe_coverage"),
                "shared_overlap": original.get("shared_overlap"),
                "dataset_manifest": original.get("dataset_manifest"),
                "holdout_fingerprint": original.get("holdout_fingerprint"),
                "recommendation": update.get("recommendation", "none"),
                "promotion_performed": False,
                "guardrails": update.get("guardrails"),
                "interpretation": update.get("interpretation"),
                "trade_sample": update.get("trade_sample"),
                "drawdown_policy_check": update.get("drawdown_policy_check"),
                "candidate_genome": update.get("candidate_genome", original.get("candidate_genome")),
                "evaluation": update.get("evaluation"),
                "result_metrics": (update.get("result") or {}).get("metrics"),
                "learned_timeframe_policies": (
                    (update.get("result") or {}).get("evidence") or {}
                ).get("learned_timeframe_policies"),
                "persisted_timeframe_policies": persisted_policies,
                "baseline_demo_feedback": original.get("baseline_demo_feedback"),
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
