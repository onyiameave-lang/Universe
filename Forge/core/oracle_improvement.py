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
import os
import random
import tempfile
import threading
import time
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

ORACLE_ABLATION_TARGETS = (
    "trend", "momentum", "volatility", "entry", "exit", "risk", "position",
    "trade_management", "market_regime",
)
ORACLE_DIAGNOSTIC_TIMEFRAMES_MS = {
    "W1": 7 * 24 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "M45": 45 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "M15": 15 * 60 * 1000,
}
ORACLE_OPERATIONAL_NO_TRADE_OUTCOMES = {
    "symbol_unmapped", "already_in_position", "max_positions_reached",
    "duplicate_position_detected", "signal_exception", "signal_timeout",
    "signal_error", "execution_rejected", "execution_unavailable",
    "kill_switch_active", "account_risk_check_unavailable",
    "positions_unavailable", "duplicate_broker_mapping", "trading_disabled",
    "market_closed", "signal_cancelled",
}


def _point_in_time_oracle_timeframe_votes(
    genome_payload: Dict[str, Any],
    bars_by_timeframe: Dict[str, list],
    decisions: list,
) -> Dict[str, Any]:
    """Replay the incumbent's raw genome vote using only bars closed by each decision.

    This is diagnostic telemetry, not the Forge-trained timeframe policy and
    not evidence that a trade should have been opened. Bars in the provider
    bundle are keyed by opening time, so each timeframe's interval is added
    before admitting it to a point-in-time view.
    """
    periods = tuple(ORACLE_DIAGNOSTIC_TIMEFRAMES_MS)
    if not isinstance(genome_payload, dict) or not isinstance(bars_by_timeframe, dict):
        return {"status": "unavailable", "reason": "genome_or_market_bundle_missing"}
    if set(bars_by_timeframe) != set(periods):
        return {"status": "unavailable", "reason": "seven_timeframe_bundle_required"}
    try:
        from Oracle.intelligence.strategy_genome import StrategyGenome
        genome = StrategyGenome.from_dict(genome_payload)
    except Exception as exc:
        return {"status": "unavailable", "reason": f"incumbent_genome_invalid:{type(exc).__name__}"}

    class VoteSeries:
        def __init__(self, bars):
            self.closes = [float(bar["close"]) for bar in bars]
            self.highs = [float(bar["high"]) for bar in bars]
            self.lows = [float(bar["low"]) for bar in bars]

    parsed = {}
    for period in periods:
        rows = []
        for bar in bars_by_timeframe[period]:
            try:
                opened = datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    return {"status": "unavailable", "reason": f"{period}_bar_timezone_missing"}
                end_unix = opened.timestamp() + ORACLE_DIAGNOSTIC_TIMEFRAMES_MS[period] / 1000.0
                values = {field: float(bar[field]) for field in ("open", "high", "low", "close")}
                if not all(math.isfinite(value) for value in values.values()):
                    raise ValueError("non-finite OHLC")
                rows.append((end_unix, {**bar, **values}))
            except (KeyError, TypeError, ValueError, OverflowError):
                return {"status": "unavailable", "reason": f"{period}_bar_invalid"}
        rows.sort(key=lambda item: item[0])
        parsed[period] = rows

    params = []
    modules = genome_payload.get("modules", {})
    if isinstance(modules, dict):
        for module_name in ("market_regime", "trend", "momentum", "volatility", "entry",
                            "exit", "risk", "position", "trade_management"):
            module = modules.get(module_name, {})
            module_params = module.get("params", {}) if isinstance(module, dict) else {}
            if isinstance(module_params, dict):
                params.extend(value for key, value in module_params.items()
                              if key.lower() in {"period", "fast", "slow", "signal",
                                                 "k_period", "d_period", "lookback"}
                              and isinstance(value, int) and not isinstance(value, bool)
                              and value > 0)
    context_window = max(100, min(2000, max(params or [50]) + 50))
    event_results = []
    for decision in decisions[-12:]:
        if not isinstance(decision, dict):
            continue
        event_id = decision.get("event_id")
        at_unix = decision.get("at_unix")
        if (not isinstance(at_unix, (int, float)) or isinstance(at_unix, bool)
                or not math.isfinite(float(at_unix)) or float(at_unix) <= 0):
            continue
        votes, frame_counts = {}, {}
        for period in periods:
            completed = [bar for end_unix, bar in parsed[period] if end_unix <= float(at_unix)]
            frame_counts[period] = len(completed)
            # A timeframe vote without enough lookback is unavailable rather
            # than silently computed from a truncated strategy period.
            if len(completed) < context_window:
                continue
            try:
                vote = float(genome.vote(VoteSeries(completed[-context_window:])))
            except Exception:
                continue
            if math.isfinite(vote):
                votes[period] = vote
        event_results.append({
            "event_id": str(event_id)[:160] if event_id is not None else None,
            "evaluated_at_unix": float(at_unix),
            "outcome_code": str(decision.get("outcome_code", ""))[:40],
            "completed_bar_counts": frame_counts,
            "raw_genome_votes": votes,
            "available_timeframes": list(votes),
        })
    if not event_results:
        return {"status": "unavailable", "reason": "no_valid_decision_timestamps"}
    return {
        "schema": "forge.oracle.no_trade_timeframe_context.v1",
        "status": "complete" if any(len(item["raw_genome_votes"]) == len(periods)
                                      for item in event_results) else "partial",
        "strategy_id": str(genome_payload.get("genome_id", ""))[:160],
        "vote_semantics": "active V1 genome raw vote per timeframe; no Forge-learned policy applied",
        "bar_availability_rule": "bar_open_time + timeframe_duration <= Oracle decision time",
        "context_window_bars": context_window,
        "timeframes": list(periods),
        "events": event_results,
        "interpretation": "descriptive replay only; not a live V1 signal, missed-trade claim, or causal attribution",
    }


def _project_oracle_decision_context(raw: Any) -> Dict[str, Any] | None:
    """Allowlist the bounded numeric/provenance context sent from Oracle V1.

    Provider text, articles, posts, memory summaries, and arbitrary nested
    payloads are intentionally not copied into Forge's durable diagnostic
    projection.
    """
    if not isinstance(raw, dict) or raw.get("schema") != "oracle.decision_context.v1":
        return None

    def number(value: Any, low: float | None = None,
               high: float | None = None) -> float | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        value = float(value)
        if not math.isfinite(value):
            return None
        if low is not None and value < low or high is not None and value > high:
            return None
        return value

    projected: Dict[str, Any] = {"schema": "oracle.decision_context.v1"}
    fused_raw = raw.get("fused")
    fused: Dict[str, Any] = {}
    if isinstance(fused_raw, dict):
        if fused_raw.get("call") in {"buy", "sell", "hold"}:
            fused["call"] = fused_raw["call"]
        for field, limits in {
            "direction": (-1.0, 1.0), "confidence": (0.0, 1.0),
            "agreement": (0.0, 1.0), "entry_threshold": (0.0, 1.0),
            "effective_confidence": (0.0, 1.0),
        }.items():
            value = number(fused_raw.get(field), *limits)
            if value is not None:
                fused[field] = value
        weights_raw = fused_raw.get("learned_weights")
        if isinstance(weights_raw, dict):
            weights = {name: value for name in ("technical", "news", "social", "memory")
                       if (value := number(weights_raw.get(name), 0.0, 1.0)) is not None}
            if weights:
                fused["learned_weights"] = weights
    if fused:
        projected["fused"] = fused

    market_raw = raw.get("market")
    market: Dict[str, Any] = {}
    if isinstance(market_raw, dict):
        regime = market_raw.get("regime")
        if isinstance(regime, str) and regime in {
                "trending_up", "trending_down", "ranging", "high_volatility", "unknown"}:
            market["regime"] = regime
        for field in ("last_price", "atr_14", "slope_20", "realized_volatility", "rsi_14"):
            value = number(market_raw.get(field))
            if value is not None:
                market[field] = value
        if isinstance(market_raw.get("high_volatility"), bool):
            market["high_volatility"] = market_raw["high_volatility"]
    if market:
        projected["market"] = market

    streams_raw = raw.get("streams")
    streams: Dict[str, Any] = {}
    if isinstance(streams_raw, dict):
        for name in ("technical", "news", "social", "memory"):
            item_raw = streams_raw.get(name)
            if not isinstance(item_raw, dict):
                continue
            item: Dict[str, Any] = {}
            direction = number(item_raw.get("direction"), -1.0, 1.0)
            confidence = number(item_raw.get("confidence"), 0.0, 1.0)
            if direction is not None:
                item["direction"] = direction
            if confidence is not None:
                item["confidence"] = confidence
            for field, values in {
                "evidence_state": {"available", "degraded", "unknown"},
                "source": {"evolved_champion", "technical", "evolution_library",
                           "scientific_library"},
            }.items():
                if item_raw.get(field) in values:
                    item[field] = item_raw[field]
            for field in ("cross_source", "manipulation_warning"):
                if isinstance(item_raw.get(field), bool):
                    item[field] = item_raw[field]
            count = item_raw.get("evidence_count")
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1_000_000:
                item["evidence_count"] = count
            if item:
                streams[name] = item
    if streams:
        projected["streams"] = streams

    risk_raw = raw.get("risk")
    risk: Dict[str, Any] = {}
    if isinstance(risk_raw, dict):
        if risk_raw.get("mode") in {"paper", "live"}:
            risk["mode"] = risk_raw["mode"]
        floor = number(risk_raw.get("confidence_floor"), 0.0, 1.0)
        if floor is not None:
            risk["confidence_floor"] = floor
    if risk:
        projected["risk"] = risk
    for field in ("news_impact", "social_risk"):
        if raw.get(field) in {"none", "low", "medium", "high", "critical", "unknown"}:
            projected[field] = raw[field]
    return projected


def _summarize_oracle_no_trade_evidence(decisions: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize observed decision context without turning it into a trade rule."""
    call_counts: Dict[str, int] = {}
    regime_counts: Dict[str, int] = {}
    stream_states: Dict[str, Dict[str, int]] = {}
    holds = 0
    threshold_gaps: list[float] = []
    risk_rejections = 0
    confidence_below_floor = 0
    context_events = 0

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        details = decision.get("details")
        context = details.get("decision_context") if isinstance(details, dict) else None
        if not isinstance(context, dict):
            continue
        context_events += 1
        fused = context.get("fused") if isinstance(context.get("fused"), dict) else {}
        call = fused.get("call")
        if call in {"buy", "sell", "hold"}:
            call_counts[call] = call_counts.get(call, 0) + 1
        market = context.get("market") if isinstance(context.get("market"), dict) else {}
        regime = market.get("regime")
        if isinstance(regime, str) and regime:
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
        if decision.get("outcome_code") == "no_setup" and call == "hold":
            holds += 1
            direction = fused.get("direction")
            threshold = fused.get("entry_threshold")
            if (isinstance(direction, (int, float)) and not isinstance(direction, bool)
                    and isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
                    and math.isfinite(float(direction)) and math.isfinite(float(threshold))):
                threshold_gaps.append(float(threshold) - abs(float(direction)))
        if decision.get("outcome_code") == "risk_rejected":
            risk_rejections += 1
            risk = context.get("risk") if isinstance(context.get("risk"), dict) else {}
            effective = fused.get("effective_confidence")
            floor = risk.get("confidence_floor")
            if (isinstance(effective, (int, float)) and not isinstance(effective, bool)
                    and isinstance(floor, (int, float)) and not isinstance(floor, bool)
                    and math.isfinite(float(effective)) and math.isfinite(float(floor))
                    and float(effective) < float(floor)):
                confidence_below_floor += 1
        streams = context.get("streams")
        if isinstance(streams, dict):
            for name in ("technical", "news", "social", "memory"):
                stream = streams.get(name)
                if not isinstance(stream, dict):
                    continue
                state = stream.get("evidence_state", "unknown")
                if state not in {"available", "degraded", "unknown"}:
                    state = "unknown"
                counts = stream_states.setdefault(name, {"available": 0, "degraded": 0, "unknown": 0})
                counts[state] += 1

    result: Dict[str, Any] = {
        "schema": "forge.oracle.no_trade_evidence_summary.v1",
        "decision_count": len(decisions),
        "decision_context_count": context_events,
        "decision_context_coverage": (context_events / len(decisions)) if decisions else 0.0,
        "call_counts": call_counts,
        "regime_counts": regime_counts,
        "hold_decisions_with_entry_threshold": len(threshold_gaps),
        "risk_rejections_with_confidence_below_floor": confidence_below_floor,
        "risk_rejection_count": risk_rejections,
        "stream_evidence_states": stream_states,
        "interpretation": "descriptive telemetry; not causal attribution or a trading signal",
    }
    if threshold_gaps:
        result["mean_hold_threshold_gap"] = sum(threshold_gaps) / len(threshold_gaps)
    return result


class OracleImprovementService:
    """Run one bounded, Forge-owned challenge for a flagged Oracle champion."""

    def __init__(self, forge_agent: Any):
        self._forge = forge_agent
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_inflight: set[str] = set()
        self._diagnostic_runtime_id = uuid.uuid4().hex
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        try:
            self._diagnostic_max_workers = min(8, max(1, int(
                os.getenv("FORGE_ORACLE_DIAGNOSIS_MAX_WORKERS", "2"))))
        except ValueError:
            self._diagnostic_max_workers = 2
        from Forge.core.oracle_research_data import OracleResearchDataProvider
        self._research_data = OracleResearchDataProvider()

    def start_supervisor(self) -> None:
        """Start lightweight recovery and telemetry monitoring for this Forge process."""
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop, name="forge-oracle-supervisor", daemon=True)
        self._supervisor_thread.start()

    def stop_supervisor(self) -> None:
        self._supervisor_stop.set()
        worker = self._supervisor_thread
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

    def _supervisor_loop(self) -> None:
        while not self._supervisor_stop.wait(60.0):
            try:
                self.resume_pending_no_trade_jobs()
                self.check_stale_trader_heartbeats()
            except Exception as exc:
                log.warning("Forge Oracle supervisor pass failed: %s", type(exc).__name__)

    def record_trader_heartbeat(self, heartbeat: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a cTrader trader liveness event without treating it as a decision."""
        if (not isinstance(heartbeat, dict)
                or heartbeat.get("schema") != "oracle.trader.heartbeat.v1"):
            return {"status": "error", "message": "a supported Oracle trader heartbeat is required"}
        event_id = str(heartbeat.get("event_id", "")).strip()
        instance_id = str(heartbeat.get("instance_id", "")).strip()
        try:
            observed_at = float(heartbeat.get("observed_at_unix"))
            encoded = json.dumps(heartbeat, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return {"status": "error", "message": "trader heartbeat must be finite JSON"}
        symbols = heartbeat.get("symbols")
        if (not event_id or len(event_id) > 160 or not instance_id or len(instance_id) > 160
                or not math.isfinite(observed_at) or observed_at <= 0
                or not isinstance(symbols, list) or len(symbols) > 100
                or any(not isinstance(item, str) or not item.strip() for item in symbols)
                or not isinstance(heartbeat.get("summary"), dict) or len(encoded) > 64 * 1024):
            return {"status": "error", "message": "trader heartbeat identity or fields are invalid"}
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}
        event_hash = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        event_key = f"forge.oracle.trader_heartbeat.{event_hash}"
        event_record = {**heartbeat, "received_at_unix": time.time(),
                        "input_fingerprint": hashlib.sha256(encoded).hexdigest(),
                        "source": "oracle_v1_ctrader_demo_trader"}
        existing = chronicle.act("state.get", {"key": event_key, "_sender": "forge"})
        if not isinstance(existing, dict) or existing.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle heartbeat event lookup failed"}
        duplicate = False
        if existing.get("found"):
            prior = existing.get("value")
            if (not isinstance(prior, dict) or prior.get("event_id") != event_id
                    or prior.get("input_fingerprint") != event_record["input_fingerprint"]):
                return {"status": "blocked", "message": "Chronicle could not persist the heartbeat event"}
            duplicate = True
        else:
            saved = chronicle.act("state.set", {
                "key": event_key, "value": event_record,
                "expected_revision": existing.get("revision", 0), "_sender": "forge"})
            if not isinstance(saved, dict) or saved.get("status") != "complete":
                return {"status": "blocked", "message": "Chronicle could not persist the heartbeat event"}
        instance_hash = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
        latest_key = f"forge.oracle.trader_heartbeat.latest.{instance_hash}"
        for _ in range(5):
            current = chronicle.act("state.get", {"key": latest_key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete":
                return {"status": "blocked", "message": "Chronicle heartbeat index lookup failed"}
            prior = current.get("value") if current.get("found") else {}
            if isinstance(prior, dict) and observed_at <= float(prior.get("last_seen_at_unix", 0) or 0):
                return {"status": "complete", "duplicate": duplicate,
                        "late_event": True, "instance_id": instance_id,
                        "heartbeat_count": int(prior.get("heartbeat_count", 0))}
            latest = {
                "schema": "forge.oracle.trader_heartbeat_latest.v1",
                "instance_id": instance_id, "last_event_id": event_id,
                "last_seen_at_unix": observed_at,
                "heartbeat_count": int(prior.get("heartbeat_count", 0)) + 1
                if isinstance(prior, dict) else 1,
                "cycle_interval_sec": heartbeat.get("cycle_interval_sec"),
                "symbols": [item.strip().upper() for item in symbols],
                "cycle": heartbeat.get("cycle"), "summary": heartbeat["summary"],
            }
            stored = chronicle.act("state.set", {
                "key": latest_key, "value": latest,
                "expected_revision": current.get("revision", 0), "_sender": "forge"})
            if isinstance(stored, dict) and stored.get("status") == "complete":
                coverage = self._record_heartbeat_symbol_coverage(
                    chronicle, latest, heartbeat)
                if coverage.get("status") == "blocked":
                    return coverage
                latest["symbol_coverage"] = coverage
                refreshed = chronicle.act("state.get", {
                    "key": latest_key, "_sender": "forge"})
                if isinstance(refreshed, dict) and refreshed.get("status") == "complete":
                    latest_saved = chronicle.act("state.set", {
                        "key": latest_key, "value": latest,
                        "expected_revision": refreshed.get("revision", 0),
                        "_sender": "forge"})
                    if not isinstance(latest_saved, dict) or latest_saved.get("status") != "complete":
                        return {"status": "blocked",
                                "message": "Chronicle could not persist heartbeat symbol coverage"}
                incident_key = f"forge.oracle.trader_heartbeat.incident.{instance_hash}"
                incident = chronicle.act("state.get", {"key": incident_key, "_sender": "forge"})
                if isinstance(incident, dict) and incident.get("status") == "complete" and incident.get("found"):
                    incident_value = incident.get("value")
                    if isinstance(incident_value, dict) and incident_value.get("status") == "stale":
                        incident_value.update({"status": "recovered", "recovered_at_unix": time.time(),
                                               "recovery_event_id": event_id})
                        recovered = chronicle.act("state.set", {
                            "key": incident_key, "value": incident_value,
                            "expected_revision": incident.get("revision", 0), "_sender": "forge"})
                        if isinstance(recovered, dict) and recovered.get("status") == "complete":
                            stale_case_id = "CASE-ORACLE-HEARTBEAT-GAP-" + hashlib.sha256(
                                f"{instance_id}::{int(incident_value.get('last_seen_at_unix', 0))}"
                                .encode("utf-8")).hexdigest()[:20]
                            stale_case_key = "forge.oracle.heartbeat_gap_case." + hashlib.sha256(
                                stale_case_id.encode("utf-8")).hexdigest()
                            stale_case = chronicle.act("state.get", {
                                "key": stale_case_key, "_sender": "forge"})
                            stale_case_value = (stale_case.get("value")
                                                if isinstance(stale_case, dict) else None)
                            if (isinstance(stale_case, dict)
                                    and stale_case.get("status") == "complete"
                                    and stale_case.get("found")
                                    and isinstance(stale_case_value, dict)):
                                stale_case_value.update({
                                    "runtime_status": "recovered",
                                    "recovered_at_unix": incident_value["recovered_at_unix"],
                                    "recovery_event_id": event_id,
                                })
                                chronicle.act("state.set", {
                                    "key": stale_case_key, "value": stale_case_value,
                                    "expected_revision": stale_case.get("revision", 0),
                                    "_sender": "forge"})
                return {"status": "complete", "duplicate": duplicate, "instance_id": instance_id,
                        "heartbeat_count": latest["heartbeat_count"],
                        "symbol_coverage": coverage}
        return {"status": "blocked", "message": "Chronicle heartbeat index changed concurrently"}

    def _record_heartbeat_symbol_coverage(self, chronicle: Any,
                                          latest: Dict[str, Any],
                                          heartbeat: Dict[str, Any]) -> Dict[str, Any]:
        """Track whether each configured symbol produced a decision this cycle.

        Older V1 versions only sent aggregate heartbeat counters. They remain
        useful for liveness but cannot prove per-symbol evaluation coverage.
        """
        raw_outcomes = heartbeat.get("summary", {}).get("per_symbol_outcomes")
        if not isinstance(raw_outcomes, dict):
            return {"status": "telemetry_unavailable",
                    "reason": "per_symbol_outcomes_not_reported",
                    "expected_symbols": latest.get("symbols", [])}
        try:
            minimum_cycles = min(10000, max(1, int(
                os.getenv("FORGE_ORACLE_NO_EVALUATION_MIN_CYCLES", "12"))))
            minimum_hours = min(720.0, max(0.0, float(
                os.getenv("FORGE_ORACLE_NO_EVALUATION_HOURS", "24"))))
        except ValueError:
            minimum_cycles, minimum_hours = 12, 24.0
        evaluated, missing = [], []
        instance_id = str(latest.get("instance_id", ""))
        observed_at = float(latest.get("last_seen_at_unix", time.time()))
        for symbol in sorted({str(item).strip().upper() for item in latest.get("symbols", [])
                              if isinstance(item, str) and item.strip()}):
            raw_counts = raw_outcomes.get(symbol)
            has_decision = (isinstance(raw_counts, dict) and any(
                isinstance(count, int) and not isinstance(count, bool) and count > 0
                for count in raw_counts.values()))
            coverage_hash = hashlib.sha256(
                f"{instance_id}::{symbol}".encode("utf-8")).hexdigest()
            coverage_key = f"forge.oracle.symbol_scan.latest.{coverage_hash}"
            for _ in range(5):
                current = chronicle.act("state.get", {
                    "key": coverage_key, "_sender": "forge"})
                if not isinstance(current, dict) or current.get("status") != "complete":
                    return {"status": "blocked",
                            "message": "Chronicle symbol scan coverage lookup failed"}
                prior = current.get("value") if current.get("found") else {}
                if not isinstance(prior, dict):
                    prior = {}
                if prior.get("last_event_id") == heartbeat.get("event_id"):
                    state = prior
                    break
                previous_missing = int(prior.get("consecutive_missing_cycles", 0) or 0)
                kill_switch = (not has_decision
                               and latest.get("summary", {}).get("kill_switch") is True)
                consecutive_missing = (0 if has_decision or kill_switch
                                       else previous_missing + 1)
                first_missing = (None if has_decision or kill_switch else
                                 prior.get("first_missing_at_unix") or observed_at)
                state = {
                    "schema": "forge.oracle.symbol_scan_coverage.v1",
                    "instance_id": instance_id, "symbol": symbol,
                    "last_cycle": heartbeat.get("cycle"),
                    "last_event_id": heartbeat.get("event_id"),
                    "last_seen_at_unix": observed_at,
                    "last_decision_outcomes": raw_counts if has_decision else {},
                    "consecutive_missing_cycles": consecutive_missing,
                    "first_missing_at_unix": first_missing,
                    "status": ("decision_observed" if has_decision else
                               "scan_aborted" if kill_switch else "decision_missing"),
                    "missing_reason": (
                        "kill_switch_active" if not has_decision
                        and latest.get("summary", {}).get("kill_switch") is True
                        else "no_symbol_decision_event_in_completed_scan"
                        if not has_decision else None),
                }
                saved = chronicle.act("state.set", {
                    "key": coverage_key, "value": state,
                    "expected_revision": current.get("revision", 0),
                    "_sender": "forge"})
                if isinstance(saved, dict) and saved.get("status") == "complete":
                    break
            else:
                return {"status": "blocked",
                        "message": "Chronicle symbol scan coverage changed concurrently"}

            if state.get("status") == "decision_observed":
                evaluated.append(symbol)
            elif state.get("status") == "decision_missing":
                missing.append({"symbol": symbol,
                                "consecutive_missing_cycles": state.get("consecutive_missing_cycles", 0),
                                "missing_reason": state.get("missing_reason")})
                first_missing = state.get("first_missing_at_unix")
                if (int(state.get("consecutive_missing_cycles", 0)) >= minimum_cycles
                        and isinstance(first_missing, (int, float))
                        and observed_at - first_missing >= minimum_hours * 3600):
                    self._enqueue_no_evaluation_diagnosis(
                        chronicle, latest, state, coverage_key, observed_at)
        return {"status": "complete", "telemetry_available": True,
                "evaluated_symbols": evaluated, "missing_symbols": missing}

    def _enqueue_no_evaluation_diagnosis(self, chronicle: Any,
                                         latest: Dict[str, Any],
                                         coverage: Dict[str, Any],
                                         coverage_key: str,
                                         now_unix: float) -> None:
        """Queue Atlas diagnosis when a live scan repeatedly omits a symbol."""
        symbol = str(coverage.get("symbol", "")).upper()
        instance_id = str(coverage.get("instance_id", ""))
        first_missing = int(float(coverage.get("first_missing_at_unix", now_unix)))
        case_id = "CASE-ORACLE-SCAN-GAP-" + hashlib.sha256(
            f"{instance_id}::{symbol}::{first_missing}".encode("utf-8")).hexdigest()[:20]
        job_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        job_key = f"forge.oracle.no_trade_job.{job_hash}"
        current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return
        job = current.get("value") if current.get("found") else None
        if isinstance(job, dict) and job.get("status") in {"queued", "running", "complete"}:
            return
        if isinstance(job, dict) and float(job.get("available_at_unix", 0) or 0) > now_unix:
            return
        job = {
            "schema": "forge.oracle.no_trade_job.v1", "status": "queued",
            "job_id": job_hash, "diagnosis_type": "symbol_evaluation_missing",
            "case_id": case_id, "coverage_key": coverage_key,
            "summary_snapshot": {"symbol": symbol, "instance_id": instance_id,
                                 "coverage": dict(coverage),
                                 "trader_summary": latest.get("summary", {}),
                                 "last_cycle": latest.get("cycle")},
            "created_at_unix": now_unix, "attempts": int(job.get("attempts", 0))
            if isinstance(job, dict) else 0,
        }
        saved = chronicle.act("state.set", {
            "key": job_key, "value": job,
            "expected_revision": current.get("revision", 0), "_sender": "forge"})
        if isinstance(saved, dict) and saved.get("status") == "complete":
            self._start_no_trade_job(chronicle, job_key, job, now_unix)

    def check_stale_trader_heartbeats(self, now_unix: float | None = None) -> Dict[str, Any]:
        """Record an operational incident when a previously seen trader goes silent."""
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}
        try:
            timeout_sec = min(86400.0, max(60.0, float(
                os.getenv("FORGE_ORACLE_HEARTBEAT_TIMEOUT_SEC", "900"))))
        except ValueError:
            timeout_sec = 900.0
        now_unix = time.time() if now_unix is None else float(now_unix)
        listed = chronicle.act("state.list", {
            "prefix": "forge.oracle.trader_heartbeat.latest.", "_sender": "forge"})
        if not isinstance(listed, dict) or listed.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle could not list trader heartbeat records"}
        stale = 0
        for latest_key in listed.get("keys", []):
            current = chronicle.act("state.get", {"key": latest_key, "_sender": "forge"})
            latest = current.get("value") if isinstance(current, dict) else None
            if not isinstance(latest, dict):
                continue
            last_seen = latest.get("last_seen_at_unix")
            interval = latest.get("cycle_interval_sec", 0)
            effective_timeout = timeout_sec
            if isinstance(interval, (int, float)) and not isinstance(interval, bool):
                effective_timeout = max(timeout_sec, min(86400.0, max(0.0, float(interval)) * 3.0))
            if not isinstance(last_seen, (int, float)) or now_unix - last_seen <= effective_timeout:
                continue
            instance_id = str(latest.get("instance_id", ""))
            if not instance_id:
                continue
            instance_hash = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
            incident_key = f"forge.oracle.trader_heartbeat.incident.{instance_hash}"
            incident = chronicle.act("state.get", {"key": incident_key, "_sender": "forge"})
            if not isinstance(incident, dict) or incident.get("status") != "complete":
                continue
            if incident.get("found"):
                value = incident.get("value")
                if isinstance(value, dict) and value.get("status") == "stale":
                    stale += 1
                    self._enqueue_stale_heartbeat_diagnosis(
                        chronicle, latest, value, incident_key, now_unix)
                    continue
            incident_value = {
                "schema": "forge.oracle.trader_heartbeat_incident.v1",
                "status": "stale", "instance_id": instance_id,
                "opened_at_unix": now_unix, "last_seen_at_unix": last_seen,
                "stale_for_sec": max(0.0, now_unix - last_seen),
                "timeout_sec": effective_timeout, "last_cycle": latest.get("cycle"),
                "symbols": latest.get("symbols", []),
                "last_summary": latest.get("summary", {}),
                "diagnosis": "Oracle trader telemetry is stale; this does not distinguish a stopped trader from a failed bridge or undelivered local outbox.",
            }
            stored = chronicle.act("state.set", {
                "key": incident_key, "value": incident_value,
                "expected_revision": incident.get("revision", 0), "_sender": "forge"})
            if isinstance(stored, dict) and stored.get("status") == "complete":
                stale += 1
                self._enqueue_stale_heartbeat_diagnosis(
                    chronicle, latest, incident_value, incident_key, now_unix)
        return {"status": "complete", "stale_instances": stale,
                "base_timeout_sec": timeout_sec}

    def _enqueue_stale_heartbeat_diagnosis(self, chronicle: Any,
                                           latest: Dict[str, Any],
                                           incident: Dict[str, Any],
                                           incident_key: str,
                                           now_unix: float) -> None:
        """Queue one durable operational diagnosis for a stale Oracle runtime."""
        instance_id = str(incident.get("instance_id", "")).strip()
        last_seen = incident.get("last_seen_at_unix")
        if not instance_id or not isinstance(last_seen, (int, float)):
            return
        case_id = "CASE-ORACLE-HEARTBEAT-GAP-" + hashlib.sha256(
            f"{instance_id}::{int(last_seen)}".encode("utf-8")).hexdigest()[:20]
        job_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        job_key = f"forge.oracle.no_trade_job.{job_hash}"
        current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return
        job = current.get("value") if current.get("found") else None
        if isinstance(job, dict) and job.get("status") in {"queued", "running", "complete"}:
            return
        if isinstance(job, dict) and float(job.get("available_at_unix", 0) or 0) > now_unix:
            return
        job = {
            "schema": "forge.oracle.no_trade_job.v1", "status": "queued",
            "job_id": job_hash, "diagnosis_type": "trader_heartbeat_stale",
            "case_id": case_id, "incident_key": incident_key,
            "summary_snapshot": {
                "instance_id": instance_id,
                "last_seen_at_unix": last_seen,
                "stale_for_sec": incident.get("stale_for_sec"),
                "timeout_sec": incident.get("timeout_sec"),
                "last_cycle": incident.get("last_cycle"),
                "symbols": incident.get("symbols", []),
                "last_summary": latest.get("summary", {}),
            },
            "created_at_unix": now_unix,
            "attempts": int(job.get("attempts", 0)) if isinstance(job, dict) else 0,
        }
        saved = chronicle.act("state.set", {
            "key": job_key, "value": job,
            "expected_revision": current.get("revision", 0), "_sender": "forge"})
        if isinstance(saved, dict) and saved.get("status") == "complete":
            self._start_no_trade_job(chronicle, job_key, job, now_unix)

    def get_oracle_training_status(self) -> Dict[str, Any]:
        """Build a read-only summary of Oracle liveness, diagnosis jobs and cases."""
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}

        def records(prefix: str) -> list[Dict[str, Any]]:
            listed = chronicle.act("state.list", {"prefix": prefix, "_sender": "forge"})
            if not isinstance(listed, dict) or listed.get("status") != "complete":
                raise RuntimeError("Chronicle state listing failed")
            found = []
            for key in listed.get("keys", []):
                current = chronicle.act("state.get", {"key": key, "_sender": "forge"})
                value = current.get("value") if isinstance(current, dict) else None
                if isinstance(value, dict):
                    found.append(value)
            return found

        try:
            now_unix = time.time()
            self.check_stale_trader_heartbeats(now_unix)
            heartbeats = records("forge.oracle.trader_heartbeat.latest.")
            incidents = records("forge.oracle.trader_heartbeat.incident.")
            cases = records("forge.oracle.no_trade_case.")
            operational_cases = records("forge.oracle.operational_no_trade_case.")
            no_evaluation_cases = records("forge.oracle.no_evaluation_case.")
            heartbeat_gap_cases = records("forge.oracle.heartbeat_gap_case.")
            jobs = records("forge.oracle.no_trade_job.")
            scan_coverage = records("forge.oracle.symbol_scan.latest.")
            decision_summaries = records("forge.oracle.decision_summary.")
        except Exception as exc:
            return {"status": "blocked", "message": f"Chronicle status lookup failed: {type(exc).__name__}"}
        incident_by_instance = {item.get("instance_id"): item for item in incidents}
        trader_status = []
        for heartbeat in heartbeats:
            instance_id = heartbeat.get("instance_id")
            incident = incident_by_instance.get(instance_id)
            trader_status.append({
                "instance_id": instance_id,
                "status": "stale" if isinstance(incident, dict) and incident.get("status") == "stale" else "online",
                "last_seen_at_unix": heartbeat.get("last_seen_at_unix"),
                "cycle": heartbeat.get("cycle"), "symbols": heartbeat.get("symbols", []),
                "last_summary": heartbeat.get("summary", {}),
                "symbol_coverage": heartbeat.get("symbol_coverage"),
                "incident": incident,
            })
        decision_status = []
        for summary in decision_summaries:
            recent_events = summary.get("recent_events", [])
            if not isinstance(recent_events, list):
                recent_events = []
            recent_projection = []
            for event in recent_events[-5:]:
                if not isinstance(event, dict):
                    continue
                details = event.get("details", {})
                context = details.get("decision_context") if isinstance(details, dict) else None
                fused = context.get("fused", {}) if isinstance(context, dict) else {}
                market = context.get("market", {}) if isinstance(context, dict) else {}
                recent_projection.append({
                    "event_id": event.get("event_id"),
                    "at_unix": event.get("at_unix"),
                    "outcome_code": event.get("outcome_code"),
                    "has_decision_context": isinstance(context, dict),
                    "decision_call": fused.get("call") if isinstance(fused, dict) else None,
                    "confidence": fused.get("confidence") if isinstance(fused, dict) else None,
                    "regime": market.get("regime") if isinstance(market, dict) else None,
                })
            decision_status.append({
                "symbol": summary.get("symbol"),
                "strategy_id": summary.get("strategy_id"),
                "regime": summary.get("regime"),
                "last_event_at_unix": summary.get("last_event_at_unix"),
                "last_trade_at_unix": summary.get("last_trade_at_unix"),
                "evaluations": summary.get("evaluations"),
                "evaluations_since_last_trade": summary.get("evaluations_since_last_trade"),
                "outcome_counts_since_last_trade": summary.get("outcome_counts_since_last_trade", {}),
                "recent_events": recent_projection,
            })
        return {
            "status": "complete", "checked_at_unix": now_unix,
            "traders": sorted(trader_status,
                               key=lambda item: float(item.get("last_seen_at_unix") or 0),
                               reverse=True),
            "no_trade_cases": sorted(cases,
                                      key=lambda item: float(item.get("opened_at_unix") or 0),
                                      reverse=True)[:100],
            "operational_no_trade_cases": sorted(
                operational_cases,
                key=lambda item: float(item.get("opened_at_unix") or 0),
                reverse=True)[:100],
            "no_evaluation_cases": sorted(
                no_evaluation_cases,
                key=lambda item: float(item.get("opened_at_unix") or 0),
                reverse=True)[:100],
            "heartbeat_gap_cases": sorted(
                heartbeat_gap_cases,
                key=lambda item: float(item.get("opened_at_unix") or 0),
                reverse=True)[:100],
            "symbol_scan_coverage": sorted(
                scan_coverage,
                key=lambda item: float(item.get("last_seen_at_unix") or 0),
                reverse=True)[:500],
            "decision_summaries": sorted(
                decision_status,
                key=lambda item: float(item.get("last_event_at_unix") or 0),
                reverse=True)[:500],
            "research_jobs": sorted(jobs,
                                     key=lambda item: float(item.get("updated_at_unix") or 0),
                                     reverse=True)[:100],
        }

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
        root = Path(__file__).resolve().parents[2] / "Chronicle" / "memory" / "research" / "oracle_market_data"
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
        raw_context = outcome.get("trade_context")
        trade_context: Dict[str, Any] = {}
        if raw_context is not None:
            if (not isinstance(raw_context, dict)
                    or raw_context.get("schema") != "oracle.demo_trade_context.v1"):
                return {"status": "error", "message": "demo trade context schema is invalid"}
            numeric_context = (
                "entry_confidence", "duration_sec", "stop_distance_atr",
                "target_distance_atr", "target_to_stop_ratio", "technical_vote",
                "technical_confidence",
            )
            for field in numeric_context:
                raw_value = raw_context.get(field)
                if raw_value is None:
                    continue
                if (isinstance(raw_value, bool) or not isinstance(raw_value, (int, float))
                        or not math.isfinite(float(raw_value))):
                    return {"status": "error", "message": f"demo trade context {field} must be finite"}
                value_float = float(raw_value)
                if field in {"entry_confidence", "technical_confidence"} and not 0 <= value_float <= 1:
                    return {"status": "error", "message": f"demo trade context {field} must be in [0, 1]"}
                if field == "duration_sec" and not 0 <= value_float <= 365 * 86400:
                    return {"status": "error", "message": "demo trade context duration_sec is out of range"}
                if field in {"stop_distance_atr", "target_distance_atr", "target_to_stop_ratio"} and not 0 <= value_float <= 100:
                    return {"status": "error", "message": f"demo trade context {field} is out of range"}
                if field == "technical_vote" and abs(value_float) > 10:
                    return {"status": "error", "message": "demo trade context technical_vote is out of range"}
                trade_context[field] = value_float
            for field in ("direction", "source", "champion_source"):
                raw_value = raw_context.get(field)
                if raw_value is not None:
                    if not isinstance(raw_value, str) or len(raw_value) > 80:
                        return {"status": "error", "message": f"demo trade context {field} is invalid"}
                    trade_context[field] = raw_value.strip()
            trade_context["schema"] = "oracle.demo_trade_context.v1"
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
            "trade_context": trade_context,
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
                exit_reason = str(outcome.get("exit_reason", "unknown")).strip()[:120] or "unknown"
                exit_stats = value.setdefault("exit_reason_feedback", {})
                if isinstance(exit_stats, dict):
                    exit_stat = exit_stats.setdefault(exit_reason, {
                        "trades": 0, "wins": 0, "losses": 0, "total_pnl_r": 0.0,
                    })
                    exit_stat["trades"] = int(exit_stat.get("trades", 0)) + 1
                    exit_stat["wins"] = int(exit_stat.get("wins", 0)) + int(pnl_r > 0)
                    exit_stat["losses"] = int(exit_stat.get("losses", 0)) + int(pnl_r <= 0)
                    exit_stat["total_pnl_r"] = float(exit_stat.get("total_pnl_r", 0.0)) + pnl_r
                policy_id = str(outcome.get("timeframe_policy_id") or "unspecified")[:160]
                policy_stats = value.setdefault("timeframe_policy_feedback", {})
                if isinstance(policy_stats, dict):
                    policy_stat = policy_stats.setdefault(policy_id, {
                        "trades": 0, "wins": 0, "losses": 0, "total_pnl_r": 0.0,
                    })
                    policy_stat["trades"] = int(policy_stat.get("trades", 0)) + 1
                    policy_stat["wins"] = int(policy_stat.get("wins", 0)) + int(pnl_r > 0)
                    policy_stat["losses"] = int(policy_stat.get("losses", 0)) + int(pnl_r <= 0)
                    policy_stat["total_pnl_r"] = float(policy_stat.get("total_pnl_r", 0.0)) + pnl_r
                recent = list(value.get("recent_outcomes", []))
                recent.append({
                    "close_event_id": close_event_id, "closed_at_unix": closed_at_unix,
                    "pnl_r": pnl_r, "won": pnl_r > 0,
                    "timeframe_policy_id": outcome.get("timeframe_policy_id"),
                    "exit_reason": exit_reason, "trade_context": trade_context,
                    "cost_status": outcome.get("cost_status", "unavailable_provisional"),
                })
                value["recent_outcomes"] = recent[-100:]
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
                    "exit_reason_feedback": value.get("exit_reason_feedback", {}),
                    "timeframe_policy_feedback": value.get("timeframe_policy_feedback", {}),
                    "duplicate": duplicate,
                    "chronicle_key": aggregate_key,
                }
        return {"status": "blocked", "message": "Chronicle strategy feedback changed concurrently; retry the outcome"}

    def record_decision_observation(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an Oracle decision and update Forge's explainable funnel view.

        This records Oracle's decision evidence without changing its live
        policy. The event is durable and idempotent; the symbol summary is a
        compact Chronicle projection for later diagnosis.
        """
        import re

        allowed_outcomes = {
            "symbol_unmapped", "already_in_position", "max_positions_reached",
            "duplicate_position_detected", "signal_exception", "signal_timeout",
            "no_setup", "risk_rejected", "signal_error", "signal_only",
            "trade_opened", "execution_rejected", "execution_unavailable",
            "kill_switch_active", "account_risk_check_unavailable",
            "positions_unavailable", "duplicate_broker_mapping", "trading_disabled",
            "market_closed", "signal_cancelled",
        }
        strategy_evaluation_outcomes = {"no_setup", "risk_rejected"}
        if (not isinstance(observation, dict)
                or observation.get("schema") != "oracle.decision.observation.v1"):
            return {"status": "error", "message": "a supported Oracle decision observation is required"}
        event_id = str(observation.get("event_id", "")).strip()
        symbol = str(observation.get("symbol", "")).strip().upper()
        outcome = str(observation.get("outcome_code", "")).strip()
        details = observation.get("details", {})
        safe_observation = dict(observation) if isinstance(observation, dict) else {}
        safe_details = dict(details) if isinstance(details, dict) else {}
        if "decision_context" in safe_details:
            projected_context = _project_oracle_decision_context(safe_details["decision_context"])
            if projected_context is None:
                safe_details.pop("decision_context", None)
            else:
                safe_details["decision_context"] = projected_context
        safe_observation["details"] = safe_details
        try:
            evaluated_at = float(safe_observation.get("evaluated_at_unix"))
            encoded = json.dumps(safe_observation, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return {"status": "error", "message": "decision observation must be finite JSON"}
        if (not event_id or len(event_id) > 160
                or not re.fullmatch(r"[A-Z0-9._-]{1,24}", symbol)
                or outcome not in allowed_outcomes
                or not math.isfinite(evaluated_at) or evaluated_at <= 0
                or not isinstance(details, dict) or len(encoded) > 64 * 1024):
            return {"status": "error", "message": "decision observation identity or fields are invalid"}
        details = safe_details

        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}
        event_hash = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        input_fingerprint = hashlib.sha256(encoded).hexdigest()
        event_key = f"forge.oracle.decision_observation.{event_hash}"
        event_record = {
            **safe_observation, "schema": "forge.oracle.decision_observation.v1",
            "symbol": symbol, "outcome_code": outcome,
            "evaluated_at_unix": evaluated_at, "received_at_unix": time.time(),
            "source": "oracle_v1_live_trader", "input_fingerprint": input_fingerprint,
        }
        saved = chronicle.act("state.set", {
            "key": event_key, "value": event_record, "expected_revision": 0,
            "_sender": "forge",
        })
        event_duplicate = False
        if not isinstance(saved, dict) or saved.get("status") != "complete":
            existing = chronicle.act("state.get", {"key": event_key, "_sender": "forge"})
            existing_value = existing.get("value") if isinstance(existing, dict) else None
            if (not isinstance(existing_value, dict)
                    or existing_value.get("event_id") != event_id
                    or existing_value.get("input_fingerprint") != input_fingerprint):
                return {"status": "blocked", "message": "Chronicle could not persist the Oracle decision event"}
            event_duplicate = True

        strategy_id = str(details.get("strategy_id") or "unknown").strip()[:160]
        regime = str(details.get("regime") or "unknown").strip()[:64]
        scope_hash = hashlib.sha256(
            f"{symbol}::{strategy_id}::{regime}".encode("utf-8")).hexdigest()
        summary_key = f"forge.oracle.decision_summary.{scope_hash}"
        for _ in range(5):
            current = chronicle.act("state.get", {"key": summary_key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete":
                return {"status": "blocked", "message": "Chronicle decision summary lookup failed"}
            value = current.get("value") if current.get("found") else {
                "schema": "forge.oracle.decision_summary.v1", "symbol": symbol,
                "strategy_id": strategy_id, "regime": regime, "evaluations": 0,
                "outcome_counts": {}, "outcome_counts_since_last_trade": {},
                "evaluations_since_last_trade": 0,
                "strategy_evaluations_since_last_trade": 0,
                "strategy_outcome_counts_since_last_trade": {},
                "last_trade_at_unix": None, "first_observation_at_unix": evaluated_at,
                "recent_events": [],
            }
            if not isinstance(value, dict):
                return {"status": "blocked", "message": "Chronicle decision summary is malformed"}
            recent = list(value.get("recent_events", []))
            if any(isinstance(item, dict) and item.get("event_id") == event_id for item in recent):
                event_duplicate = True
                break
            counts = dict(value.get("outcome_counts", {}))
            counts[outcome] = int(counts.get(outcome, 0)) + 1
            window_counts = dict(value.get("outcome_counts_since_last_trade", {}))
            strategy_counts = dict(value.get("strategy_outcome_counts_since_last_trade", {}))
            value["evaluations"] = int(value.get("evaluations", 0)) + 1
            value["outcome_counts"] = counts
            value["last_event_at_unix"] = evaluated_at
            if outcome == "trade_opened":
                value["last_trade_at_unix"] = evaluated_at
                value["evaluations_since_last_trade"] = 0
                value["outcome_counts_since_last_trade"] = {}
                value["strategy_evaluations_since_last_trade"] = 0
                value["strategy_outcome_counts_since_last_trade"] = {}
                value["first_strategy_evaluation_at_unix"] = None
            else:
                value["evaluations_since_last_trade"] = int(
                    value.get("evaluations_since_last_trade", 0)) + 1
                window_counts[outcome] = int(window_counts.get(outcome, 0)) + 1
                value["outcome_counts_since_last_trade"] = window_counts
                if outcome in strategy_evaluation_outcomes:
                    previous_strategy_evaluations = int(
                        value.get("strategy_evaluations_since_last_trade", 0))
                    value["strategy_evaluations_since_last_trade"] = previous_strategy_evaluations + 1
                    strategy_counts[outcome] = int(strategy_counts.get(outcome, 0)) + 1
                    value["strategy_outcome_counts_since_last_trade"] = strategy_counts
                    value["last_strategy_evaluation_at_unix"] = evaluated_at
                    if previous_strategy_evaluations == 0:
                        value["first_strategy_evaluation_at_unix"] = evaluated_at
            reason_codes = details.get("reasons", [])
            diagnostic_details: Dict[str, Any] = {}
            if isinstance(reason_codes, list):
                diagnostic_details["reason_codes"] = [
                    str(reason)[:240] for reason in reason_codes[:20]
                    if isinstance(reason, (str, int, float)) and not isinstance(reason, bool)
                ]
            for field in ("strategy_id", "regime", "call", "market_state"):
                raw_value = details.get(field)
                if isinstance(raw_value, str) and raw_value.strip():
                    diagnostic_details[field] = raw_value.strip()[:160]
            raw_message = details.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                diagnostic_details["message"] = raw_message.strip()[:320]
            raw_confidence = details.get("confidence")
            if (isinstance(raw_confidence, (int, float))
                    and not isinstance(raw_confidence, bool)
                    and math.isfinite(float(raw_confidence))):
                diagnostic_details["confidence"] = float(raw_confidence)
            decision_context = details.get("decision_context")
            if isinstance(decision_context, dict):
                diagnostic_details["decision_context"] = decision_context
            recent.append({"event_id": event_id, "at_unix": evaluated_at,
                           "outcome_code": outcome,
                           "reason_codes": diagnostic_details.get("reason_codes", []),
                           "details": diagnostic_details})
            value["recent_events"] = recent[-200:]
            stored = chronicle.act("state.set", {
                "key": summary_key, "value": value,
                "expected_revision": current.get("revision", 0), "_sender": "forge",
            })
            if isinstance(stored, dict) and stored.get("status") == "complete":
                break
        else:
            return {"status": "blocked", "message": "Chronicle decision summary changed concurrently; retry the event"}

        diagnosis_case = self._schedule_no_trade_diagnosis(
            chronicle, summary_key, value, evaluated_at)
        operational_case = self._schedule_persistent_operational_no_trade(
            chronicle, summary_key, value, evaluated_at)
        current_counts = value.get("outcome_counts_since_last_trade")
        if not isinstance(current_counts, dict):
            current_counts = value.get("outcome_counts", {})
        blockers = {key: count for key, count in current_counts.items()
                    if key != "trade_opened"}
        last_trade = value.get("last_trade_at_unix")
        return {
            "status": "complete", "duplicate": event_duplicate, "symbol": symbol,
            "strategy_id": strategy_id, "event_key": event_key,
            "summary_key": summary_key,
            "diagnosis": {
                "latest_outcome": outcome,
                "primary_observed_blocker": max(blockers, key=blockers.get) if blockers else None,
                "evaluations": value.get("evaluations", 0),
                "evaluations_since_last_trade": value.get("evaluations_since_last_trade", 0),
                "strategy_evaluations_since_last_trade": value.get(
                    "strategy_evaluations_since_last_trade", 0),
                "strategy_outcome_counts_since_last_trade": value.get(
                    "strategy_outcome_counts_since_last_trade", {}),
                "seconds_since_last_trade": max(0.0, evaluated_at - float(last_trade))
                if isinstance(last_trade, (int, float)) else None,
                "outcome_counts": value.get("outcome_counts", {}),
                "outcome_counts_since_last_trade": current_counts,
                "research_case": diagnosis_case,
                "operational_research_case": operational_case,
            },
        }

    def _schedule_persistent_operational_no_trade(
        self, chronicle: Any, summary_key: str, summary: Dict[str, Any],
        now_unix: float,
    ) -> Dict[str, Any]:
        """Queue an operational Atlas diagnosis for persistent non-strategy blockers."""
        try:
            threshold_days = min(90.0, max(1.0, float(os.getenv(
                "FORGE_ORACLE_NO_TRADE_DAYS", "14"))))
            minimum_events = min(10000, max(1, int(os.getenv(
                "FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "10"))))
            max_gap_hours = min(720.0, max(1.0, float(os.getenv(
                "FORGE_ORACLE_NO_TRADE_MAX_EVALUATION_GAP_HOURS", "48"))))
        except ValueError:
            threshold_days, minimum_events, max_gap_hours = 14.0, 10, 48.0
        counts = summary.get("outcome_counts_since_last_trade", {})
        if not isinstance(counts, dict):
            return {"status": "not_due", "reason": "operational_outcome_counts_unavailable"}
        operational_counts = {
            outcome: int(count) for outcome, count in counts.items()
            if outcome in ORACLE_OPERATIONAL_NO_TRADE_OUTCOMES
            and isinstance(count, int) and not isinstance(count, bool) and count > 0
        }
        if not operational_counts:
            return {"status": "not_due", "reason": "no_operational_blocker_observed"}
        primary = max(operational_counts, key=operational_counts.get)
        if operational_counts[primary] < minimum_events:
            return {"status": "not_due", "reason": "minimum_repeated_operational_blocker_count_not_met"}
        baseline = summary.get("last_trade_at_unix") or summary.get("first_observation_at_unix")
        if (not isinstance(baseline, (int, float))
                or now_unix - float(baseline) < threshold_days * 86400):
            return {"status": "not_due", "reason": "no_trade_window_not_elapsed"}
        last_event = summary.get("last_event_at_unix")
        if (not isinstance(last_event, (int, float))
                or now_unix - float(last_event) > max_gap_hours * 3600):
            return {"status": "not_due", "reason": "operational_events_not_recent"}
        episode_seconds = threshold_days * 86400
        episode_index = max(1, int((now_unix - float(baseline)) // episode_seconds))
        episode_id = f"{int(baseline)}:{episode_index}"
        symbol = str(summary.get("symbol", "")).upper()
        strategy_id = str(summary.get("strategy_id", "unknown"))
        regime = str(summary.get("regime", "unknown"))
        case_id = "CASE-ORACLE-OPERATIONAL-NO-TRADE-" + hashlib.sha256(
            f"{symbol}::{strategy_id}::{episode_id}::{primary}".encode()).hexdigest()[:20]
        job_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        job_key = f"forge.oracle.no_trade_job.{job_hash}"
        current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "pending", "reason": "Chronicle operational diagnosis lookup failed"}
        prior = current.get("value") if current.get("found") else None
        if isinstance(prior, dict):
            if prior.get("status") == "complete":
                return {"status": "already_open", "job_key": job_key, "case_id": case_id}
            if prior.get("status") == "running" and float(prior.get("lease_until_unix", 0) or 0) > now_unix:
                return {"status": "already_open", "job_key": job_key, "case_id": case_id}
            if prior.get("status") == "retryable" and float(prior.get("available_at_unix", 0) or 0) > now_unix:
                return {"status": "pending", "reason": "operational diagnosis retry backoff"}
        recent_events = [event for event in summary.get("recent_events", [])[-30:]
                         if isinstance(event, dict)]
        job = {
            "schema": "forge.oracle.no_trade_job.v1", "status": "queued",
            "diagnosis_type": "persistent_operational_no_trade",
            "job_id": job_hash, "case_id": case_id, "summary_key": summary_key,
            "episode_id": episode_id, "episode_index": episode_index,
            "summary_snapshot": {
                "symbol": symbol, "strategy_id": strategy_id, "regime": regime,
                "baseline_at_unix": baseline, "threshold_days": threshold_days,
                "operational_outcome_counts": operational_counts,
                "primary_observed_outcome": primary,
                "recent_decisions": recent_events,
            },
            "created_at_unix": now_unix,
            "attempts": int(prior.get("attempts", 0)) if isinstance(prior, dict) else 0,
        }
        queued = chronicle.act("state.set", {
            "key": job_key, "value": job,
            "expected_revision": current.get("revision", 0), "_sender": "forge",
        })
        if not isinstance(queued, dict) or queued.get("status") != "complete":
            return {"status": "pending", "reason": "Chronicle could not enqueue operational diagnosis"}
        if not self._start_no_trade_job(chronicle, job_key, job, now_unix):
            return {"status": "queued", "reason": "durable_job_waiting_for_research_capacity",
                    "job_key": job_key, "case_id": case_id}
        return {"status": "scheduled", "reason": "persistent_operational_no_trade_threshold_met",
                "job_key": job_key, "case_id": case_id}

    def _schedule_no_trade_diagnosis(self, chronicle: Any, summary_key: str,
                                     summary: Dict[str, Any], now_unix: float) -> Dict[str, Any]:
        """Run Atlas work away from the V1 bridge request/acknowledgement path."""
        try:
            threshold_days = float(os.getenv("FORGE_ORACLE_NO_TRADE_DAYS", "14"))
            minimum_evaluations = int(os.getenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "10"))
            max_evaluation_gap_hours = float(os.getenv(
                "FORGE_ORACLE_NO_TRADE_MAX_EVALUATION_GAP_HOURS", "48"))
        except ValueError:
            threshold_days, minimum_evaluations, max_evaluation_gap_hours = 14.0, 10, 48.0
        threshold_days = min(90.0, max(1.0, threshold_days))
        minimum_evaluations = min(10000, max(1, minimum_evaluations))
        max_evaluation_gap_hours = min(720.0, max(1.0, max_evaluation_gap_hours))
        strategy_evaluations = int(summary.get("strategy_evaluations_since_last_trade", 0))
        baseline = summary.get("last_trade_at_unix") or summary.get("first_strategy_evaluation_at_unix")
        if strategy_evaluations < minimum_evaluations:
            return {"status": "not_due", "reason": "minimum_strategy_evaluation_count_not_met"}
        last_strategy_eval = summary.get("last_strategy_evaluation_at_unix")
        if (not isinstance(last_strategy_eval, (int, float))
                or now_unix - last_strategy_eval > max_evaluation_gap_hours * 3600):
            return {"status": "not_due", "reason": "strategy_evaluations_not_recent"}
        if not isinstance(baseline, (int, float)) or now_unix - baseline < threshold_days * 86400:
            return {"status": "not_due", "reason": "no_trade_window_not_elapsed"}
        episode_seconds = threshold_days * 86400
        episode_index = max(1, int((now_unix - baseline) // episode_seconds))
        episode_id = f"{int(baseline)}:{episode_index}"
        key = f"{summary_key}::{episode_id}"
        marker_exists = summary.get("last_no_trade_research_episode") == episode_id
        job_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        job_key = f"forge.oracle.no_trade_job.{job_hash}"
        current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "pending", "reason": "Chronicle diagnosis-job lookup failed"}
        job = current.get("value") if current.get("found") else None
        if marker_exists and not isinstance(job, dict):
            return {"status": "already_open", "reason": "case_exists_for_current_no_trade_window"}
        if isinstance(job, dict):
            if job.get("status") == "complete":
                return {"status": "already_open", "reason": "durable_research_job_complete"}
            if job.get("status") == "running" and float(job.get("lease_until_unix", 0) or 0) > now_unix:
                return {"status": "already_open", "reason": "durable_research_job_has_active_lease"}
            if job.get("status") == "retryable" and float(job.get("available_at_unix", 0) or 0) > now_unix:
                return {"status": "pending", "reason": "durable_research_job_retry_backoff"}
        else:
            job = {
                "schema": "forge.oracle.no_trade_job.v1", "status": "queued",
                "job_id": job_hash, "summary_key": summary_key,
                "episode_id": episode_id, "episode_index": episode_index,
                "summary_snapshot": dict(summary), "created_at_unix": now_unix,
                "attempts": 0,
            }
        job.update({"status": "queued", "summary_key": summary_key,
                    "episode_id": episode_id, "episode_index": episode_index,
                    "summary_snapshot": dict(summary), "updated_at_unix": now_unix,
                    "runtime_id": self._diagnostic_runtime_id})
        queued = chronicle.act("state.set", {
            "key": job_key, "value": job,
            "expected_revision": current.get("revision", 0), "_sender": "forge",
        })
        if not isinstance(queued, dict) or queued.get("status") != "complete":
            return {"status": "pending", "reason": "Chronicle could not enqueue the no-trade research job"}
        started = self._start_no_trade_job(chronicle, job_key, job, now_unix)
        if not started:
            return {"status": "queued", "reason": "durable_job_waiting_for_research_capacity"}
        return {"status": "scheduled", "reason": "sustained_no_trade_threshold_met",
                "job_key": job_key}

    def _start_no_trade_job(self, chronicle: Any, job_key: str,
                            job: Dict[str, Any], now_unix: float) -> bool:
        with self._diagnostic_lock:
            if job_key in self._diagnostic_inflight:
                return True
            if len(self._diagnostic_inflight) >= self._diagnostic_max_workers:
                return False
            self._diagnostic_inflight.add(job_key)
        job.update({"status": "running", "runtime_id": self._diagnostic_runtime_id,
                    "started_at_unix": now_unix, "lease_until_unix": now_unix + 900.0,
                    "scheduled_at_unix": job.get("scheduled_at_unix", now_unix),
                    "attempts": int(job.get("attempts", 0)) + 1,
                    "updated_at_unix": now_unix})
        current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete" or not current.get("found"):
            with self._diagnostic_lock:
                self._diagnostic_inflight.discard(job_key)
            return False
        claimed = chronicle.act("state.set", {
            "key": job_key, "value": job,
            "expected_revision": current.get("revision", 0), "_sender": "forge",
        })
        if not isinstance(claimed, dict) or claimed.get("status") != "complete":
            with self._diagnostic_lock:
                self._diagnostic_inflight.discard(job_key)
            return False
        worker = threading.Thread(
            target=self._run_no_trade_diagnosis,
            args=(job_key, chronicle, dict(job)),
            name=f"forge-oracle-diagnosis-{job.get('summary_snapshot', {}).get('symbol', 'symbol')}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            with self._diagnostic_lock:
                self._diagnostic_inflight.discard(job_key)
            latest = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
            saved_job = latest.get("value") if isinstance(latest, dict) else None
            if (isinstance(latest, dict) and latest.get("status") == "complete"
                    and isinstance(saved_job, dict)):
                saved_job.update({"status": "queued", "lease_until_unix": None,
                                  "updated_at_unix": time.time(),
                                  "last_dispatch_error": "thread_start_failed"})
                chronicle.act("state.set", {
                    "key": job_key, "value": saved_job,
                    "expected_revision": latest.get("revision", 0), "_sender": "forge",
                })
            return False
        return True

    def resume_pending_no_trade_jobs(self) -> Dict[str, Any]:
        """Recover durable queued jobs and jobs whose worker lease expired."""
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}
        listed = chronicle.act("state.list", {
            "prefix": "forge.oracle.no_trade_job.", "_sender": "forge"})
        if not isinstance(listed, dict) or listed.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle could not list durable Oracle research jobs"}
        now_unix = time.time()
        resumed = 0
        skipped = 0
        for job_key in listed.get("keys", []):
            current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
            job = current.get("value") if isinstance(current, dict) else None
            if not isinstance(job, dict) or job.get("status") == "complete":
                skipped += 1
                continue
            if job.get("status") == "running" and float(job.get("lease_until_unix", 0) or 0) > now_unix:
                skipped += 1
                continue
            if job.get("status") == "retryable" and float(job.get("available_at_unix", 0) or 0) > now_unix:
                skipped += 1
                continue
            if not isinstance(job.get("summary_snapshot"), dict):
                log.error("Ignoring malformed durable Oracle diagnosis job %s", job_key)
                skipped += 1
                continue
            job["status"] = "queued"
            queued = chronicle.act("state.set", {
                "key": job_key, "value": job,
                "expected_revision": current.get("revision", 0), "_sender": "forge",
            })
            if isinstance(queued, dict) and queued.get("status") == "complete":
                if self._start_no_trade_job(chronicle, job_key, job, now_unix):
                    resumed += 1
            else:
                skipped += 1
        return {"status": "complete", "resumed": resumed, "skipped": skipped}

    def _run_no_trade_diagnosis(self, job_key: str, chronicle: Any,
                                job: Dict[str, Any]) -> None:
        result = {"status": "error", "reason": "diagnostic worker did not finish"}
        summary = job.get("summary_snapshot", {})
        summary_key = job.get("summary_key")
        try:
            if job.get("diagnosis_type") == "symbol_evaluation_missing":
                result = self._research_symbol_evaluation_gap(chronicle, job, summary)
            elif job.get("diagnosis_type") == "trader_heartbeat_stale":
                result = self._research_stale_trader_heartbeat(chronicle, job, summary)
            elif job.get("diagnosis_type") == "persistent_operational_no_trade":
                result = self._research_persistent_operational_no_trade(chronicle, job, summary)
            else:
                result = self._maybe_research_no_trade_case(
                    chronicle, summary_key, summary,
                    time.time())
        except Exception as exc:
            log.warning("Oracle no-trade diagnosis failed: %s", type(exc).__name__)
        finally:
            with self._diagnostic_lock:
                self._diagnostic_inflight.discard(job_key)
            try:
                current = chronicle.act("state.get", {"key": job_key, "_sender": "forge"})
                saved_job = current.get("value") if isinstance(current, dict) else None
                if isinstance(current, dict) and current.get("status") == "complete" and isinstance(saved_job, dict):
                    completed = (result.get("experiment_status") == "complete"
                                 or result.get("status") == "already_open"
                                 or (job.get("diagnosis_type") in {
                                         "symbol_evaluation_missing", "trader_heartbeat_stale",
                                         "persistent_operational_no_trade"}
                                     and result.get("case_persisted") is True
                                     and result.get("atlas_status") == "complete"))
                    saved_job.update({
                        "status": "complete" if completed else "retryable",
                        "last_result": result, "updated_at_unix": time.time(),
                        "available_at_unix": time.time() + 3600.0,
                        "lease_until_unix": None,
                    })
                    chronicle.act("state.set", {
                        "key": job_key, "value": saved_job,
                        "expected_revision": current.get("revision", 0), "_sender": "forge",
                    })
            except Exception as exc:
                log.warning("Could not persist Oracle diagnosis-job completion: %s",
                            type(exc).__name__)
            try:
                self.resume_pending_no_trade_jobs()
            except Exception as exc:
                log.warning("Could not dispatch queued Oracle diagnosis jobs: %s",
                            type(exc).__name__)

    def _research_stale_trader_heartbeat(self, chronicle: Any,
                                         job: Dict[str, Any],
                                         snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Record an operational hypothesis without running a strategy experiment."""
        case_id = str(job.get("case_id", ""))
        if not case_id:
            return {"status": "retryable", "reason": "heartbeat incident case identity is missing"}
        case_key = "forge.oracle.heartbeat_gap_case." + hashlib.sha256(
            case_id.encode("utf-8")).hexdigest()
        current = chronicle.act("state.get", {"key": case_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "retryable", "reason": "Chronicle heartbeat case lookup failed"}
        prior = current.get("value") if current.get("found") else None
        if (isinstance(prior, dict)
                and isinstance(prior.get("atlas_research"), dict)
                and prior["atlas_research"].get("status") == "complete"):
            return {"status": "complete", "case_persisted": True,
                    "case_key": case_key, "atlas_status": "complete",
                    "strategy_experiment_performed": False}

        instance_id = str(snapshot.get("instance_id", ""))
        symbols = snapshot.get("symbols", [])
        query = (
            f"Diagnose an Oracle cTrader demo runtime heartbeat gap for instance {instance_id}. "
            f"Its last heartbeat was at {snapshot.get('last_seen_at_unix')} and Forge observed "
            f"a gap of {snapshot.get('stale_for_sec')} seconds against a configured timeout of "
            f"{snapshot.get('timeout_sec')} seconds. Last reported cycle: {snapshot.get('last_cycle')}; "
            f"configured symbols: {symbols}; last reported summary: "
            f"{json.dumps(snapshot.get('last_summary', {}), sort_keys=True, ensure_ascii=False)[:4000]}. "
            "The available evidence cannot distinguish a stopped process, network or bridge failure, "
            "or an undelivered local outbox. State that uncertainty clearly. Give an ordered, "
            "read-only diagnostic checklist and identify which logs or health checks would resolve "
            "the cause. Do not run a strategy experiment or change trading policy."
        )
        research: Dict[str, Any] = {"status": "unavailable", "reason": "Atlas is not connected"}
        atlas = getattr(self._forge, "atlas", None)
        if atlas is not None:
            try:
                response = atlas.handle({
                    "task": "research.investigate",
                    "context": {"query": query, "domain": "trading system operations",
                                "deep_research": True, "max_rounds": 2,
                                "deadline_sec": 60.0},
                    "sender": "forge",
                })
                report = response.get("report", {}) if isinstance(response, dict) else {}
                if isinstance(report, dict):
                    sources = report.get("sources", report.get("citations", []))
                    if not isinstance(sources, list):
                        sources = report.get("evidence", [])
                    sources = [source for source in sources
                               if isinstance(source, (dict, str))] if isinstance(sources, list) else []
                    summary_text = str(report.get("summary", "")).strip()[:12000]
                    findings = report.get("findings", [])
                    limitations = report.get("limitations", [])
                    contradictions = report.get("contradictions", [])
                    complete = (isinstance(response, dict)
                                and response.get("status") in {"complete", "success"}
                                and bool(summary_text) and bool(sources))
                    research = {
                        "status": "complete" if complete else "partial",
                        "summary": summary_text, "sources": sources[:50],
                        "confidence": report.get("confidence"),
                        "findings": findings[:30] if isinstance(findings, list) else [],
                        "limitations": limitations[:30] if isinstance(limitations, list) else [],
                        "contradictions": contradictions[:20] if isinstance(contradictions, list) else [],
                    }
                else:
                    research = {"status": "partial", "summary": str(report)[:12000]}
            except Exception as exc:
                research = {"status": "unavailable", "error": type(exc).__name__}

        hypothesis = self._forge.hypothesis_queue.add(
            f"Oracle runtime {instance_id} stopped delivering heartbeats; identify and verify "
            "the operational cause before assessing strategy performance.",
            "forge_oracle_heartbeat_diagnosis", case_id)
        incident_status = "stale"
        incident_key = str(job.get("incident_key", ""))
        if incident_key:
            incident_record = chronicle.act("state.get", {
                "key": incident_key, "_sender": "forge"})
            incident_value = (incident_record.get("value")
                              if isinstance(incident_record, dict) else None)
            if (isinstance(incident_value, dict)
                    and incident_value.get("status") in {"stale", "recovered"}):
                incident_status = incident_value["status"]
        case = {
            "schema": "forge.oracle.heartbeat_gap_case.v1", "case_id": case_id,
            "instance_id": instance_id, "symbols": symbols,
            "opened_at_unix": time.time(), "incident_snapshot": dict(snapshot),
            "runtime_status": incident_status,
            "recovered_at_unix": (incident_value.get("recovered_at_unix")
                                  if incident_status == "recovered"
                                  and isinstance(incident_value, dict) else None),
            "atlas_research": research, "hypothesis": hypothesis.to_dict(),
            "lifecycle_status": ("research_recorded" if research.get("status") == "complete"
                                 else "research_unavailable"),
            "strategy_experiment_performed": False,
            "promotion_performed": False, "risk_policy_changed": False,
        }
        try:
            self._record_atlas_hypothesis_evidence(hypothesis.id, research, case)
            hypothesis = self._forge.hypothesis_queue.get(hypothesis.id) or hypothesis
            case["hypothesis"] = hypothesis.to_dict()
        except Exception as exc:
            return {"status": "retryable", "case_persisted": False,
                    "reason": f"Chronicle hypothesis evidence update failed: {type(exc).__name__}",
                    "atlas_status": research.get("status"),
                    "strategy_experiment_performed": False}
        saved = chronicle.act("state.set", {
            "key": case_key, "value": case,
            "expected_revision": current.get("revision", 0), "_sender": "forge"})
        persisted = isinstance(saved, dict) and saved.get("status") == "complete"
        return {"status": "complete" if persisted and research.get("status") == "complete"
                else "retryable", "case_persisted": persisted,
                "case_key": case_key, "hypothesis_id": hypothesis.id,
                "atlas_status": research.get("status"),
                "strategy_experiment_performed": False}

    def _research_symbol_evaluation_gap(self, chronicle: Any,
                                        job: Dict[str, Any],
                                        snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Ask Atlas why a configured symbol is repeatedly absent from scans.

        This is an operational diagnosis. It deliberately creates no strategy
        ablation and makes no request to weaken risk or execution controls.
        """
        case_id = str(job.get("case_id", ""))
        case_key = "forge.oracle.no_evaluation_case." + hashlib.sha256(
            case_id.encode("utf-8")).hexdigest()
        current = chronicle.act("state.get", {"key": case_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "retryable", "reason": "Chronicle case lookup failed"}
        coverage = snapshot.get("coverage", {})
        symbol = str(snapshot.get("symbol", "")).upper()
        instance_id = str(snapshot.get("instance_id", ""))
        query = (
            f"Diagnose an Oracle cTrader demo scan-coverage gap. Trader instance "
            f"{instance_id} lists {symbol} as configured, but the latest completed "
            f"cycle {snapshot.get('last_cycle')} has no per-symbol decision event. "
            f"This gap persisted for {coverage.get('consecutive_missing_cycles')} "
            f"cycles since {coverage.get('first_missing_at_unix')}; latest known "
            f"scan status is {coverage.get('missing_reason')}. Use the supplied "
            f"telemetry {snapshot.get('trader_summary', {})} to separate scanner, "
            "symbol mapping, market-state, risk gate, and bridge/outbox causes. "
            "Do not infer that the strategy is poor and do not suggest relaxing "
            "risk controls. Return evidence, uncertainty, and the next diagnostic." )
        research: Dict[str, Any] = {"status": "unavailable", "reason": "Atlas is not connected"}
        atlas = getattr(self._forge, "atlas", None)
        if atlas is not None:
            try:
                response = atlas.handle({
                    "task": "research.investigate",
                    "context": {"query": query, "domain": "trading system operations",
                                "deep_research": True, "max_rounds": 2,
                                "deadline_sec": 60.0},
                    "sender": "forge",
                })
                report = response.get("report", {}) if isinstance(response, dict) else {}
                if isinstance(report, dict):
                    sources = report.get("sources", report.get("citations", []))
                    if not isinstance(sources, list):
                        sources = report.get("evidence", [])
                    summary_text = str(report.get("summary", "")).strip()[:12000]
                    sources = [source for source in sources
                               if isinstance(source, (dict, str))] if isinstance(sources, list) else []
                    atlas_complete = (isinstance(response, dict)
                                      and response.get("status") in {"complete", "success"}
                                      and bool(summary_text) and bool(sources))
                    research = {
                        "status": "complete" if atlas_complete else "partial",
                        "summary": summary_text,
                        "confidence": report.get("confidence"),
                        "sources": sources[:50],
                        "source_status": report.get("source_status", {}),
                    }
                else:
                    research = {"status": "partial", "summary": str(report)[:12000]}
            except Exception as exc:
                research = {"status": "unavailable", "error": type(exc).__name__}

        hypothesis = self._forge.hypothesis_queue.add(
            f"Oracle scan telemetry repeatedly omitted configured symbol {symbol}; "
            "identify and verify the operational cause before evaluating strategy quality.",
            "forge_oracle_scan_coverage", case_id)
        case = {
            "schema": "forge.oracle.no_evaluation_case.v1", "case_id": case_id,
            "symbol": symbol, "instance_id": instance_id,
            "opened_at_unix": float(coverage.get("first_missing_at_unix") or time.time()),
            "latest_cycle": snapshot.get("last_cycle"),
            "consecutive_missing_cycles": coverage.get("consecutive_missing_cycles"),
            "missing_reason": coverage.get("missing_reason"),
            "trader_summary": snapshot.get("trader_summary", {}),
                    "atlas_research": research, "hypothesis": hypothesis.to_dict(),
            "lifecycle_status": "research_recorded" if research.get("status") == "complete"
            else "research_unavailable", "strategy_experiment_performed": False,
            "promotion_performed": False, "risk_policy_changed": False,
        }
        saved = chronicle.act("state.set", {
            "key": case_key, "value": case,
            "expected_revision": current.get("revision", 0), "_sender": "forge"})
        persisted = isinstance(saved, dict) and saved.get("status") == "complete"
        return {"status": "complete" if persisted and research.get("status") == "complete"
                else "retryable", "case_persisted": persisted,
                "case_key": case_key, "hypothesis_id": hypothesis.id,
                "atlas_status": research.get("status"),
                "strategy_experiment_performed": False}

    def _research_persistent_operational_no_trade(self, chronicle: Any,
                                                  job: Dict[str, Any],
                                                  snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Research repeated operational blockers without labeling them strategy failures."""
        case_id = str(job.get("case_id", "")).strip()
        symbol = str(snapshot.get("symbol", "")).upper()
        if not case_id or not symbol:
            return {"status": "retryable", "reason": "operational no-trade case identity is incomplete"}
        case_key = "forge.oracle.operational_no_trade_case." + hashlib.sha256(
            case_id.encode("utf-8")).hexdigest()
        current = chronicle.act("state.get", {"key": case_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "retryable", "reason": "Chronicle operational case lookup failed"}
        prior = current.get("value") if current.get("found") else None
        if (isinstance(prior, dict)
                and isinstance(prior.get("atlas_research"), dict)
                and prior["atlas_research"].get("status") == "complete"):
            return {"status": "complete", "case_persisted": True,
                    "case_key": case_key, "atlas_status": "complete",
                    "strategy_experiment_performed": False}

        counts = snapshot.get("operational_outcome_counts", {})
        if not isinstance(counts, dict):
            counts = {}
        recent = snapshot.get("recent_decisions", [])
        if not isinstance(recent, list):
            recent = []
        recent = [item for item in recent if isinstance(item, dict)][-30:]
        primary = str(snapshot.get("primary_observed_outcome", "unknown"))
        strategy_id = str(snapshot.get("strategy_id", "unknown"))
        regime = str(snapshot.get("regime", "unknown"))
        days = snapshot.get("threshold_days", 14)
        query = (
            f"Diagnose why Oracle's demo runtime has produced no fills for symbol {symbol} "
            f"for at least {days} days while repeatedly reporting operational decision outcome "
            f"{primary} ({counts}). Strategy identity is {strategy_id}; observed regime is {regime}. "
            f"Recent decision evidence: {json.dumps(recent[-20:], sort_keys=True, ensure_ascii=False)[:5000]}. "
            "Determine what the evidence supports about broker mapping, trading schedule, account/risk "
            "availability, signal runtime, or order execution. Do not assume the strategy caused the "
            "failure. Separate cited evidence from uncertainty and suggest the next read-only check. "
            "Do not run strategy ablations, recommend weakening risk controls, alter execution, "
            "or promote a strategy in this operational diagnosis."
        )
        research: Dict[str, Any] = {"status": "unavailable", "reason": "Atlas is not connected"}
        atlas = getattr(self._forge, "atlas", None)
        if atlas is not None:
            try:
                response = atlas.handle({
                    "task": "research.investigate",
                    "context": {"query": query, "domain": "trading system operations",
                                "deep_research": True, "max_rounds": 2,
                                "deadline_sec": 60.0},
                    "sender": "forge",
                })
                report = response.get("report", {}) if isinstance(response, dict) else {}
                if isinstance(report, dict):
                    sources = report.get("sources", report.get("citations", []))
                    if not isinstance(sources, list):
                        sources = report.get("evidence", [])
                    sources = [source for source in sources
                               if isinstance(source, (dict, str))] if isinstance(sources, list) else []
                    summary_text = str(report.get("summary", "")).strip()[:12000]
                    complete = (isinstance(response, dict)
                                and response.get("status") in {"complete", "success"}
                                and bool(summary_text) and bool(sources))
                    research = {
                        "status": "complete" if complete else "partial",
                        "summary": summary_text, "sources": sources[:50],
                        "confidence": report.get("confidence"),
                        "findings": report.get("findings", [])[:30]
                        if isinstance(report.get("findings"), list) else [],
                        "limitations": report.get("limitations", [])[:30]
                        if isinstance(report.get("limitations"), list) else [],
                        "contradictions": report.get("contradictions", [])[:20]
                        if isinstance(report.get("contradictions"), list) else [],
                    }
                else:
                    research = {"status": "partial", "summary": str(report)[:12000], "sources": []}
            except Exception as exc:
                research = {"status": "unavailable", "error": type(exc).__name__}

        hypothesis = self._forge.hypothesis_queue.add(
            f"Repeated {primary} outcomes accompany a prolonged no-fill period for {symbol}; "
            "verify the operational cause before judging strategy quality or changing trading behavior.",
            "forge_oracle_operational_no_trade_diagnosis", case_id)
        case = {
            "schema": "forge.oracle.operational_no_trade_case.v1",
            "case_id": case_id, "symbol": symbol, "strategy_id": strategy_id,
            "regime": regime, "opened_at_unix": snapshot.get("baseline_at_unix"),
            "diagnosed_at_unix": time.time(), "threshold_days": days,
            "primary_observed_outcome": primary,
            "operational_outcome_counts": counts,
            "recent_decisions": recent,
            "atlas_research": research, "hypothesis": hypothesis.to_dict(),
            "lifecycle_status": ("research_recorded" if research.get("status") == "complete"
                                 else "research_unavailable"),
            "strategy_experiment_performed": False,
            "promotion_performed": False, "risk_policy_changed": False,
        }
        try:
            self._record_atlas_hypothesis_evidence(hypothesis.id, research, case)
            updated_hypothesis = self._forge.hypothesis_queue.get(hypothesis.id)
            if updated_hypothesis is not None:
                case["hypothesis"] = updated_hypothesis.to_dict()
        except Exception as exc:
            return {"status": "retryable", "case_persisted": False,
                    "reason": f"Chronicle hypothesis update failed: {type(exc).__name__}",
                    "atlas_status": research.get("status"),
                    "strategy_experiment_performed": False}
        saved = chronicle.act("state.set", {
            "key": case_key, "value": case,
            "expected_revision": current.get("revision", 0), "_sender": "forge",
        })
        persisted = isinstance(saved, dict) and saved.get("status") == "complete"
        return {"status": "complete" if persisted and research.get("status") == "complete"
                else "retryable", "case_persisted": persisted,
                "case_key": case_key, "hypothesis_id": hypothesis.id,
                "atlas_status": research.get("status"),
                "strategy_experiment_performed": False}

    def _build_no_trade_timeframe_context(self, symbol: str, strategy_id: str,
                                          regime: str, decisions: list) -> Dict[str, Any]:
        """Build a replayable seven-timeframe incumbent view at observed decision times."""
        try:
            token = os.getenv("ORACLE_FORGE_BRIDGE_TOKEN", "").strip()
            if not token:
                return {"status": "unavailable", "reason": "Oracle V1 bridge token is not configured"}
            import urllib.request
            payload = json.dumps({"symbol": symbol}, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                f"{os.getenv('ORACLE_V1_API_URL', 'http://127.0.0.1:8001').rstrip('/')}/internal/oracle/forge/incumbent",
                data=payload, method="POST",
                headers={"Content-Type": "application/json", "X-Oracle-Forge-Token": token},
            )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                incumbent = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"status": "unavailable", "reason": f"V1 incumbent lookup failed: {type(exc).__name__}"}
        if not isinstance(incumbent, dict) or incumbent.get("status") != "complete":
            return {"status": "unavailable", "reason": "V1 incumbent was not available"}
        genome = incumbent.get("baseline_genome")
        if not isinstance(genome, dict) or genome.get("genome_id") != strategy_id:
            return {"status": "unavailable", "reason": "V1 incumbent does not match the observed strategy"}
        if regime not in {"", "unknown", str(incumbent.get("regime", ""))}:
            return {"status": "unavailable", "reason": "V1 incumbent regime differs from observed context"}

        market = self._research_data.fetch_topdown_bundle(symbol)
        if not isinstance(market, dict) or market.get("status") != "complete":
            return {"status": "unavailable", "reason": str(
                market.get("message", "cTrader seven-timeframe history unavailable")
                if isinstance(market, dict) else "invalid cTrader bundle")[:320]}
        timeframe_bars = market.get("timeframes")
        fingerprints = market.get("timeframe_fingerprints")
        coverage = market.get("timeframe_coverage")
        required = set(ORACLE_DIAGNOSTIC_TIMEFRAMES_MS)
        if (not isinstance(timeframe_bars, dict) or set(timeframe_bars) != required
                or not isinstance(fingerprints, dict) or set(fingerprints) != required
                or not isinstance(coverage, dict)):
            return {"status": "unavailable", "reason": "cTrader bundle lacks validated seven-timeframe provenance"}
        dataset = self._persist_topdown_dataset(
            symbol, timeframe_bars, fingerprints, coverage,
            str(market.get("data_fingerprint", "")), str(market.get("as_of", "")))
        if dataset.get("status") != "complete":
            return {"status": "unavailable", "reason": str(
                dataset.get("message", "Chronicle dataset persistence failed"))[:320]}
        replay = _point_in_time_oracle_timeframe_votes(genome, timeframe_bars, decisions)
        if replay.get("status") not in {"complete", "partial"}:
            return replay
        return {
            **replay,
            "source": market.get("source"), "price_basis": market.get("price_basis"),
            "data_as_of": market.get("as_of"),
            "data_fingerprint": market.get("data_fingerprint"),
            "timeframe_fingerprints": fingerprints,
            "dataset_manifest": dataset,
            "incumbent_regime_at_lookup": incumbent.get("regime"),
        }

    def _research_oracle_no_trade_with_atlas(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Collect retryable, source-backed Atlas research for one no-trade case."""
        symbol = str(case.get("symbol", "")).upper()
        strategy_id = str(case.get("strategy_id", "unknown"))
        regime = str(case.get("regime", "unknown"))
        counts = case.get("outcome_counts", {})
        recent_decisions = case.get("recent_decisions", [])
        decision_evidence = case.get("decision_evidence")
        if not isinstance(decision_evidence, dict):
            decision_evidence = _summarize_oracle_no_trade_evidence(recent_decisions)
        timeframe_context = case.get("timeframe_context")
        primary = str(case.get("primary_observed_outcome", "no_trade_decisions"))
        threshold_days = case.get("threshold_days", 14)
        evaluation_count = case.get("evaluation_count_since_last_trade", 0)
        query = (
            f"Research why Oracle's demo strategy {strategy_id} for {symbol} in regime {regime} "
            f"has produced no trade for at least {threshold_days} days despite {evaluation_count} "
            f"logged strategy decisions. Observed decision outcomes: {counts}. The most frequent "
            f"observed outcome is {primary}. Recent decision evidence: "
            f"{json.dumps(recent_decisions[-12:], sort_keys=True, ensure_ascii=False)[:6000]}. "
            f"Aggregate decision diagnostics: {json.dumps(decision_evidence, sort_keys=True, ensure_ascii=False)[:2500]}. "
            f"Point-in-time seven-timeframe incumbent vote replay: "
            f"{json.dumps(timeframe_context, sort_keys=True, ensure_ascii=False)[:5000]}. "
            "Use the decision context to distinguish fusion holds, weak/degraded evidence, "
            "risk-gate rejections, and market-structure explanations. Treat aggregates as "
            "descriptive telemetry, not causal proof. Identify testable hypotheses; "
            "the timeframe replay uses only bars closed by each decision timestamp and is a raw incumbent-genome diagnostic, "
            "not the deployed V1 multi-timeframe policy or proof that a trade should have occurred. "
            "separate sourced evidence from speculation, cite sources, "
            "and do not infer a strategy component to ablate solely from the outcome label. "
            "Do not recommend weakening risk controls without controlled evidence."
        )
        query_fingerprint = hashlib.sha256(query.encode("utf-8")).hexdigest()
        atlas = getattr(self._forge, "atlas", None)
        if atlas is None:
            return {"status": "unavailable", "reason": "Atlas is not connected"}
        try:
            response = atlas.handle({
                "task": "research.investigate",
                "context": {"query": query, "domain": "forex strategy diagnostics",
                            "deep_research": True, "max_rounds": 2,
                            "deadline_sec": 60.0},
                "sender": "forge",
            })
        except Exception as exc:
            return {"status": "unavailable", "error": type(exc).__name__}
        report = response.get("report", {}) if isinstance(response, dict) else {}
        if not isinstance(report, dict):
            return {"status": "partial", "summary": str(report)[:12000], "sources": [],
                    "query_fingerprint": query_fingerprint}
        sources = report.get("sources", report.get("citations"))
        if not isinstance(sources, list):
            sources = report.get("evidence", [])
        sources = [source for source in sources if isinstance(source, (dict, str))]
        summary = str(report.get("summary", "")).strip()[:12000]
        response_complete = isinstance(response, dict) and response.get("status") in {
            "complete", "success"}
        complete = response_complete and bool(summary) and bool(sources)
        return {
            "status": "complete" if complete else "partial",
            "query_fingerprint": query_fingerprint,
            "report_id": str(report.get("report_id", ""))[:160] or None,
            "summary": summary,
            "confidence": report.get("confidence"),
            "sources": sources[:50],
            "source_status": report.get("source_status", {}),
            "key_terms": report.get("key_terms", []),
            "findings": report.get("findings", [])[:30]
            if isinstance(report.get("findings"), list) else [],
            "limitations": report.get("limitations", [])[:30]
            if isinstance(report.get("limitations"), list) else [],
            "contradictions": report.get("contradictions", [])[:20]
            if isinstance(report.get("contradictions"), list) else [],
            "consensus": report.get("consensus"),
            "dissent": report.get("dissent"),
            "completion_checks": {"atlas_response_complete": response_complete,
                                  "summary_present": bool(summary),
                                  "sources_present": bool(sources)},
        }

    def _record_atlas_hypothesis_evidence(self, hypothesis_id: str,
                                          research: Dict[str, Any],
                                          case: Dict[str, Any]) -> None:
        """Attach bounded Atlas provenance to the durable hypothesis record."""
        if not isinstance(research, dict):
            return
        summary = str(research.get("summary", "")).strip()
        sources = research.get("sources", [])
        if not summary and not sources:
            return
        atlas_report_id = research.get("report_id")
        identity_payload = {
            "report_id": atlas_report_id,
            "summary": summary,
            "sources": sources,
            "status": research.get("status"),
        }
        evidence_fingerprint = hashlib.sha256(json.dumps(
            identity_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False, default=str,
        ).encode("utf-8")).hexdigest()
        raw_confidence = research.get("confidence")
        confidence = (float(raw_confidence) if isinstance(raw_confidence, (int, float))
                      and not isinstance(raw_confidence, bool)
                      and math.isfinite(float(raw_confidence)) else None)
        result = {
            "evidence_id": f"atlas:{atlas_report_id}" if atlas_report_id
            else f"atlas-fingerprint:{evidence_fingerprint}",
            "evidence_role": "atlas_research_context",
            "research_status": research.get("status", "partial"),
            "atlas_report_id": atlas_report_id,
            "atlas_query_fingerprint": research.get("query_fingerprint"),
            "summary": summary[:12000],
            "confidence": confidence,
            "sources": sources[:50] if isinstance(sources, list) else [],
            "source_status": research.get("source_status", {}),
            "findings": research.get("findings", [])[:30],
            "limitations": research.get("limitations", [])[:30],
            "contradictions": research.get("contradictions", [])[:20],
            "consensus": research.get("consensus"),
            "dissent": research.get("dissent"),
            "observed_outcomes": case.get("outcome_counts", {}),
            "recent_decisions": case.get("recent_decisions", [])[-30:],
            "symbol": case.get("symbol"), "regime": case.get("regime"),
            "strategy_id": case.get("strategy_id"),
            "dataset_fingerprint": None,
            "oracle_observation_fingerprint": hashlib.sha256(json.dumps({
                "outcome_counts": case.get("outcome_counts", {}),
                "recent_decisions": case.get("recent_decisions", [])[-30:],
                "timeframe_context": case.get("timeframe_context"),
                "strategy_id": case.get("strategy_id"), "symbol": case.get("symbol"),
                "regime": case.get("regime"),
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                allow_nan=False, default=str).encode("utf-8")).hexdigest(),
            "timeframe_context": case.get("timeframe_context"),
            "reason": "Atlas research is provenance and context; only verified lockbox experiment evidence can settle this hypothesis.",
        }
        self._forge.hypothesis_queue.record_evidence(
            hypothesis_id, "atlas_source_backed_research_v1", result, sample_size=0)

    def _maybe_research_no_trade_case(self, chronicle: Any, summary_key: str,
                                     summary: Dict[str, Any], now_unix: float) -> Dict[str, Any]:
        """After sustained active no-trade evidence, ask Atlas and queue a hypothesis.

        Thresholds intentionally require both elapsed time and repeated Oracle
        decisions. This avoids treating a few quiet checks as a strategy failure.
        The case and hypothesis are persisted in Chronicle; this method never
        changes the incumbent or trade policy.
        """
        try:
            threshold_days = float(os.getenv("FORGE_ORACLE_NO_TRADE_DAYS", "14"))
            minimum_evaluations = int(os.getenv("FORGE_ORACLE_NO_TRADE_MIN_EVALUATIONS", "10"))
            max_evaluation_gap_hours = float(os.getenv(
                "FORGE_ORACLE_NO_TRADE_MAX_EVALUATION_GAP_HOURS", "48"))
        except ValueError:
            threshold_days, minimum_evaluations, max_evaluation_gap_hours = 14.0, 10, 48.0
        threshold_days = min(90.0, max(1.0, threshold_days))
        minimum_evaluations = min(10000, max(1, minimum_evaluations))
        max_evaluation_gap_hours = min(720.0, max(1.0, max_evaluation_gap_hours))
        evaluation_count = int(summary.get(
            "strategy_evaluations_since_last_trade", 0))
        if evaluation_count < minimum_evaluations:
            return {"status": "not_due", "reason": "minimum_observation_count_not_met"}
        last_strategy_eval = summary.get("last_strategy_evaluation_at_unix")
        if (not isinstance(last_strategy_eval, (int, float))
                or now_unix - last_strategy_eval > max_evaluation_gap_hours * 3600):
            return {"status": "not_due", "reason": "strategy_evaluations_not_recent"}
        baseline = summary.get("last_trade_at_unix") or summary.get("first_strategy_evaluation_at_unix")
        if not isinstance(baseline, (int, float)) or now_unix - baseline < threshold_days * 86400:
            return {"status": "not_due", "reason": "no_trade_window_not_elapsed"}
        episode_seconds = threshold_days * 86400
        episode_index = max(1, int((now_unix - baseline) // episode_seconds))
        episode_id = f"{int(baseline)}:{episode_index}"
        baseline_id = str(int(baseline))
        symbol = str(summary.get("symbol", "")).upper()
        strategy_id = str(summary.get("strategy_id", "unknown"))
        regime = str(summary.get("regime", "unknown"))
        hypothesis_id = "HYP-ORACLE-NO-TRADE-" + hashlib.sha256(
            f"{symbol}::{strategy_id}::{episode_id}".encode("utf-8")).hexdigest()[:20]
        case_key = f"forge.oracle.no_trade_case.{hashlib.sha256(hypothesis_id.encode()).hexdigest()}"
        existing_case = chronicle.act("state.get", {"key": case_key, "_sender": "forge"})
        if not isinstance(existing_case, dict) or existing_case.get("status") != "complete":
            return {"status": "pending", "reason": "Chronicle case lookup failed"}
        if existing_case.get("found"):
            saved_case = existing_case.get("value")
            prior_experiment = saved_case.get("experiment", {}) if isinstance(saved_case, dict) else {}
            if (isinstance(saved_case, dict) and isinstance(prior_experiment, dict)
                    and prior_experiment.get("status") in {None, "blocked"}):
                last_attempt = float(saved_case.get("last_experiment_attempt_at_unix", 0.0) or 0.0)
                if now_unix - last_attempt >= 3600.0:
                    if (not isinstance(saved_case.get("timeframe_context"), dict)
                            or saved_case["timeframe_context"].get("status") == "unavailable"):
                        saved_case["timeframe_context"] = self._build_no_trade_timeframe_context(
                            str(saved_case.get("symbol", "")).upper(),
                            str(saved_case.get("strategy_id", "")),
                            str(saved_case.get("regime", "unknown")),
                            saved_case.get("recent_decisions", []),
                        )
                    atlas_research = saved_case.get("atlas_research", {})
                    if not isinstance(atlas_research, dict) or atlas_research.get("status") != "complete":
                        saved_case["atlas_research"] = self._research_oracle_no_trade_with_atlas(
                            saved_case)
                    hypothesis_id_for_research = (saved_case.get("hypothesis") or {}).get("id")
                    if hypothesis_id_for_research:
                        self._record_atlas_hypothesis_evidence(
                            hypothesis_id_for_research,
                            saved_case.get("atlas_research", {}), saved_case)
                    retried = self._run_no_trade_hypothesis_experiment(chronicle, saved_case)
                    saved_case["experiment"] = retried
                    saved_case["experiment_attempts"] = int(saved_case.get("experiment_attempts", 1)) + 1
                    saved_case["last_experiment_attempt_at_unix"] = now_unix
                    hypothesis_id_in_case = (saved_case.get("hypothesis") or {}).get("id")
                    latest_hypothesis = self._forge.hypothesis_queue.get(hypothesis_id_in_case)
                    if latest_hypothesis is not None:
                        saved_case["hypothesis"] = latest_hypothesis.to_dict()
                    saved_case["lifecycle_status"] = (
                        "experiment_recorded" if retried.get("status") == "complete"
                        else "experiment_blocked")
                    updated_case = chronicle.act("state.set", {
                        "key": case_key, "value": saved_case,
                        "expected_revision": existing_case.get("revision", 0), "_sender": "forge",
                    })
                    if not isinstance(updated_case, dict) or updated_case.get("status") != "complete":
                        return {"status": "pending", "reason": "Chronicle could not persist the no-trade experiment retry"}
                    return {"status": retried.get("status"), "case_key": case_key,
                            "case_id": hypothesis_id,
                            "experiment_status": retried.get("status"),
                            "retry_count": saved_case["experiment_attempts"]}
            summary["last_no_trade_research_episode"] = episode_id
            summary["latest_no_trade_case_key"] = case_key
            return {"status": "already_open", "case_key": case_key,
                    "case_id": hypothesis_id}

        counts = summary.get("strategy_outcome_counts_since_last_trade")
        if not isinstance(counts, dict):
            counts = {}
        else:
            counts = dict(counts)
        blockers = {key: value for key, value in counts.items() if key != "trade_opened"}
        primary = max(blockers, key=blockers.get) if blockers else "no_trade_decisions"
        last_trade_at = summary.get("last_trade_at_unix")
        recent_decisions = [
            {key: item.get(key) for key in ("event_id", "at_unix", "outcome_code", "reason_codes", "details")
             if key in item}
            for item in summary.get("recent_events", [])[-30:]
            if isinstance(item, dict)
            and (not isinstance(last_trade_at, (int, float))
                 or float(item.get("at_unix", 0) or 0) >= float(last_trade_at))
        ]
        decision_evidence = _summarize_oracle_no_trade_evidence(recent_decisions)
        timeframe_context = self._build_no_trade_timeframe_context(
            symbol, strategy_id, regime, recent_decisions)
        research = self._research_oracle_no_trade_with_atlas({
            "symbol": symbol, "strategy_id": strategy_id, "regime": regime,
            "threshold_days": threshold_days,
            "evaluation_count_since_last_trade": evaluation_count,
            "outcome_counts": counts, "recent_decisions": recent_decisions,
            "decision_evidence": decision_evidence,
            "timeframe_context": timeframe_context,
            "primary_observed_outcome": primary,
        })
        selection_id = f"oracle-no-trade:{hypothesis_id}"
        ablation_selection = self.select_oracle_ablation({
            "symbol": symbol, "strategy_id": strategy_id, "regime": regime,
            "selection_id": selection_id,
            "candidate_targets": list(ORACLE_ABLATION_TARGETS),
        })
        selected_target = (ablation_selection.get("selected_target")
                           if isinstance(ablation_selection, dict)
                           and ablation_selection.get("status") == "complete" else None)

        research_summary = str(research.get("summary", "")).strip()
        hypothesis_basis = (research_summary[:1200] if research_summary else
                            "Atlas could not provide a source-backed explanation; investigate the observed decision evidence")
        if selected_target and ablation_selection.get("qualified_screen_total", 0) > 0:
            selected_target_text = (
                f"Forge selected the {selected_target} component using prior same-strategy, "
                "symbol, and regime ablation evidence. "
            )
        elif selected_target:
            selected_target_text = (
                f"Forge selected the {selected_target} component using its deterministic "
                "cold-start exploration policy because no qualified same-scope screens exist. "
            )
        else:
            selected_target_text = "Forge could not select a registered component yet. "
        statement = (
            f"For Oracle strategy {strategy_id} in regime {regime} on {symbol}, test whether the "
            f"Atlas research hypothesis ({hypothesis_basis}) is consistent with the recurring "
            f"no-trade observations ({primary}; counts={counts}; diagnostics={decision_evidence}). "
            f"{selected_target_text}A controlled component screen measures strategy performance; "
            "it does not by itself prove why Oracle did not trade. Keep live risk policy unchanged "
            "and require an untouched lockbox before treating the hypothesis as supported."
        )
        hypothesis = self._forge.hypothesis_queue.add(
            statement, "forge_oracle_no_trade_diagnosis", hypothesis_id)
        case = {
            "schema": "forge.oracle.no_trade_research_case.v1",
            "case_id": hypothesis_id, "symbol": symbol, "strategy_id": strategy_id,
            "regime": regime,
            "baseline_at_unix": baseline, "opened_at_unix": now_unix,
            "episode_id": episode_id, "episode_index": episode_index,
            "episode_start_at_unix": baseline + (episode_index * episode_seconds),
            "threshold_days": threshold_days, "minimum_evaluations": minimum_evaluations,
            "evaluation_count": summary.get("evaluations"),
            "evaluation_count_since_last_trade": evaluation_count,
            "outcome_counts": counts, "recent_decisions": recent_decisions,
            "decision_evidence": decision_evidence,
            "timeframe_context": timeframe_context,
            "primary_observed_outcome": primary,
            "atlas_research": research, "ablation_selection": ablation_selection,
            "hypothesis": hypothesis.to_dict(),
            "experiment_required": True, "promotion_performed": False,
            "risk_policy_changed": False,
        }
        self._record_atlas_hypothesis_evidence(hypothesis.id, research, case)
        hypothesis = self._forge.hypothesis_queue.get(hypothesis.id) or hypothesis
        case["hypothesis"] = hypothesis.to_dict()
        saved = chronicle.act("state.set", {
            "key": case_key, "value": case, "expected_revision": 0, "_sender": "forge",
        })
        if not isinstance(saved, dict) or saved.get("status") != "complete":
            return {"status": "pending", "reason": "Chronicle case persistence failed"}
        experiment = self._run_no_trade_hypothesis_experiment(chronicle, case)
        case["experiment"] = experiment
        case["experiment_attempts"] = 1
        case["last_experiment_attempt_at_unix"] = now_unix
        refreshed_hypothesis = self._forge.hypothesis_queue.get(hypothesis.id)
        if refreshed_hypothesis is not None:
            case["hypothesis"] = refreshed_hypothesis.to_dict()
        case["lifecycle_status"] = (
            "experiment_recorded" if experiment.get("status") == "complete"
            else "experiment_blocked" if experiment.get("status") == "blocked"
            else "research_hypothesis_recorded"
        )
        current_case = chronicle.act("state.get", {"key": case_key, "_sender": "forge"})
        if (isinstance(current_case, dict) and current_case.get("status") == "complete"
                and current_case.get("found")):
            updated_case = chronicle.act("state.set", {
                "key": case_key, "value": case,
                "expected_revision": current_case.get("revision", 0), "_sender": "forge",
            })
            if not isinstance(updated_case, dict) or updated_case.get("status") != "complete":
                log.warning("Could not append Oracle no-trade experiment result to Chronicle case %s",
                            hypothesis_id)
        for _ in range(3):
            current = chronicle.act("state.get", {"key": summary_key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete":
                break
            latest = current.get("value") if current.get("found") else summary
            if not isinstance(latest, dict):
                break
            latest["last_no_trade_research_episode"] = episode_id
            latest["latest_no_trade_case_key"] = case_key
            updated = chronicle.act("state.set", {
                "key": summary_key, "value": latest,
                "expected_revision": current.get("revision", 0), "_sender": "forge",
            })
            if isinstance(updated, dict) and updated.get("status") == "complete":
                break
        return {"status": research.get("status", "partial"),
                "case_id": hypothesis_id, "case_key": case_key,
                "hypothesis_id": hypothesis.id, "atlas_status": research.get("status"),
                "experiment_status": experiment.get("status")}

    @staticmethod
    def _load_chronicle_topdown_bundle(experiment: Dict[str, Any],
                                       repository_root: Path | None = None) -> Dict[str, Any]:
        """Reload the exact Chronicle snapshot used by a completed screen."""
        repository_root = (Path(repository_root) if repository_root is not None else
                           Path(__file__).resolve().parents[2]).resolve()
        artifact = experiment.get("dataset_manifest")
        if not isinstance(artifact, dict) or artifact.get("status") != "complete":
            raise ValueError("development screen has no durable Chronicle dataset manifest")

        def safe_path(relative: Any) -> Path:
            if not isinstance(relative, str) or not relative.strip():
                raise ValueError("Chronicle dataset path is missing")
            candidate = (repository_root / relative).resolve()
            if not candidate.is_relative_to(repository_root):
                raise ValueError("Chronicle dataset path escapes the repository")
            return candidate

        manifest_path = safe_path(artifact.get("manifest_path"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Chronicle dataset manifest is malformed")
        saved_fingerprint = manifest.get("manifest_fingerprint")
        canonical = {key: value for key, value in manifest.items()
                     if key != "manifest_fingerprint"}
        actual_fingerprint = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False).encode("utf-8")).hexdigest()
        if (saved_fingerprint != actual_fingerprint
                or artifact.get("manifest_fingerprint") != actual_fingerprint
                or manifest.get("data_fingerprint") != experiment.get("dataset_fingerprint")
                or artifact.get("data_fingerprint") != experiment.get("dataset_fingerprint")):
            raise ValueError("Chronicle dataset manifest fingerprint does not match the experiment")
        periods = ("W1", "D1", "H4", "H1", "M45", "M30", "M15")
        paths, expected_fingerprints = manifest.get("datasets"), manifest.get("timeframe_fingerprints")
        if (not isinstance(paths, dict) or set(paths) != set(periods)
                or not isinstance(expected_fingerprints, dict)
                or set(expected_fingerprints) != set(periods)):
            raise ValueError("Chronicle manifest must contain all seven timeframes")
        bars_by_timeframe: Dict[str, list] = {}
        for period in periods:
            with safe_path(paths[period]).open("r", encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            normalized = OracleImprovementService._normalize_bars(rows)
            actual = OracleImprovementService._fingerprint(normalized)
            if actual != expected_fingerprints[period]:
                raise ValueError(f"Chronicle {period} dataset no longer matches its fingerprint")
            bars_by_timeframe[period] = normalized
        combined = hashlib.sha256(json.dumps(
            {"symbol": str(manifest.get("symbol", "")).upper(),
             "period_fingerprints": expected_fingerprints,
             "price_basis": manifest.get("price_basis"),
             "schema": "oracle.topdown_ohlcv.v1"},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        if combined != manifest.get("data_fingerprint"):
            raise ValueError("Chronicle timeframe fingerprints do not reproduce the bundle fingerprint")
        return {
            "bars_by_timeframe": bars_by_timeframe,
            "timeframe_fingerprints": expected_fingerprints,
            "timeframe_coverage": manifest.get("timeframe_coverage", {}),
            "data_fingerprint": combined,
            "data_as_of": manifest.get("data_as_of"),
            "history_target": manifest.get("history_target"),
            "data_source": manifest.get("source"),
            "research_timeframes": list(periods),
        }

    def _run_no_trade_hypothesis_experiment(self, chronicle: Any,
                                           case: Dict[str, Any]) -> Dict[str, Any]:
        """Screen Forge's learned component choice, then test on a fresh lockbox."""
        primary_outcome = case.get("primary_observed_outcome")
        symbol = str(case.get("symbol", "")).strip().upper()
        strategy_id = str(case.get("strategy_id", "")).strip()
        regime = str(case.get("regime", "")).strip()
        selection_id = f"oracle-no-trade:{case.get('case_id', '')}"
        selection = case.get("ablation_selection")
        if (not isinstance(selection, dict) or selection.get("status") != "complete"
                or selection.get("selection_id") != selection_id):
            selection = self.select_oracle_ablation({
                "symbol": symbol, "strategy_id": strategy_id, "regime": regime,
                "selection_id": selection_id,
                "candidate_targets": list(ORACLE_ABLATION_TARGETS),
            })
        ablation_target = selection.get("selected_target") if isinstance(selection, dict) else None
        expected_selection_scope = {
            "strategy_id": strategy_id, "symbol": symbol, "regime": regime,
        }
        if (not isinstance(selection, dict) or selection.get("status") != "complete"
                or selection.get("scope") != expected_selection_scope
                or selection.get("candidate_targets") != list(ORACLE_ABLATION_TARGETS)
                or ablation_target not in ORACLE_ABLATION_TARGETS):
            return {"status": "blocked", "reason": "Chronicle could not select a registered component from durable ablation evidence",
                    "selection": selection}
        atlas = case.get("atlas_research") or {}
        sources = atlas.get("sources")
        if (atlas.get("status") != "complete" or not str(atlas.get("summary", "")).strip()
                or not isinstance(sources, list) or not sources):
            return {"status": "blocked",
                    "reason": "complete Atlas research with a summary and cited sources is required before an automatic hypothesis test"}
        if not strategy_id or strategy_id == "unknown":
            return {"status": "blocked", "reason": "the active Oracle strategy identity is unavailable"}

        latest_key = f"forge.oracle.cost_profile.latest.{symbol}"
        latest = chronicle.act("state.get", {"key": latest_key, "_sender": "forge"})
        if (not isinstance(latest, dict) or latest.get("status") != "complete"
                or not latest.get("found") or not isinstance(latest.get("value"), dict)):
            return {"status": "blocked", "reason": "no Chronicle cost-profile pointer exists for this symbol"}
        version_key = latest["value"].get("version_key")
        if not isinstance(version_key, str) or not version_key:
            return {"status": "blocked", "reason": "Chronicle cost-profile pointer is malformed"}
        version = chronicle.act("state.get", {"key": version_key, "_sender": "forge"})
        version_value = version.get("value") if isinstance(version, dict) else None
        costs = version_value.get("profile") if isinstance(version_value, dict) else None
        if (not isinstance(version, dict) or version.get("status") != "complete"
                or not version.get("found") or not isinstance(costs, dict)
                or version_value.get("status") != "complete"
                or costs.get("profile_status") != "api_observed_costs"):
            return {"status": "blocked", "reason": "Chronicle does not contain a qualified empirical cost profile"}
        configured_account = os.getenv("CTRADER_ACCOUNT_ID", "").strip()
        if (not configured_account or version_value.get("symbol") != symbol
                or str(version_value.get("account_id", "")) != configured_account
                or str(costs.get("account_id", "")) != configured_account):
            return {"status": "blocked", "reason": "qualified cost profile does not match the configured cTrader account and symbol"}

        try:
            import urllib.error
            import urllib.request

            base = os.getenv("ORACLE_V1_API_URL", "http://127.0.0.1:8001").rstrip("/")
            token = os.getenv("ORACLE_FORGE_BRIDGE_TOKEN", "").strip()
            if not token:
                return {"status": "blocked", "reason": "Oracle V1 incumbent bridge token is not configured"}
            payload = json.dumps({"symbol": symbol}, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                f"{base}/internal/oracle/forge/incumbent", data=payload, method="POST",
                headers={"Content-Type": "application/json", "X-Oracle-Forge-Token": token},
            )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                incumbent = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"status": "blocked", "reason": f"Oracle V1 incumbent lookup failed: {type(exc).__name__}"}
        if not isinstance(incumbent, dict) or incumbent.get("status") != "complete":
            return {"status": "blocked", "reason": "Oracle V1 did not return a persisted incumbent"}
        genome = incumbent.get("baseline_genome")
        genome_id = str(genome.get("genome_id", "")).strip() if isinstance(genome, dict) else ""
        if genome_id != strategy_id:
            return {"status": "blocked", "reason": "the active V1 genome differs from the strategy observed in this no-trade case"}
        if case.get("regime") not in {None, "", "unknown"} and incumbent.get("regime") != case.get("regime"):
            return {"status": "blocked", "reason": "Oracle V1 returned a different regime than the hypothesis context"}

        request_payload = {
            "symbol": symbol,
            "regime": str(incumbent.get("regime", "")).strip(),
            "baseline_genome": genome,
            "performance_metrics": incumbent.get("performance_metrics", {}),
            "risk_policy": incumbent.get("risk_policy"),
            "minimum_executed_trades": incumbent.get("minimum_executed_trades", 100),
            "execution_costs": costs,
            "reason": (f"no-trade research case {case.get('case_id')}: Forge selected the "
                       f"{ablation_target} ablation from prior same-scope evidence; "
                       "exploratory strategy-performance test only, live risk policy unchanged"),
            "evaluation_mode": "development_screen",
            "evaluation_engine_version": (
                f"oracle_no_trade_{primary_outcome}_{ablation_target}_episode_"
                f"{int(case.get('episode_index', 1))}_v2"),
            "ablation_targets": [ablation_target],
        }
        experiment = self.request(request_payload)
        if not isinstance(experiment, dict) or experiment.get("status") != "complete":
            return {"status": "blocked", "reason": (experiment.get("message", "Oracle component ablation did not complete")
                    if isinstance(experiment, dict) else "Oracle component ablation returned an invalid result"),
                    "request_status": experiment.get("status") if isinstance(experiment, dict) else "error"}
        experiment_id = str(experiment.get("experiment_id", "")).strip()
        if not experiment_id:
            return {"status": "blocked", "reason": "Forge completed the review without an experiment identity"}
        guardrails = experiment.get("guardrails") or {}
        trade_sample = experiment.get("trade_sample") or {}
        baseline_trades = trade_sample.get("baseline_trades")
        candidate_trades = trade_sample.get("candidate_trades")
        sample_size = min(baseline_trades, candidate_trades) if (
            isinstance(baseline_trades, int) and isinstance(candidate_trades, int)
            and baseline_trades >= 0 and candidate_trades >= 0) else 0
        evidence_result = {
            "supported": None,
            "evidence_role": "exploratory_development_screen_not_lockbox",
            "ablation_target": ablation_target,
            "ablation_selection": selection,
            "dataset_fingerprint": experiment.get("dataset_fingerprint"),
            "holdout_fingerprint": experiment.get("holdout_fingerprint"),
            "screen_interpretation": experiment.get("interpretation"),
            "recommendation": experiment.get("recommendation"),
            "guardrails": guardrails,
            "trade_sample": trade_sample,
            "result_metrics": experiment.get("result_metrics"),
            "reason": "An exploratory screen cannot confirm or reject the hypothesis; a fresh untouched lockbox and separate review are required.",
            "sample_size": sample_size,
            "promotion_performed": False,
        }
        try:
            hypothesis_id = str(case["hypothesis"]["id"])
            if self._forge.hypothesis_queue.get(hypothesis_id) is None:
                saved_hypothesis = case["hypothesis"]
                self._forge.hypothesis_queue.add(
                    str(saved_hypothesis.get("statement", "")),
                    str(saved_hypothesis.get("proposed_by", "forge_oracle_no_trade_diagnosis")),
                    hypothesis_id,
                )
            linked = self._forge.hypothesis_queue.link_experiment(hypothesis_id, experiment_id)
            self._forge.hypothesis_queue.record_evidence(
                linked.id, f"{ablation_target.replace('.', '_')}_component_development_screen_v1",
                evidence_result, sample_size=sample_size)
        except (AttributeError, KeyError, ValueError) as exc:
            return {"status": "blocked", "reason": f"hypothesis evidence update failed: {type(exc).__name__}",
                    "experiment_id": experiment_id, "record_key": experiment.get("record_key")}
        try:
            replay_bundle = self._load_chronicle_topdown_bundle(experiment)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"status": "blocked",
                    "reason": f"Chronicle development data cannot be replayed for lockbox confirmation: {type(exc).__name__}",
                    "development_experiment_id": experiment_id,
                    "record_key": experiment.get("record_key"),
                    "hypothesis_status": self._forge.hypothesis_queue.get(linked.id).status}
        lockbox_request = {
            **request_payload, **replay_bundle,
            "evaluation_mode": "lockbox_confirmation",
            "evaluation_engine_version": (
                f"oracle_no_trade_{primary_outcome}_{ablation_target}_episode_"
                f"{int(case.get('episode_index', 1))}_lockbox_v2"),
            "hypothesis_id": hypothesis_id,
        }
        lockbox = self.request(lockbox_request)
        if not isinstance(lockbox, dict) or lockbox.get("status") != "complete":
            return {"status": "blocked",
                    "reason": (lockbox.get("message", "lockbox confirmation did not complete")
                               if isinstance(lockbox, dict) else "lockbox returned an invalid result"),
                    "development_experiment_id": experiment_id,
                    "development_record_key": experiment.get("record_key"),
                    "lockbox_status": lockbox.get("status") if isinstance(lockbox, dict) else "error",
                    "hypothesis_status": self._forge.hypothesis_queue.get(linked.id).status}
        return {
            "status": "complete", "experiment_id": lockbox.get("experiment_id"),
            "record_key": lockbox.get("record_key"),
            "development_experiment_id": experiment_id,
            "development_record_key": experiment.get("record_key"),
            "method": f"registered_{ablation_target.replace('.', '_')}_development_then_lockbox_v2",
            "ablation_target": ablation_target,
            "ablation_selection": selection,
            "evidence_role": "single_lockbox_confirmation",
            "promotion_performed": False, "risk_policy_changed": False,
            "hypothesis_evidence": evidence_result,
            "lockbox_evidence": lockbox.get("hypothesis_evidence"),
            "hypothesis_status": lockbox.get("hypothesis_status",
                self._forge.hypothesis_queue.get(linked.id).status),
            "lockbox_recommendation": lockbox.get("recommendation"),
            "lockbox_guardrails": lockbox.get("guardrails"),
            "lockbox_trade_sample": lockbox.get("trade_sample"),
        }

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

    def collect_cost_observations(self, symbols: list[str]) -> Dict[str, Any]:
        """Collect read-only cTrader cost evidence and persist it through Chronicle."""
        if (not isinstance(symbols, list) or not symbols or len(symbols) > 100
                or any(not isinstance(item, str) or not item.strip() for item in symbols)):
            return {"status": "error", "message": "symbols must be a non-empty list of at most 100 names"}
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable; cost collection was not started"}
        normalized = list(dict.fromkeys(item.strip().upper() for item in symbols))
        result = self._research_data.collect_cost_observations(normalized)
        if not isinstance(result, dict) or result.get("status") != "complete":
            return result if isinstance(result, dict) else {"status": "error", "message": "cTrader collector returned an invalid response"}

        persisted = {}
        failures = []
        observations_by_symbol = {
            str(item.get("symbol", "")).upper(): item
            for item in result.get("observations", []) if isinstance(item, dict)
        }
        profiles = result.get("cost_profiles", {})
        for symbol in normalized:
            observation = observations_by_symbol.get(symbol)
            if observation is None:
                continue
            profile = profiles.get(symbol) if isinstance(profiles, dict) else None
            version = profile.get("profile_version") if isinstance(profile, dict) else result.get("observed_at", "unknown")
            version_hash = hashlib.sha256(str(version).encode("utf-8")).hexdigest()[:20]
            version_key = f"forge.oracle.cost_profile.version.{symbol}.{version_hash}"
            latest_key = f"forge.oracle.cost_profile.latest.{symbol}"
            record = {
                "schema": "forge.oracle.cost_profile_record.v1",
                "symbol": symbol,
                "status": observation.get("status", "blocked"),
                "account_id": profile.get("account_id") if isinstance(profile, dict) else None,
                "broker_symbol": observation.get("broker_symbol"),
                "profile": profile,
                "observation": observation,
                "observed_at": result.get("observed_at"),
                "source": result.get("source", "ctrader_open_api"),
            }
            current = chronicle.act("state.get", {"key": version_key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete":
                failures.append(symbol)
                continue
            if not current.get("found"):
                saved = chronicle.act("state.set", {
                    "key": version_key, "value": record,
                    "expected_revision": 0, "_sender": "forge",
                })
                if not isinstance(saved, dict) or saved.get("status") != "complete":
                    failures.append(symbol)
                    continue
            pointer = chronicle.act("state.get", {"key": latest_key, "_sender": "forge"})
            if not isinstance(pointer, dict) or pointer.get("status") != "complete":
                failures.append(symbol)
                continue
            pointed = chronicle.act("state.set", {
                "key": latest_key,
                "value": {"schema": "forge.oracle.cost_profile_pointer.v1",
                          "symbol": symbol, "version_key": version_key,
                          "profile_version": version, "status": record["status"],
                          "observed_at": record["observed_at"]},
                "expected_revision": pointer.get("revision", 0), "_sender": "forge",
            })
            if not isinstance(pointed, dict) or pointed.get("status") != "complete":
                failures.append(symbol)
                continue
            persisted[symbol] = {"version_key": version_key, "latest_key": latest_key,
                                 "status": record["status"]}

        return {**result,
                "chronicle_persistence": "partial" if failures else "complete",
                "persisted_symbols": persisted,
                "persistence_failures": sorted(set(failures))}

    def research_window_fingerprint(self, symbol: str) -> Dict[str, Any]:
        """Fetch and fingerprint Forge-owned market history for experiment deduplication."""
        market = self._research_data.fetch_topdown_bundle(symbol)
        if not isinstance(market, dict) or market.get("status") != "complete":
            return market if isinstance(market, dict) else {
                "status": "error", "message": "Forge market-data provider returned an invalid response"}
        daily = market.get("timeframes", {}).get("D1", [])
        latest_close = daily[-1].get("close") if daily and isinstance(daily[-1], dict) else None
        return {
            "status": "complete", "symbol": market.get("symbol"),
            "data_fingerprint": market.get("data_fingerprint"),
            "timeframe_fingerprints": market.get("timeframe_fingerprints"),
            "timeframe_coverage": market.get("timeframe_coverage"),
            "latest_close": latest_close, "data_as_of": market.get("as_of"),
            "history_target": market.get("history_target"),
            "data_source": market.get("source"), "price_basis": market.get("price_basis"),
        }

    def assess_market_data_drift(self, symbol: str) -> Dict[str, Any]:
        """Measure daily price-distribution shift using Forge-owned cTrader history.

        This is an observation only. It does not attribute cause, assess strategy
        performance, or produce a trading signal. It fetches only the daily bars
        needed for drift instead of downloading the full seven-timeframe strategy
        research bundle.
        """
        import statistics
        from datetime import timedelta, timezone

        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            return {"status": "error", "message": "symbol is required"}
        connection = self._research_data._connect()
        if not isinstance(connection, dict) or connection.get("status") != "complete":
            return connection if isinstance(connection, dict) else {
                "status": "blocked", "message": "Forge cTrader connection failed"}
        broker = connection["broker"]
        broker_symbol = self._research_data._symbol_aliases().get(normalized_symbol, normalized_symbol)
        as_of = datetime.now(timezone.utc)
        result = broker.historical_bars(
            symbol=broker_symbol,
            from_timestamp_ms=int((as_of - timedelta(days=500)).timestamp() * 1000),
            to_timestamp_ms=int(as_of.timestamp() * 1000),
            period="D1", count=1000, timeout=60.0,
        )
        if not isinstance(result, dict) or result.get("status") != "complete":
            message = result.get("message", "cTrader returned an invalid daily history response") if isinstance(result, dict) else "cTrader returned an invalid daily history response"
            return {"status": "blocked", "symbol": normalized_symbol,
                    "message": f"Forge D1 drift history unavailable: {message}"}
        if result.get("has_more"):
            return {"status": "blocked", "symbol": normalized_symbol,
                    "message": "Forge D1 drift history was truncated; refusing an incomplete comparison"}
        unique_bars = {}
        for item in result.get("bars", []):
            if not isinstance(item, dict):
                continue
            stamp = str(item.get("timestamp", ""))
            try:
                opened = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                values = {name: float(item[name]) for name in ("open", "high", "low", "close")}
                volume = float(item.get("volume", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            if (not all(math.isfinite(value) for value in (*values.values(), volume))
                    or not (values["low"] <= min(values["open"], values["close"])
                            <= max(values["open"], values["close"]) <= values["high"])
                    or volume < 0 or opened + timedelta(days=1) > as_of):
                continue
            unique_bars[opened] = {"timestamp": opened.isoformat(), **values, "volume": volume}
        bars = [unique_bars[stamp] for stamp in sorted(unique_bars)]
        if len(bars) < 311:
            return {"status": "blocked", "symbol": normalized_symbol,
                    "message": "at least 311 completed D1 bars are required for the drift comparison"}
        last_open = max(unique_bars)
        if self._research_data._bar_is_stale(
                last_open, 24 * 60 * 60 * 1000, as_of):
            return {"status": "blocked", "symbol": normalized_symbol,
                    "message": "Forge D1 drift history is stale"}

        baseline, recent = bars[-310:-30], bars[-30:]

        def features(items):
            result = []
            for previous, current in zip(items, items[1:]):
                previous_close = float(previous["close"])
                close = float(current["close"])
                if previous_close <= 0 or close <= 0:
                    raise ValueError("daily closes must be positive")
                result.append((math.log(close / previous_close),
                               (float(current["high"]) - float(current["low"])) / previous_close))
            return result

        baseline_features = features(baseline)
        recent_features = features(recent)

        def psi(reference, observed, bins=10):
            ordered = sorted(reference)
            edges = [ordered[min(len(ordered) - 1, int(i * len(ordered) / bins))]
                     for i in range(1, bins)]

            def proportions(values):
                counts = [0] * bins
                for value in values:
                    index = 0
                    while index < len(edges) and value > edges[index]:
                        index += 1
                    counts[index] += 1
                return [(count + 0.5) / (len(values) + bins * 0.5) for count in counts]

            expected, actual = proportions(reference), proportions(observed)
            return sum((actual_value - expected_value)
                       * math.log(actual_value / expected_value)
                       for expected_value, actual_value in zip(expected, actual))

        try:
            return_psi = psi([row[0] for row in baseline_features],
                             [row[0] for row in recent_features])
            range_psi = psi([row[1] for row in baseline_features],
                            [row[1] for row in recent_features])
            recent_volatility = statistics.pstdev(row[0] for row in recent_features)
            baseline_volatility = statistics.pstdev(row[0] for row in baseline_features)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            return {"status": "error", "symbol": normalized_symbol,
                    "message": f"invalid completed D1 drift input: {type(exc).__name__}"}

        recent_timestamps = [str(item.get("timestamp", "")) for item in recent]
        window_fingerprint = hashlib.sha256("|".join(recent_timestamps).encode("utf-8")).hexdigest()
        maximum_psi = max(return_psi, range_psi)
        classification = "high" if maximum_psi >= 0.25 else "moderate" if maximum_psi >= 0.10 else "low"
        return {
            "status": "complete", "symbol": normalized_symbol,
            "monitor": "forge_daily_market_data_distribution_v1",
            "classification": classification,
            "features": {
                "daily_log_return_psi": return_psi,
                "normalized_daily_range_psi": range_psi,
                "baseline_daily_return_volatility": baseline_volatility,
                "recent_daily_return_volatility": recent_volatility,
            },
            "sample": {"baseline_bars": len(baseline), "recent_bars": len(recent),
                       "bar_interval": "D1", "completed_bars_only": True},
            "window_fingerprint": window_fingerprint,
            "broker_symbol": broker_symbol,
            "data_as_of": as_of.isoformat(),
            "interpretation": (
                "Distribution shift indicator only; not strategy-performance drift, "
                "causal attribution, or a trading signal."),
        }

    def _validate_hypothesis_lockbox_request(self, hypothesis_id: str,
                                             ablation_targets: list[str],
                                             request: Dict[str, Any] | None = None,
                                             require_exact_context: bool = False) -> Dict[str, Any] | None:
        """Require a lockbox request to match an existing registered screen."""
        if len(ablation_targets) != 1:
            return {"status": "error",
                    "message": "hypothesis lockbox confirmation requires exactly one registered ablation target"}
        hypothesis = self._forge.hypothesis_queue.get(hypothesis_id)
        if hypothesis is None:
            return {"status": "error", "message": "hypothesis_id does not exist in Chronicle"}
        target = ablation_targets[0]
        registered_item = next((item for item in hypothesis.evidence if isinstance(item, dict)
            and isinstance(item.get("result"), dict)
            and item.get("result", {}).get("evidence_role")
                == "exploratory_development_screen_not_lockbox"
            and item.get("result", {}).get("ablation_target") == target
            and item.get("result", {}).get("experiment_id") in hypothesis.linked_experiment_ids), None)
        if registered_item is None:
            return {"status": "blocked",
                    "message": "hypothesis has no Chronicle-linked development screen for this exact ablation"}
        development_id = registered_item["result"].get("experiment_id")
        chronicle = getattr(self._forge, "chronicle", None)
        stored = (chronicle.act("state.get", {
            "key": f"forge.oracle.experiment.{development_id}", "_sender": "forge"})
            if chronicle is not None else None)
        development = stored.get("value") if isinstance(stored, dict) else None
        if (not isinstance(stored, dict) or stored.get("status") != "complete"
                or not stored.get("found") or not isinstance(development, dict)
                or development.get("status") != "complete"
                or development.get("evaluation_mode") != "development_screen"
                or (development.get("candidate_generation") or {}).get("ablation_targets") != [target]):
            return {"status": "blocked",
                    "message": "the registered development experiment is missing or differs in Chronicle"}
        if require_exact_context:
            profile = request.get("execution_costs") if isinstance(request, dict) else None
            risk_policy = request.get("risk_policy") if isinstance(request, dict) else None
            stored_profile = development.get("execution_cost_profile")
            stored_costs = development.get("execution_costs_bps")
            stored_risk = development.get("oracle_risk_policy")
            baseline = request.get("baseline_genome") if isinstance(request, dict) else None
            stored_baseline = development.get("baseline_genome")

            def same_json(left: Any, right: Any) -> bool:
                try:
                    return json.dumps(left, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False) == json.dumps(
                        right, sort_keys=True, separators=(",", ":"), allow_nan=False)
                except (TypeError, ValueError):
                    return False

            cost_fields = {
                "commission_bps": "commission_per_side",
                "slippage_bps": "slippage_per_side",
                "spread_bps": "spread_full",
                "stress_slippage_bps": "stress_slippage_per_side",
                "stress_spread_bps": "stress_spread_full",
            }
            same_costs = (isinstance(profile, dict) and isinstance(stored_profile, dict)
                          and isinstance(stored_costs, dict)
                          and all(profile.get(field) == stored_profile.get(field)
                                  for field in ("broker", "account_type", "profile_version", "price_basis"))
                          and all(isinstance(profile.get(field), (int, float))
                                  and not isinstance(profile.get(field), bool)
                                  and float(profile[field]) == float(stored_costs.get(stored_field, float("nan")))
                                  for field, stored_field in cost_fields.items()))
            same_risk = (isinstance(risk_policy, dict) and isinstance(stored_risk, dict)
                         and risk_policy.get("source") == stored_risk.get("source")
                         and isinstance(risk_policy.get("max_drawdown_fraction"), (int, float))
                         and not isinstance(risk_policy.get("max_drawdown_fraction"), bool)
                         and float(risk_policy["max_drawdown_fraction"])
                             == float(stored_risk.get("absolute_max_drawdown_fraction", float("nan"))))
            if (not isinstance(request, dict)
                    or request.get("symbol") != development.get("symbol")
                    or request.get("regime") != development.get("regime")
                    or request.get("data_fingerprint") != development.get("dataset_fingerprint")
                    or not same_json(baseline, stored_baseline)
                    or not same_costs or not same_risk):
                return {"status": "blocked",
                        "message": "lockbox context must exactly match the registered screen's data, incumbent, costs, risk policy, symbol, and regime"}
        return None

    def _attach_hypothesis_lockbox_evidence(self, chronicle: Any,
                                            result: Dict[str, Any],
                                            hypothesis_id: str) -> Dict[str, Any]:
        """Append a lockbox result to its registered hypothesis in Chronicle."""
        if result.get("status") != "complete":
            return result
        record_key = result.get("record_key")
        stored = chronicle.act("state.get", {"key": record_key, "_sender": "forge"})
        record = stored.get("value") if isinstance(stored, dict) else None
        design = record.get("evaluation_design", {}) if isinstance(record, dict) else {}
        if (not isinstance(record, dict) or record.get("status") != "complete"
                or record.get("evaluation_mode") != "lockbox_confirmation"
                or record.get("holdout_claim_status") != "claimed_once"
                or design.get("evidence_role") != "single_lockbox_confirmation"):
            return {**result, "status": "blocked",
                    "message": "Chronicle experiment does not prove a claimed-once lockbox evaluation"}
        target_list = (record.get("candidate_generation") or {}).get("ablation_targets", [])
        hypothesis = self._forge.hypothesis_queue.get(hypothesis_id)
        if hypothesis is None or len(target_list) != 1:
            return {**result, "status": "blocked",
                    "message": "lockbox evidence does not match one registered hypothesis ablation"}
        target = target_list[0]
        registered = any(
            item.get("result", {}).get("evidence_role")
                == "exploratory_development_screen_not_lockbox"
            and item.get("result", {}).get("ablation_target") == target
            and item.get("result", {}).get("experiment_id") in hypothesis.linked_experiment_ids
            for item in hypothesis.evidence if isinstance(item, dict)
            and isinstance(item.get("result"), dict))
        if not registered:
            return {**result, "status": "blocked",
                    "message": "lockbox ablation differs from the registered development screen"}

        trade_sample = record.get("trade_sample", {})
        sample_counts = [trade_sample.get(key) for key in (
            "baseline_trades", "candidate_trades", "stress_baseline_trades",
            "stress_candidate_trades")]
        required = int(trade_sample.get("minimum_required", 0) or 0)
        sample_met = (required > 0 and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= required
            for value in sample_counts))
        evaluation = record.get("evaluation", {})
        statistical_complete = (isinstance(evaluation, dict)
                                and evaluation.get("statistical_evidence_complete") is True)
        guardrails = record.get("guardrails", {})
        guards_complete = (isinstance(guardrails, dict) and bool(guardrails)
                           and all(isinstance(value, bool) for value in guardrails.values()))
        supported = (all(guardrails.values()) if sample_met and statistical_complete
                     and guards_complete else None)
        sample_size = min(sample_counts) if sample_met else 0
        evidence = {
            "evidence_id": str(record.get("experiment_id", "")),
            "experiment_id": str(record.get("experiment_id", "")),
            "evidence_role": "single_lockbox_confirmation",
            "evaluation_status": "complete",
            "holdout_claim_status": "claimed_once",
            "dataset_fingerprint": record.get("dataset_fingerprint"),
            "holdout_fingerprint": record.get("holdout_fingerprint"),
            "ablation_target": target,
            "supported": supported,
            "minimum_sample_met": sample_met,
            "statistical_evidence_complete": statistical_complete,
            "guardrails": guardrails,
            "recommendation": record.get("recommendation"),
            "trade_sample": trade_sample,
            "promotion_performed": False,
            "reason": "Lockbox evidence informs the hypothesis only; governed review is still required.",
        }
        try:
            self._forge.hypothesis_queue.link_experiment(
                hypothesis_id, evidence["experiment_id"])
            updated = self._forge.hypothesis_queue.record_evidence(
                hypothesis_id, f"{target.replace('.', '_')}_single_lockbox_v1",
                evidence, sample_size=sample_size)
        except (KeyError, ValueError, RuntimeError) as exc:
            return {**result, "status": "blocked",
                    "message": f"Chronicle hypothesis evidence update failed: {type(exc).__name__}"}
        return {**result, "hypothesis": updated.to_dict(),
                "hypothesis_status": updated.status,
                "hypothesis_evidence": evidence,
                "promotion_performed": False}

    @staticmethod
    def _claim_lockbox_once(chronicle: Any, holdout_fingerprint: str,
                            experiment_id: str, symbol: str,
                            regime: str) -> Dict[str, Any]:
        """Claim a holdout once while allowing recovery of its same experiment."""
        key = f"forge.oracle.holdout.{holdout_fingerprint}"
        current = chronicle.act("state.get", {"key": key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle holdout claim lookup failed"}
        expected = {"experiment_id": experiment_id, "symbol": symbol, "regime": regime}
        if current.get("found"):
            if current.get("value") == expected:
                return {"status": "complete", "recovered": True}
            return {"status": "blocked",
                    "message": "Chronicle holdout was already claimed by a different experiment"}
        return chronicle.act("state.set", {
            "key": key, "value": expected,
            "expected_revision": current.get("revision", 0), "_sender": "forge"})

    def request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict):
            return {"status": "error", "message": "request must be an object"}
        request = dict(request)
        evaluation_mode = str(request.get("evaluation_mode", "lockbox_confirmation")).strip().lower()
        if evaluation_mode not in {"lockbox_confirmation", "development_screen"}:
            return {"status": "error", "message": "evaluation_mode must be lockbox_confirmation or development_screen"}
        request["evaluation_mode"] = evaluation_mode
        requested_ablations = request.get("ablation_targets", [])
        if (not isinstance(requested_ablations, list)
                or any(not isinstance(target, str) for target in requested_ablations)):
            return {"status": "error", "message": "ablation_targets must be a list of component names"}
        if evaluation_mode == "development_screen" and not requested_ablations:
            return {"status": "error", "message": "development_screen is only available for registered component ablations"}
        hypothesis_id = str(request.get("hypothesis_id", "")).strip()
        if hypothesis_id:
            if evaluation_mode != "lockbox_confirmation":
                return {"status": "error",
                        "message": "hypothesis evidence can only be recorded from lockbox_confirmation"}
            registration_error = self._validate_hypothesis_lockbox_request(
                hypothesis_id, requested_ablations)
            if registration_error is not None:
                return registration_error
            request["hypothesis_id"] = hypothesis_id
        symbol = str(request.get("symbol", "")).strip().upper()
        regime = str(request.get("regime", "")).strip()
        if not symbol or not regime:
            return {"status": "error", "message": "symbol and regime are required"}
        if symbol and not isinstance(request.get("execution_costs"), dict):
            raw_profiles = (os.getenv("ORACLE_FORGE_COST_PROFILES_JSON_OVERRIDE", "").strip()
                            or os.getenv("ORACLE_FORGE_COST_PROFILES_JSON", "").strip())
            if raw_profiles:
                try:
                    profiles = json.loads(raw_profiles)
                    profile = profiles.get(symbol) if isinstance(profiles, dict) else None
                    if isinstance(profile, dict):
                        request["execution_costs"] = profile
                except json.JSONDecodeError:
                    return {"status": "blocked", "symbol": symbol,
                            "message": "Forge execution-cost profile JSON is invalid"}
            if not isinstance(request.get("execution_costs"), dict):
                profile_path = (Path(__file__).resolve().parents[2] / "Chronicle" / "memory" / "research"
                                / "oracle_cost_profiles" / symbol / "latest.json")
                try:
                    saved = json.loads(profile_path.read_text(encoding="utf-8"))
                    if (isinstance(saved, dict)
                            and saved.get("schema") == "forge.oracle.cost_profile_record.v1"
                            and saved.get("status") == "complete"
                            and isinstance(saved.get("profile"), dict)):
                        request["execution_costs"] = saved["profile"]
                except (OSError, json.JSONDecodeError):
                    pass
        if not isinstance(request.get("execution_costs"), dict):
            return {"status": "blocked", "symbol": symbol,
                    "message": "a versioned broker/account execution-cost profile is required"}
        profile = request["execution_costs"]
        evidence = profile.get("evidence")
        if (profile.get("profile_status") != "api_observed_costs"
                or not isinstance(evidence, dict)
                or any(isinstance(evidence.get(field), bool)
                       or not isinstance(evidence.get(field), int)
                       or evidence[field] < minimum
                       for field, minimum in (("symbol_filled_deals", 20),
                                              ("matched_slippage_deals", 20),
                                              ("matched_spread_quotes", 100)))):
            return {"status": "blocked", "symbol": symbol,
                    "message": ("cTrader-observed cost evidence requires at least 20 symbol fills, "
                                "20 matched fill/slippage observations, and 100 matched spread quotes")}
        required_cost_fields = ("commission_bps", "slippage_bps", "spread_bps",
                                "stress_slippage_bps", "stress_spread_bps")
        if (not all(isinstance(profile.get(field), (int, float))
                    and not isinstance(profile.get(field), bool)
                    and math.isfinite(float(profile[field])) and float(profile[field]) >= 0
                    for field in required_cost_fields)
                or not all(isinstance(profile.get(field), str) and profile[field].strip()
                           for field in ("broker", "account_type", "profile_version"))
                or profile.get("price_basis") != "bid"):
            return {"status": "blocked", "symbol": symbol,
                    "message": "the cTrader cost profile is incomplete or does not match bid-based history"}
        if not isinstance(request.get("baseline_genome"), dict):
            return {"status": "blocked", "symbol": symbol,
                    "message": "Oracle must send the active strategy genome before Forge can research it"}
        if not isinstance(request["baseline_genome"].get("modules"), dict):
            return {"status": "blocked", "symbol": symbol,
                    "message": "Oracle active genome has no supported strategy modules"}
        risk_policy = request.get("risk_policy")
        drawdown_cap = risk_policy.get("max_drawdown_fraction") if isinstance(risk_policy, dict) else None
        if (not isinstance(risk_policy, dict)
                or not isinstance(risk_policy.get("source"), str)
                or not risk_policy["source"].strip()
                or isinstance(drawdown_cap, bool)
                or not isinstance(drawdown_cap, (int, float))
                or not math.isfinite(float(drawdown_cap))
                or not 0 < float(drawdown_cap) <= 1):
            return {"status": "blocked", "symbol": symbol,
                    "message": "Oracle's valid authoritative drawdown limit and policy source are required before Forge can research"}
        if not isinstance(request.get("bars_by_timeframe"), dict):
            market = self._research_data.fetch_topdown_bundle(symbol)
            if market.get("status") != "complete":
                return {"status": "blocked", "symbol": symbol,
                        "message": market.get("message", "Forge could not obtain cTrader top-down history")}
            request.update({
                "bars_by_timeframe": market["timeframes"],
                "timeframe_fingerprints": market["timeframe_fingerprints"],
                "timeframe_coverage": market["timeframe_coverage"],
                "data_fingerprint": market["data_fingerprint"],
                "data_as_of": market["as_of"],
                "history_target": market["history_target"],
                "data_source": market["source"],
                "research_timeframes": list(market["timeframes"]),
            })
        if hypothesis_id:
            registration_error = self._validate_hypothesis_lockbox_request(
                hypothesis_id, requested_ablations, request=request,
                require_exact_context=True)
            if registration_error is not None:
                return registration_error
        key = f"{symbol}::{regime}"
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

        evaluation_mode = str(request.get("evaluation_mode", "lockbox_confirmation")).strip().lower()
        evaluation_engine_version = str(
            request.get("evaluation_engine_version", "oracle_topdown_v1")
        ).strip()
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
        cost_evidence = costs.get("evidence")
        if (costs.get("profile_status") != "api_observed_costs"
                or not isinstance(cost_evidence, dict)
                or isinstance(cost_evidence.get("symbol_filled_deals"), bool)
                or not isinstance(cost_evidence.get("symbol_filled_deals"), int)
                or cost_evidence["symbol_filled_deals"] < 20
                or isinstance(cost_evidence.get("matched_slippage_deals"), bool)
                or not isinstance(cost_evidence.get("matched_slippage_deals"), int)
                or cost_evidence["matched_slippage_deals"] < 20
                or isinstance(cost_evidence.get("matched_spread_quotes"), bool)
                or not isinstance(cost_evidence.get("matched_spread_quotes"), int)
                or cost_evidence["matched_spread_quotes"] < 100):
            return {"status": "blocked", "message": (
                "cTrader-observed cost evidence requires at least 20 symbol fills, "
                "20 matched fill/slippage observations, and 100 matched spread quotes")}
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
            "cost_normalized_trades", "pnl_basis", "exit_reason_feedback",
            "timeframe_policy_feedback",
        ) if key in demo_feedback_record}
        recent_demo_outcomes = demo_feedback_record.get("recent_outcomes", [])
        demo_feedback["recent_outcomes"] = (
            recent_demo_outcomes[-30:] if isinstance(recent_demo_outcomes, list) else [])

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
                # Timestamps are bar opens. Judge freshness after the final
                # bar's interval has elapsed; a new W1 bar can open seven days
                # before it completes and must not be rejected as stale.
                if self._research_data._bar_is_stale(
                        parsed[-1][0], periods[period] * 60 * 1000, as_of):
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
        full_evaluation_by_timeframe = evaluation_by_timeframe
        full_canonical_bars = canonical_bars
        holdout_start = max(40, int(len(full_canonical_bars) * 0.6))
        if holdout_start >= len(canonical_bars) - 30:
            return {"status": "blocked", "message": "the predeclared evaluation suffix needs at least 30 bars"}
        holdout_fingerprint = self._fingerprint(full_canonical_bars[holdout_start:])
        if evaluation_mode == "development_screen":
            # Keep the final chronological suffix untouched. Fit the learned
            # timeframe policy on the first 40%, then use the next 20% only as
            # an exploratory component screen. These results cannot authorize
            # promotion and are not reported as holdout evidence.
            evaluation_start_index = max(40, int(len(full_canonical_bars) * 0.4))
            evaluation_end_index = holdout_start
            if evaluation_end_index <= evaluation_start_index + 30:
                return {"status": "blocked", "message": "the development screen needs at least 30 chronological validation bars"}
            development_fingerprint = self._fingerprint(full_canonical_bars[:evaluation_start_index])
            evaluation_fingerprint = self._fingerprint(
                full_canonical_bars[evaluation_start_index:evaluation_end_index])
            evaluation_end = datetime.fromisoformat(
                full_canonical_bars[evaluation_end_index - 1]["timestamp"].replace("Z", "+00:00")
            ) + timedelta(minutes=15)
            run_timeframe_by_timeframe = {
                period: [item for item in items
                         if datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                         + timedelta(minutes=periods[period]) <= evaluation_end]
                for period, items in full_evaluation_by_timeframe.items()
            }
            canonical_bars = run_timeframe_by_timeframe["M15"]
        else:
            evaluation_start_index = holdout_start
            evaluation_end_index = len(full_canonical_bars)
            development_fingerprint = self._fingerprint(full_canonical_bars[:holdout_start])
            evaluation_fingerprint = holdout_fingerprint
            run_timeframe_by_timeframe = full_evaluation_by_timeframe
            canonical_bars = full_canonical_bars
        experiment_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"forge-oracle:{symbol}:{regime}:{baseline.genome_id}:{data_fingerprint}:"
            f"{evaluation_mode}:{evaluation_engine_version}:"
            f"{','.join(sorted(request.get('ablation_targets', [])))}",
        ))
        candidate_seed = int(hashlib.sha256(
            f"forge-oracle-candidate-v1:{symbol}:{regime}:{baseline.genome_id}:{development_fingerprint}".encode()
        ).hexdigest()[:8], 16)
        ablation_targets = request.get("ablation_targets", [])
        candidate = (StrategyGenome.from_dict(baseline.to_dict()) if ablation_targets else
                     self._mutate_candidate(baseline, random.Random(candidate_seed), rate=0.15))
        candidate_identity = candidate.to_dict()
        candidate_identity["genome_id"] = ""
        candidate.genome_id = "forge-" + hashlib.sha256(
            json.dumps(candidate_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]

        # A holdout belongs to one family. Until Forge has a new, independent
        # suffix, Chronicle rejects another candidate search on the same suffix.
        if evaluation_mode == "lockbox_confirmation":
            claim = self._claim_lockbox_once(
                chronicle, holdout_fingerprint, experiment_id, symbol, regime)
            if not isinstance(claim, dict) or claim.get("status") != "complete":
                return {"status": "blocked", "message": claim.get(
                    "message", "Chronicle holdout claim failed") if isinstance(claim, dict)
                    else "Chronicle holdout claim failed"}

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
                "execution_period": "M15", "execution_bars": len(full_canonical_bars),
            },
            "holdout_fingerprint": holdout_fingerprint,
            "holdout_claim_status": "not_claimed_development_screen" if evaluation_mode == "development_screen" else "claimed_once",
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
                "evaluation_engine_version": evaluation_engine_version,
                "method": ("chronological_development_validation_screen_v1"
                           if evaluation_mode == "development_screen" else "chronological_suffix_holdout_v1"),
                "start_index": evaluation_start_index,
                "end_index_exclusive": evaluation_end_index,
                "bootstrap_method": "paired circular block bootstrap",
                "bootstrap_replicates": 200,
                "confidence_level": 0.95,
                "candidate_family_size": 1,
                "evidence_role": ("exploratory_only_no_promotion_or_holdout_claim"
                                  if evaluation_mode == "development_screen" else "single_lockbox_confirmation"),
                "development_validation_fingerprint": evaluation_fingerprint,
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
        existing_experiment = chronicle.act("state.get", {
            "key": record_key, "_sender": "forge",
        })
        if (not isinstance(existing_experiment, dict)
                or existing_experiment.get("status") != "complete"):
            return {"status": "blocked", "message": "Chronicle experiment identity lookup failed"}
        if existing_experiment.get("found"):
            prior = existing_experiment.get("value")
            if not isinstance(prior, dict) or any((
                    prior.get("symbol") != symbol,
                    prior.get("regime") != regime,
                    prior.get("dataset_fingerprint") != data_fingerprint,
                    prior.get("evaluation_mode") != evaluation_mode,
                    (prior.get("candidate_generation") or {}).get("ablation_targets") != list(ablation_targets),
            )):
                return {"status": "blocked", "message": "Chronicle experiment ID conflicts with different research inputs"}
            if prior.get("status") != "complete":
                return {"status": "blocked", "message": "a matching Chronicle experiment exists but is not complete; manual reconciliation is required"}
            prior_result = prior.get("result") or {}
            prior_evaluation = prior.get("evaluation")
            return {
                "status": "complete", "experiment_id": experiment_id,
                "symbol": symbol, "regime": regime,
                "record_key": record_key,
                "evaluation_mode": prior.get("evaluation_mode"),
                "holdout_claim_status": prior.get("holdout_claim_status"),
                "recommendation": prior.get("recommendation", "none"),
                "interpretation": prior.get("interpretation"),
                "promotion_performed": False,
                "guardrails": prior.get("guardrails"),
                "trade_sample": prior.get("trade_sample"),
                "evaluation": prior_evaluation,
                "result_metrics": prior_result.get("metrics"),
                "recovered_from_chronicle": True,
            } if not hypothesis_id else self._attach_hypothesis_lockbox_evidence(
                chronicle, {
                    "status": "complete", "experiment_id": experiment_id,
                    "symbol": symbol, "regime": regime, "record_key": record_key,
                    "evaluation_mode": prior.get("evaluation_mode"),
                    "holdout_claim_status": prior.get("holdout_claim_status"),
                    "recommendation": prior.get("recommendation", "none"),
                    "interpretation": prior.get("interpretation"),
                    "promotion_performed": False, "guardrails": prior.get("guardrails"),
                    "trade_sample": prior.get("trade_sample"),
                    "evaluation": prior_evaluation,
                    "result_metrics": prior_result.get("metrics"),
                    "recovered_from_chronicle": True,
                }, hypothesis_id)
        stored = chronicle.act("state.set", {
            "key": record_key, "value": record, "expected_revision": 0,
            "_sender": "forge",
        })
        if not isinstance(stored, dict) or stored.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle could not persist the experiment record"}

        try:
            with tempfile.TemporaryDirectory(prefix="forge-oracle-") as directory:
                timeframe_datasets = {}
                for period, period_bars in run_timeframe_by_timeframe.items():
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
                        "evaluation_start_index": evaluation_start_index,
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
            elif evaluation_mode == "development_screen":
                recommendation = "development_screen_only"
                interpretation = "exploratory_validation_not_lockbox_or_promotion_evidence"
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
            finished = self._finish_record(chronicle, record_key, record, {
                "status": "complete",
                "evaluation_mode": evaluation_mode,
                "recommendation": recommendation,
                "interpretation": interpretation,
                "promotion_performed": False,
                "promotion_block_reason": (
                    "development screen is exploratory and cannot authorize promotion"
                    if evaluation_mode == "development_screen"
                    else "manual profile includes modeled slippage and cannot authorize promotion"
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
            if hypothesis_id and finished.get("status") == "complete":
                finished = self._attach_hypothesis_lockbox_evidence(
                    chronicle, finished, hypothesis_id)
            return finished
        except Exception as exc:
            return self._finish_record(chronicle, record_key, record, {
                "status": "failed", "error": f"{type(exc).__name__}: {exc}",
            })

    def select_oracle_ablation(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Select a scheduled development-screen ablation from durable evidence."""
        if not isinstance(request, dict):
            return {"status": "error", "message": "ablation selection request must be an object"}
        symbol = str(request.get("symbol", "")).strip().upper()
        strategy_id = str(request.get("strategy_id", "")).strip()
        regime = str(request.get("regime", "")).strip()
        selection_id = str(request.get("selection_id", "")).strip()
        targets = request.get("candidate_targets", list(ORACLE_ABLATION_TARGETS))
        if (not symbol or len(symbol) > 32 or not strategy_id or len(strategy_id) > 160
                or not regime or len(regime) > 64 or not selection_id or len(selection_id) > 240
                or not isinstance(targets, list) or not targets
                or len(targets) > len(ORACLE_ABLATION_TARGETS)
                or any(not isinstance(target, str) or target not in ORACLE_ABLATION_TARGETS
                       for target in targets) or len(set(targets)) != len(targets)):
            return {"status": "error", "message": "ablation selector scope or target list is invalid"}
        chronicle = getattr(self._forge, "chronicle", None)
        if chronicle is None or not hasattr(chronicle, "act"):
            return {"status": "blocked", "message": "Chronicle persistence is unavailable"}

        scope = {"strategy_id": strategy_id, "symbol": symbol, "regime": regime}
        scope_hash = hashlib.sha256(
            f"{strategy_id}::{symbol}::{regime}".encode("utf-8")).hexdigest()
        learning_key = f"forge.oracle.ablation_learning.v2.{scope_hash}"
        selection_hash = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
        selection_key = f"forge.oracle.ablation_selection.{selection_hash}"
        checkpoint = self._load_ablation_checkpoint(scope_hash, scope)
        if checkpoint.get("status") == "error":
            return {"status": "blocked", "message": checkpoint.get("message")}
        prior = chronicle.act("state.get", {"key": selection_key, "_sender": "forge"})
        if not isinstance(prior, dict) or prior.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle selection idempotency lookup failed"}
        if not prior.get("found"):
            checkpoint_selection = checkpoint.get("selections", {}).get(selection_id)
            if isinstance(checkpoint_selection, dict):
                restored = chronicle.act("state.set", {
                    "key": selection_key, "value": checkpoint_selection,
                    "expected_revision": 0, "_sender": "forge",
                })
                if not isinstance(restored, dict) or restored.get("status") != "complete":
                    return {"status": "blocked", "message": "Chronicle could not restore the durable ablation selection"}
                return {"status": "complete", **checkpoint_selection,
                        "reused": True, "promotion_performed": False}
        if prior.get("found"):
            saved = prior.get("value")
            if (not isinstance(saved, dict) or saved.get("scope") != scope
                    or saved.get("candidate_targets") != targets):
                return {"status": "blocked", "message": "selection ID conflicts with a different scope"}
            return {"status": "complete", **saved, "reused": True,
                    "promotion_performed": False}

        current = chronicle.act("state.get", {"key": learning_key, "_sender": "forge"})
        if not isinstance(current, dict) or current.get("status") != "complete":
            return {"status": "blocked", "message": "Chronicle ablation evidence lookup failed"}
        value = current.get("value") if current.get("found") else {
            "schema": "forge.oracle.ablation_learning.v2", "scope": scope,
            "targets": {}, "updated_at_unix": time.time(),
        }
        if not current.get("found") and isinstance(checkpoint.get("learning"), dict):
            value = checkpoint["learning"]
            restored = chronicle.act("state.set", {
                "key": learning_key, "value": value,
                "expected_revision": 0, "_sender": "forge",
            })
            if not isinstance(restored, dict) or restored.get("status") != "complete":
                return {"status": "blocked", "message": "Chronicle could not restore durable ablation lessons"}
            current = chronicle.act("state.get", {"key": learning_key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete" or not current.get("found"):
                return {"status": "blocked", "message": "Chronicle could not verify restored ablation lessons"}
            value = current.get("value")
        if not isinstance(value, dict) or value.get("scope") != scope:
            return {"status": "blocked", "message": "Chronicle ablation evidence is malformed or scoped incorrectly"}
        stats = value.get("targets", {})
        if not isinstance(stats, dict):
            return {"status": "blocked", "message": "Chronicle ablation target statistics are malformed"}
        total_valid = sum(
            max(0, int(stats.get(target, {}).get("qualified_screens", 0)))
            for target in targets if isinstance(stats.get(target, {}), dict)
        )
        scores = {}
        for target in targets:
            item = stats.get(target, {})
            if not isinstance(item, dict):
                item = {}
            count = max(0, int(item.get("qualified_screens", 0)))
            reward_sum = float(item.get("reward_sum", 0.0))
            mean_reward = reward_sum / count if count else 0.5
            win_rate_sum = float(item.get("trade_win_rate_delta_sum", 0.0))
            mean_trade_win_rate_delta = win_rate_sum / count if count else None
            exploration_bonus = 0.35 * math.sqrt(
                math.log(total_valid + len(targets) + 1) / (count + 1))
            scores[target] = {
                "qualified_screens": count,
                "inconclusive_screens": max(0, int(item.get("inconclusive_screens", 0))),
                "mean_screen_score": round(mean_reward, 6),
                "mean_trade_win_rate_delta": (
                    round(mean_trade_win_rate_delta, 6)
                    if mean_trade_win_rate_delta is not None else None),
                "exploration_bonus": round(exploration_bonus, 6),
                "selection_score": round(mean_reward + exploration_bonus, 6),
            }
        best_score = max(item["selection_score"] for item in scores.values())
        tied = [target for target, item in scores.items()
                if math.isclose(item["selection_score"], best_score, rel_tol=0.0, abs_tol=1e-12)]
        selected = min(tied, key=lambda target: hashlib.sha256(
            f"{selection_id}::{target}".encode("utf-8")).hexdigest())
        result = {
            "schema": "forge.oracle.ablation_selection.v1",
            "selection_id": selection_id, "scope": scope,
            "candidate_targets": list(targets), "selected_target": selected,
            "selection_scores": scores,
            "selection_policy": "chronicle_trade_win_rate_screen_ucb_v2",
            "qualified_screen_total": total_valid,
            "promotion_performed": False, "selected_at_unix": time.time(),
        }
        saved_selection = chronicle.act("state.set", {
            "key": selection_key, "value": result, "expected_revision": 0,
            "_sender": "forge",
        })
        if not isinstance(saved_selection, dict) or saved_selection.get("status") != "complete":
            existing = chronicle.act("state.get", {"key": selection_key, "_sender": "forge"})
            existing_value = existing.get("value") if isinstance(existing, dict) else None
            if not isinstance(existing_value, dict) or existing_value.get("scope") != scope:
                return {"status": "blocked", "message": "Chronicle could not persist ablation selection"}
            result = existing_value
        checkpoint_selections = checkpoint.get("selections", {})
        if not isinstance(checkpoint_selections, dict):
            checkpoint_selections = {}
        checkpoint_selections[selection_id] = result
        checkpoint_selections = dict(list(checkpoint_selections.items())[-1000:])
        checkpoint_saved = self._write_ablation_checkpoint(
            scope_hash, scope, value, checkpoint_selections)
        if checkpoint_saved.get("status") != "complete":
            return {"status": "blocked", "message": checkpoint_saved.get(
                "message", "durable ablation checkpoint could not be saved")}
        return {"status": "complete", **result, "reused": False}

    @staticmethod
    def _ablation_checkpoint_path(scope_hash: str) -> Path:
        configured = os.getenv("FORGE_ORACLE_ABLATION_MEMORY_DIR", "").strip()
        directory = (Path(configured) if configured else
                     Path(__file__).resolve().parents[2] / "Chronicle" / "memory" / "research" /
                     "oracle_ablation_learning")
        return directory / f"{scope_hash}.json"

    @classmethod
    def _load_ablation_checkpoint(cls, scope_hash: str,
                                  scope: Dict[str, Any]) -> Dict[str, Any]:
        path = cls._ablation_checkpoint_path(scope_hash)
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"status": "missing", "learning": None, "selections": {}}
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "error", "message": f"ablation checkpoint is unreadable: {type(exc).__name__}"}
        if (not isinstance(checkpoint, dict)
                or checkpoint.get("schema") != "forge.oracle.ablation_memory_checkpoint.v1"
                or checkpoint.get("scope_hash") != scope_hash
                or checkpoint.get("scope") != scope
                or not isinstance(checkpoint.get("learning"), dict)
                or checkpoint["learning"].get("schema") != "forge.oracle.ablation_learning.v2"
                or checkpoint["learning"].get("scope") != scope
                or not isinstance(checkpoint.get("selections", {}), dict)):
            return {"status": "error", "message": "ablation checkpoint schema or scope is invalid"}
        target_map = checkpoint["learning"].get("targets")
        if not isinstance(target_map, dict) or any(
                target not in ORACLE_ABLATION_TARGETS or not isinstance(record, dict)
                for target, record in target_map.items()):
            return {"status": "error", "message": "ablation checkpoint target map is invalid"}
        for record in target_map.values():
            qualified = record.get("qualified_screens")
            inconclusive = record.get("inconclusive_screens")
            reward_sum = record.get("reward_sum")
            trade_delta_sum = record.get("trade_win_rate_delta_sum", 0.0)
            experiment_ids = record.get("seen_experiment_ids")
            if (not isinstance(qualified, int) or isinstance(qualified, bool) or qualified < 0
                    or not isinstance(inconclusive, int) or isinstance(inconclusive, bool) or inconclusive < 0
                    or not isinstance(reward_sum, (int, float)) or isinstance(reward_sum, bool)
                    or not math.isfinite(float(reward_sum)) or not 0 <= float(reward_sum) <= qualified
                    or not isinstance(trade_delta_sum, (int, float)) or isinstance(trade_delta_sum, bool)
                    or not math.isfinite(float(trade_delta_sum)) or abs(float(trade_delta_sum)) > qualified
                    or not isinstance(experiment_ids, list) or len(experiment_ids) > 10000
                    or any(not isinstance(item, str) or not item for item in experiment_ids)
                    or len(experiment_ids) != len(set(experiment_ids))):
                return {"status": "error", "message": "ablation checkpoint contains invalid target statistics"}
        for selection_id, selection in checkpoint.get("selections", {}).items():
            if (not isinstance(selection_id, str) or not selection_id
                    or not isinstance(selection, dict)
                    or selection.get("schema") != "forge.oracle.ablation_selection.v1"
                    or selection.get("selection_id") != selection_id
                    or selection.get("scope") != scope
                    or selection.get("selected_target") not in ORACLE_ABLATION_TARGETS
                    or selection.get("promotion_performed") is not False):
                return {"status": "error", "message": "ablation checkpoint selection history is invalid"}
        return checkpoint

    @classmethod
    def _write_ablation_checkpoint(cls, scope_hash: str, scope: Dict[str, Any],
                                   learning: Dict[str, Any],
                                   selections: Dict[str, Any]) -> Dict[str, Any]:
        path = cls._ablation_checkpoint_path(scope_hash)
        if (learning.get("schema") != "forge.oracle.ablation_learning.v2"
                or learning.get("scope") != scope):
            return {"status": "error", "message": "refusing to checkpoint invalid ablation learning state"}
        checkpoint = {
            "schema": "forge.oracle.ablation_memory_checkpoint.v1",
            "scope_hash": scope_hash, "scope": scope,
            "learning": learning,
            "selections": dict(list(selections.items())[-1000:]),
            "updated_at_unix": time.time(),
        }
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(checkpoint, sort_keys=True, indent=2,
                                        allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return {"status": "complete", "path": str(path)}
        except (OSError, TypeError, ValueError) as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            return {"status": "error", "message": f"could not persist Chronicle ablation checkpoint: {type(exc).__name__}"}

    @staticmethod
    def _record_ablation_screen_lesson(
        chronicle: Any, original: Dict[str, Any], update: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Accumulate qualified, single-target development-screen evidence."""
        generation = original.get("candidate_generation")
        targets = generation.get("ablation_targets") if isinstance(generation, dict) else None
        if (update.get("status") != "complete"
                or original.get("evaluation_mode") != "development_screen"
                or not isinstance(targets, list) or len(targets) != 1
                or targets[0] not in ORACLE_ABLATION_TARGETS):
            return {"status": "not_applicable"}
        target = targets[0]
        trade_sample = update.get("trade_sample")
        guardrails = update.get("guardrails")
        evaluation = update.get("evaluation")
        if not isinstance(trade_sample, dict) or not isinstance(guardrails, dict):
            return {"status": "inconclusive", "reason": "screen_evidence_missing"}
        required = trade_sample.get("minimum_required")
        counts = [trade_sample.get(key) for key in (
            "baseline_trades", "candidate_trades",
            "stress_baseline_trades", "stress_candidate_trades",
        )]
        result_payload = update.get("result")
        result_evidence = result_payload.get("evidence") if isinstance(result_payload, dict) else None
        baseline_result = (result_evidence.get("baseline_full_result")
                           if isinstance(result_evidence, dict) else None)
        candidate_result = (result_evidence.get("treatment_full_result")
                            if isinstance(result_evidence, dict) else None)
        baseline_stress = (result_evidence.get("baseline_stress_result")
                           if isinstance(result_evidence, dict) else None)
        candidate_stress = (result_evidence.get("treatment_stress_result")
                            if isinstance(result_evidence, dict) else None)
        trade_rates = [
            baseline_result.get("win_rate_raw") if isinstance(baseline_result, dict) else None,
            candidate_result.get("win_rate_raw") if isinstance(candidate_result, dict) else None,
            baseline_stress.get("win_rate_raw") if isinstance(baseline_stress, dict) else None,
            candidate_stress.get("win_rate_raw") if isinstance(candidate_stress, dict) else None,
        ]
        qualified = (
            isinstance(required, int) and required > 0
            and all(isinstance(count, int) and not isinstance(count, bool) and count >= required
                    for count in counts)
            and all(isinstance(rate, (int, float)) and not isinstance(rate, bool)
                    and math.isfinite(float(rate)) and 0.0 <= float(rate) <= 1.0
                    for rate in trade_rates)
            and guardrails.get("statistical_evidence_complete") is True
        )
        strategy_id = str((original.get("baseline_genome") or {}).get("genome_id", "")).strip()
        symbol = str(original.get("symbol", "")).strip().upper()
        regime = str(original.get("regime", "")).strip()
        experiment_id = str(original.get("experiment_id", "")).strip()
        if not strategy_id or not symbol or not regime or not experiment_id:
            return {"status": "inconclusive", "reason": "screen_scope_missing"}
        scope = {"strategy_id": strategy_id, "symbol": symbol, "regime": regime}
        scope_hash = hashlib.sha256(
            f"{strategy_id}::{symbol}::{regime}".encode("utf-8")).hexdigest()
        key = f"forge.oracle.ablation_learning.v2.{scope_hash}"
        checkpoint = OracleImprovementService._load_ablation_checkpoint(scope_hash, scope)
        if checkpoint.get("status") == "error":
            return {"status": "blocked", "reason": checkpoint.get("message")}
        reward = None
        if qualified:
            comparisons = evaluation.get("comparisons", []) if isinstance(evaluation, dict) else []
            comparison_by_metric = {
                item.get("metric"): item for item in comparisons
                if isinstance(item, dict) and isinstance(item.get("metric"), str)
            } if isinstance(comparisons, list) else {}

            def effect(metric: str, scale: float, higher_is_better: bool) -> float:
                item = comparison_by_metric.get(metric, {})
                delta = item.get("absolute_change")
                if not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
                    return 0.0
                normalized = float(delta) / scale
                if not higher_is_better:
                    normalized = -normalized
                return math.tanh(normalized)

            trade_win_rate_delta = 0.5 * (
                (float(trade_rates[1]) - float(trade_rates[0]))
                + (float(trade_rates[3]) - float(trade_rates[2]))
            )
            trade_win_rate_effect = math.tanh(trade_win_rate_delta / 0.05)
            combined_effect = (
                0.50 * trade_win_rate_effect
                + 0.30 * effect("period_sharpe", 0.25, True)
                + 0.20 * effect("period_max_drawdown", 0.05, False)
            )
            reward = 0.5 + 0.45 * combined_effect
            safety = (
                "candidate_profitable_after_costs", "absolute_drawdown_cap_respected",
                "stress_candidate_profitable_after_costs", "stress_absolute_drawdown_cap_respected",
                "relative_max_drawdown_not_worse", "relative_trade_win_rate_not_worse",
                "stress_relative_max_drawdown_not_worse",
                "stress_relative_trade_win_rate_not_worse",
            )
            if any(guardrails.get(name) is not True for name in safety):
                reward = min(reward, 0.2)

        for _ in range(8):
            current = chronicle.act("state.get", {"key": key, "_sender": "forge"})
            if not isinstance(current, dict) or current.get("status") != "complete":
                return {"status": "blocked", "reason": "learning_record_lookup_failed"}
            value = current.get("value") if current.get("found") else {
                "schema": "forge.oracle.ablation_learning.v2", "scope": scope,
                "targets": {}, "updated_at_unix": time.time(),
            }
            if not current.get("found") and isinstance(checkpoint.get("learning"), dict):
                value = checkpoint["learning"]
                restored = chronicle.act("state.set", {
                    "key": key, "value": value, "expected_revision": 0,
                    "_sender": "forge",
                })
                if not isinstance(restored, dict) or restored.get("status") != "complete":
                    return {"status": "blocked", "reason": "durable_learning_restore_failed"}
                current = chronicle.act("state.get", {"key": key, "_sender": "forge"})
                if not isinstance(current, dict) or current.get("status") != "complete" or not current.get("found"):
                    return {"status": "blocked", "reason": "durable_learning_restore_unverified"}
                value = current.get("value")
            if not isinstance(value, dict) or value.get("scope") != scope:
                return {"status": "blocked", "reason": "learning_record_scope_invalid"}
            by_target = value.setdefault("targets", {})
            if not isinstance(by_target, dict):
                return {"status": "blocked", "reason": "learning_target_map_invalid"}
            item = by_target.setdefault(target, {
                "qualified_screens": 0, "inconclusive_screens": 0,
                "reward_sum": 0.0, "seen_experiment_ids": [],
            })
            if not isinstance(item, dict):
                return {"status": "blocked", "reason": "learning_target_record_invalid"}
            seen = item.setdefault("seen_experiment_ids", [])
            if not isinstance(seen, list):
                return {"status": "blocked", "reason": "learning_idempotency_record_invalid"}
            if experiment_id in seen:
                return {"status": "complete", "qualified": qualified,
                        "duplicate": True, "target": target, "score": reward}
            if qualified:
                item["qualified_screens"] = int(item.get("qualified_screens", 0)) + 1
                item["reward_sum"] = float(item.get("reward_sum", 0.0)) + float(reward)
                item["latest_score"] = float(reward)
                item["trade_win_rate_delta_sum"] = float(item.get("trade_win_rate_delta_sum", 0.0)) + trade_win_rate_delta
                item["latest_trade_win_rate_delta"] = trade_win_rate_delta
            else:
                item["inconclusive_screens"] = int(item.get("inconclusive_screens", 0)) + 1
            item["seen_experiment_ids"] = (seen + [experiment_id])[-10000:]
            item["latest_experiment_id"] = experiment_id
            value["updated_at_unix"] = time.time()
            saved = chronicle.act("state.set", {
                "key": key, "value": value,
                "expected_revision": current.get("revision", 0), "_sender": "forge",
            })
            if isinstance(saved, dict) and saved.get("status") == "complete":
                selections = checkpoint.get("selections", {})
                if not isinstance(selections, dict):
                    selections = {}
                checkpoint_saved = OracleImprovementService._write_ablation_checkpoint(
                    scope_hash, scope, value, selections)
                if checkpoint_saved.get("status") != "complete":
                    return {"status": "blocked", "reason": checkpoint_saved.get(
                        "message", "durable_learning_checkpoint_write_failed")}
                return {"status": "complete", "qualified": qualified,
                        "duplicate": False, "target": target, "score": reward,
                        "learning_key": key}
        return {"status": "blocked", "reason": "learning_record_compare_and_set_exhausted"}

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
        lesson = OracleImprovementService._record_ablation_screen_lesson(
            chronicle, original, final)
        return {"status": update.get("status", "complete"), "experiment_id": original["experiment_id"],
                "symbol": original["symbol"], "regime": original["regime"],
                "dataset_fingerprint": original.get("dataset_fingerprint"),
                "timeframe_fingerprints": original.get("timeframe_fingerprints"),
                "timeframe_coverage": original.get("timeframe_coverage"),
                "shared_overlap": original.get("shared_overlap"),
                "dataset_manifest": original.get("dataset_manifest"),
                "holdout_fingerprint": original.get("holdout_fingerprint"),
            "evaluation_mode": original.get("evaluation_mode"),
            "holdout_claim_status": original.get("holdout_claim_status"),
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
                "ablation_learning_update": lesson,
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
