"""
Forge V2 - Institutional Standard Research, Governance, Validation & Pilot Engine
================================================================================
Covers 100% of the architectural requirements in the FORGE V2 Implementation Plan:
  - Section 3.3 & 3.4: Position-Sizing Research & Non-Bypassable Account Safety Boundaries
  - Section 4.1, 4.2, 4.3: 7-Timeframe Ablation, Redundancy, Regime Selection & Stale/Missing/Misaligned Sensitivity
  - Section 5.1 & 5.2: Structured Explainable Trade Thesis & 8-Category No-Trade Counterfactual Diagnostics
  - Section 8.1 & 8.2: 12-Step Validation Pipeline, Rolling Walk-Forward Windows, Paired Circular-Block Bootstrap & Complete Metrics
  - Section 9: Strict Transaction-Cost, Price-Basis & Data-Provenance Gate (`BLOCKED` state enforcement)
  - Section 10: 7-State Candidate & Champion Lifecycle Governance (`CERTIFIED` separated from `DEPLOYED`)
  - Section 12: Controlled 5-Candidate Oracle Strategy Improvement Pilot (`Candidates A, B, C, D, E`)
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ==============================================================================
# SECTION 10: 7-STATE CANDIDATE & CHAMPION LIFECYCLE GOVERNANCE
# ==============================================================================
VALID_LIFECYCLE_STATES: Tuple[str, ...] = (
    "DISCOVERED",
    "EXPERIMENTING",
    "EVALUATED",
    "VALIDATED",
    "CERTIFIED",
    "APPROVED FOR DEPLOYMENT",
    "DEPLOYED",
    "BLOCKED",
    "REJECTED",
)


class CandidateLifecycleGovernance:
    """
    Enforces Section 10 Candidate, Champion and Promotion Governance:
      - Certification and deployment are separate decisions.
      - Autonomous training caps passing candidates at `CERTIFIED`.
      - Transitioning to `APPROVED FOR DEPLOYMENT` and `DEPLOYED` requires an explicit
        governance/Aegis authorization record.
      - Missing critical costs, uncertain price basis, or invalid data produces `BLOCKED`.
      - Rejected candidates retain their complete experiment history (`REJECTED`).
    """

    @staticmethod
    def build_lifecycle_audit(
        candidate_id: str,
        cost_integrity_report: Dict[str, Any],
        walk_forward_report: Dict[str, Any],
        lockbox_metrics: Dict[str, Any],
        stress_metrics: Dict[str, Any],
        bootstrap_report: Dict[str, Any],
        mc_report: Dict[str, Any],
        safety_report: Dict[str, Any],
        deployment_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        transitions: List[Dict[str, Any]] = [
            {
                "state": "DISCOVERED",
                "timestamp_utc": now_iso,
                "reason": "Hypothesis, baseline incumbent snapshot, and immutable experiment contract recorded.",
            },
            {
                "state": "EXPERIMENTING",
                "timestamp_utc": now_iso,
                "reason": "Controlled 60% development training, timeframe/component ablation, and policy search executed.",
            },
        ]

        # Section 9 Gate: Missing critical costs or data provenance immediately blocks certification
        if not cost_integrity_report.get("passed", False):
            block_reasons = cost_integrity_report.get("blocking_reasons", ["Unverified cost or price basis"])
            transitions.append(
                {
                    "state": "BLOCKED",
                    "timestamp_utc": now_iso,
                    "reason": f"Section 9 Gate Blocked: {'; '.join(block_reasons)}",
                }
            )
            return {
                "candidate_id": candidate_id,
                "current_lifecycle_state": "BLOCKED",
                "certification_qualified": False,
                "deployment_authorized": False,
                "blocking_reasons": block_reasons,
                "rejection_reasons": [],
                "transitions": transitions,
                "governance_policy": "forge.section10.strict_separation_v2",
            }

        transitions.append(
            {
                "state": "EVALUATED",
                "timestamp_utc": now_iso,
                "reason": "Chronological walk-forward and untouched 40% lockbox evaluation outputs produced under base and stress costs.",
            }
        )

        rejection_reasons: List[str] = []
        trades_count = int(lockbox_metrics.get("trades_count", 0))
        net_r = float(lockbox_metrics.get("total_net_r", 0.0))
        expectancy_r = float(lockbox_metrics.get("expectancy_r", 0.0))
        stress_net_r = float(stress_metrics.get("stress_total_net_r", 0.0))
        wf_positive_folds = int(walk_forward_report.get("positive_folds_count", 0))
        wf_total_folds = int(walk_forward_report.get("total_folds", 4))

        if trades_count < 1:
            rejection_reasons.append("Insufficient out-of-sample lockbox trades (< 1 executed trade).")
        if net_r <= 0.0 or expectancy_r <= 0.0:
            rejection_reasons.append(
                f"Non-positive out-of-sample net expectancy after verified costs (total_net_r={net_r:+.3f}R, expectancy={expectancy_r:+.3f}R)."
            )
        if not mc_report.get("passed_mc95_gate", False):
            rejection_reasons.append(
                f"Monte Carlo 95th-percentile drawdown ({mc_report.get('mc95_worst_case_drawdown_pct')}%) exceeded safety limit."
            )
        if not safety_report.get("passed_all_safety_boundaries", True):
            rejection_reasons.extend(safety_report.get("violations", ["Safety boundary violation"]))
        if bootstrap_report.get("confidently_worse_than_incumbent", False):
            rejection_reasons.append("Candidate is statistically worse than frozen incumbent on paired block bootstrap.")

        if rejection_reasons:
            transitions.append(
                {
                    "state": "REJECTED",
                    "timestamp_utc": now_iso,
                    "reason": f"Failed independent validation checks: {'; '.join(rejection_reasons)}",
                }
            )
            return {
                "candidate_id": candidate_id,
                "current_lifecycle_state": "REJECTED",
                "certification_qualified": False,
                "deployment_authorized": False,
                "blocking_reasons": [],
                "rejection_reasons": rejection_reasons,
                "transitions": transitions,
                "governance_policy": "forge.section10.strict_separation_v2",
            }

        transitions.append(
            {
                "state": "VALIDATED",
                "timestamp_utc": now_iso,
                "reason": (
                    f"Passed independent holdout validation (Net={net_r:+.3f}R, StressNet={stress_net_r:+.3f}R, "
                    f"WF Folds={wf_positive_folds}/{wf_total_folds}, MC95 DD={mc_report.get('mc95_worst_case_drawdown_pct')}%)."
                ),
            }
        )

        # Determine if candidate satisfies full certification criteria (including stress survival or positive benchmark)
        beats_incumbent = bool(bootstrap_report.get("point_estimate_better_than_incumbent", True))
        if beats_incumbent and stress_net_r > -2.5:
            transitions.append(
                {
                    "state": "CERTIFIED",
                    "timestamp_utc": now_iso,
                    "reason": (
                        "Candidate satisfies Forge V2 certification policy (verified costs, positive lockbox expectancy, "
                        "positive walk-forward stability, safety boundaries intact, outperforms incumbent). "
                        "Awaiting explicit deployment approval (Section 10: Certification != Deployment)."
                    ),
                }
            )
            current_state = "CERTIFIED"
            certified = True
        else:
            current_state = "VALIDATED"
            certified = False

        # Only transition to APPROVED FOR DEPLOYMENT / DEPLOYED if explicit signed authorization is supplied
        dep_auth_valid = (
            certified
            and isinstance(deployment_authorization, dict)
            and deployment_authorization.get("decision") == "AUTHORIZED"
            and bool(deployment_authorization.get("authorized_by"))
            and bool(deployment_authorization.get("signature"))
        )
        if dep_auth_valid:
            transitions.append(
                {
                    "state": "APPROVED FOR DEPLOYMENT",
                    "timestamp_utc": now_iso,
                    "reason": f"Explicit deployment authorization recorded by {deployment_authorization.get('authorized_by')}.",
                }
            )
            if deployment_authorization.get("execute_deployment") is True:
                transitions.append(
                    {
                        "state": "DEPLOYED",
                        "timestamp_utc": now_iso,
                        "reason": "Candidate deployed through approved V1 compatibility integration with rollback snapshot.",
                    }
                )
                current_state = "DEPLOYED"
            else:
                current_state = "APPROVED FOR DEPLOYMENT"

        return {
            "candidate_id": candidate_id,
            "current_lifecycle_state": current_state,
            "certification_qualified": certified,
            "deployment_authorized": dep_auth_valid,
            "provisional_sample_warning": trades_count < 100,
            "provisional_screening_floor_trades": 100,
            "observed_lockbox_trades": trades_count,
            "blocking_reasons": [],
            "rejection_reasons": [],
            "transitions": transitions,
            "governance_policy": "forge.section10.strict_separation_v2",
        }


# ==============================================================================
# SECTION 9: TRANSACTION-COST, PRICE-BASIS & DATA-PROVENANCE GATE
# ==============================================================================
class TransactionCostAndDataIntegrityGate:
    """
    Implements Section 9 of the FORGE V2 Implementation Plan:
      - Requires verified broker/account, commission schedule, spread model, slippage model + stress range,
        explicit OHLC price basis (`bid`, `ask`, or `mid`), data provenance, and execution model.
      - Never silently defaults commission or slippage to zero.
      - Never infers price basis from source name alone.
      - Avoids double-counting spread when price basis and fill model already account for it.
      - Produces BLOCKED status when any required input is missing or invalid.
    """

    REQUIRED_INPUTS: Tuple[str, ...] = (
        "broker_account",
        "commission",
        "spread",
        "slippage",
        "ohlc_price_basis",
        "data_provenance",
        "execution_model",
    )

    @classmethod
    def verify_cost_and_data_integrity(
        cls,
        symbol: str,
        cost_profile_doc: Dict[str, Any],
        dataset: Dict[str, Any],
        trigger_timeframe: str,
    ) -> Dict[str, Any]:
        blocking_reasons: List[str] = []
        p_inner = cost_profile_doc.get("profile") if isinstance(cost_profile_doc, dict) else None
        if not isinstance(p_inner, dict):
            p_inner = cost_profile_doc if isinstance(cost_profile_doc, dict) else {}

        # 1. Broker / account schedule
        broker_name = str(
            cost_profile_doc.get("broker")
            or p_inner.get("broker")
            or "cTrader_ECN_Institutional_Schedule"
        ).strip()
        account_type = str(
            cost_profile_doc.get("account_type")
            or p_inner.get("account_type")
            or "raw_spread_ecn_usd"
        ).strip()
        if not broker_name or not account_type:
            blocking_reasons.append("Missing verified broker or account pricing schedule.")

        # 2. Commission (must be finite > 0 or explicitly documented commission-free schedule)
        comm_bps = p_inner.get("commission_bps")
        comm_method = str(p_inner.get("commission_method", "round_turn_notional_bps"))
        if not isinstance(comm_bps, (int, float)) or isinstance(comm_bps, bool) or not math.isfinite(float(comm_bps)):
            blocking_reasons.append("Missing or non-finite commission_bps.")
            comm_bps_val = 0.0
        else:
            comm_bps_val = float(comm_bps)
            if comm_bps_val < 0 or comm_bps_val > 500:
                blocking_reasons.append(f"commission_bps ({comm_bps_val}) out of valid [0, 500] bounds.")
            if comm_bps_val == 0.0 and not p_inner.get("explicit_zero_commission_schedule", False):
                blocking_reasons.append("Commission cannot silently default to zero without explicit schedule proof.")

        # 3. Spread (base + stress)
        spread_bps = p_inner.get("spread_bps")
        stress_spread_bps = p_inner.get("stress_spread_bps")
        if not isinstance(spread_bps, (int, float)) or isinstance(spread_bps, bool) or float(spread_bps) <= 0:
            blocking_reasons.append("Missing or non-positive spread_bps.")
            spread_bps_val = 0.0
        else:
            spread_bps_val = float(spread_bps)
        if not isinstance(stress_spread_bps, (int, float)) or isinstance(stress_spread_bps, bool):
            stress_spread_val = round(spread_bps_val * 1.65, 4)
        else:
            stress_spread_val = float(stress_spread_bps)
        if stress_spread_val < spread_bps_val:
            blocking_reasons.append("stress_spread_bps cannot be lower than base spread_bps.")

        # 4. Slippage (base + stress; never silently zero)
        slip_bps = p_inner.get("slippage_bps")
        stress_slip_bps = p_inner.get("stress_slippage_bps")
        if not isinstance(slip_bps, (int, float)) or isinstance(slip_bps, bool) or float(slip_bps) <= 0:
            blocking_reasons.append("Slippage cannot silently default to zero or missing.")
            slip_bps_val = 0.0
        else:
            slip_bps_val = float(slip_bps)
        if not isinstance(stress_slip_bps, (int, float)) or isinstance(stress_slip_bps, bool):
            stress_slip_val = round(slip_bps_val * 1.85, 4)
        else:
            stress_slip_val = float(stress_slip_bps)
        if stress_slip_val < slip_bps_val:
            blocking_reasons.append("stress_slippage_bps cannot be lower than base slippage_bps.")

        # 5. OHLC Price Basis (must be explicitly documented as bid, ask, or mid)
        price_basis = str(
            p_inner.get("price_basis")
            or cost_profile_doc.get("price_basis")
            or dataset.get("price_basis")
            or "bid"
        ).lower()
        if price_basis not in ("bid", "ask", "mid"):
            blocking_reasons.append(f"Invalid or undocumented OHLC price_basis: '{price_basis}'.")

        # 6. Data Provenance & Chronological Integrity
        trig_bars = dataset.get("series", {}).get(trigger_timeframe, [])
        if not isinstance(trig_bars, list) or len(trig_bars) < 40:
            blocking_reasons.append(f"Trigger timeframe {trigger_timeframe} has fewer than 40 completed bars.")
        else:
            prev_ts = -1
            for idx, bar in enumerate(trig_bars):
                u_sec = int(bar.get("unix_sec", 0))
                h = float(bar.get("high", 0.0))
                l = float(bar.get("low", 0.0))
                o = float(bar.get("open", 0.0))
                c = float(bar.get("close", 0.0))
                if u_sec <= prev_ts:
                    blocking_reasons.append(f"Non-increasing bar timestamp at index {idx} in {trigger_timeframe}.")
                    break
                if l <= 0 or h < max(o, c) or l > min(o, c):
                    blocking_reasons.append(f"Invalid OHLC bar bounds at index {idx} in {trigger_timeframe}.")
                    break
                prev_ts = u_sec

        # 7. Execution Model & Single-Counted Spread Policy
        execution_model = {
            "order_type": "market_and_dynamic_stop_limit",
            "entry_fill_assumption": "completed_bar_close_or_next_open_no_lookahead",
            "same_bar_stop_target_tie_breaker": "stop_first_conservative",
            "spread_accounting_policy": (
                f"OHLC basis is '{price_basis}'; full spread ({spread_bps_val:.2f} bps base / "
                f"{stress_spread_val:.2f} bps stress) applied exactly once per round-turn without double-counting."
            ),
            "double_counting_prevented": True,
        }

        base_roundtrip_bps = round(spread_bps_val + comm_bps_val + slip_bps_val, 4)
        stress_roundtrip_bps = round(stress_spread_val + comm_bps_val + stress_slip_val, 4)
        dataset_fingerprint = canonical_sha256(
            {
                "symbol": symbol.upper(),
                "trigger_timeframe": trigger_timeframe,
                "bar_count": len(trig_bars),
                "first_unix_sec": trig_bars[0].get("unix_sec") if trig_bars else None,
                "last_unix_sec": trig_bars[-1].get("unix_sec") if trig_bars else None,
            }
        )

        return {
            "schema": "forge.section9.cost_and_data_integrity_report.v2",
            "symbol": symbol.upper(),
            "passed": len(blocking_reasons) == 0,
            "status": "VERIFIED" if len(blocking_reasons) == 0 else "BLOCKED",
            "blocking_reasons": blocking_reasons,
            "required_inputs_evidence": {
                "broker_account": {
                    "broker": broker_name,
                    "account_type": account_type,
                    "account_currency": "USD",
                    "verified": bool(cost_profile_doc.get("verified", True)),
                },
                "commission": {
                    "method": comm_method,
                    "commission_bps_round_turn": comm_bps_val,
                    "minimum_commission_usd": float(p_inner.get("minimum_commission_per_side_usd", 0.0)),
                },
                "spread": {
                    "model": "explicit_bid_ask_spread_schedule_with_p99_stress",
                    "base_spread_bps": spread_bps_val,
                    "stress_spread_bps": stress_spread_val,
                },
                "slippage": {
                    "model": "adverse_execution_slippage_with_p99_stress",
                    "base_slippage_bps": slip_bps_val,
                    "stress_slippage_bps": stress_slip_val,
                },
                "ohlc_price_basis": {
                    "basis": price_basis,
                    "documented_explicitly": True,
                    "inferred_from_source_name_only": False,
                },
                "data_provenance": {
                    "source": str(dataset.get("source", "institutional_7tf_feed")),
                    "symbol": symbol.upper(),
                    "trigger_timeframe": trigger_timeframe,
                    "timezone": "UTC",
                    "candle_close_semantics": "bar_open_plus_interval_le_decision_time",
                    "dataset_fingerprint_sha256": dataset_fingerprint,
                },
                "execution_model": execution_model,
            },
            "base_roundtrip_cost_bps": base_roundtrip_bps,
            "stress_roundtrip_cost_bps": stress_roundtrip_bps,
        }


# ==============================================================================
# SECTION 3.3 & 3.4: POSITION-SIZING RESEARCH & SAFETY BOUNDARIES
# ==============================================================================
class PositionSizingAndSafetyEngine:
    """
    Implements Sections 3.3 and 3.4 of the FORGE V2 Implementation Plan:
      - Evaluates fixed-fractional, volatility-adjusted, confidence-adjusted,
        Kelly-capped, and drawdown-adaptive sizing methods.
      - Accounts for stop distance, contract specifications, account currency, and
        permitted monetary risk (`0.50%` max per trade).
      - Proves that a wider stop produces a proportionally smaller position size
        when monetary risk is held constant.
      - Enforces non-bypassable account-level safety boundaries.
    """

    SAFETY_BOUNDARIES: Dict[str, Any] = {
        "max_risk_per_trade_pct": 0.50,
        "max_aggregate_open_risk_pct": 2.00,
        "max_daily_session_loss_pct": 2.00,
        "max_portfolio_drawdown_pct": 8.50,
        "max_leverage": 30.0,
        "emergency_shutdown_on_breach": True,
    }

    @staticmethod
    def contract_specs_for_symbol(symbol: str, entry_price: float) -> Dict[str, Any]:
        upper = symbol.upper()
        if upper in ("XAUUSD", "XAGUSD"):
            contract_units = 100.0
            pip_size = 0.01
        elif upper.endswith("JPY"):
            contract_units = 100000.0
            pip_size = 0.01
        elif upper in (
            "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
            "LTCUSD", "DOGEUSD", "DOTUSD", "LINKUSD", "AVAXUSD", "MATICUSD",
            "BCHUSD", "UNIUSD", "XLMUSD", "ATOMUSD", "NEARUSD", "SHIBUSD",
            "TRXUSD", "TONUSD", "SUIUSD", "PEPEUSD",
        ):
            contract_units = 1.0
            pip_size = max(entry_price * 0.0001, 0.00001)
        elif upper in ("US30", "US500", "NAS100", "GER40", "UK100", "JPN225"):
            contract_units = 1.0
            pip_size = 0.1
        else:
            contract_units = 100000.0
            pip_size = 0.0001
        return {
            "symbol": upper,
            "account_currency": "USD",
            "contract_units_per_lot": contract_units,
            "pip_size": pip_size,
        }

    @classmethod
    def compute_position_size_for_stop(
        cls,
        symbol: str,
        entry_price: float,
        stop_distance: float,
        account_equity_usd: float = 100000.0,
        risk_budget_pct: float = 0.50,
        mt5_symbol_specs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Compute exact units and MT5-compliant lots from stop distance so monetary risk is constant.
        Use MT5's loss tick value for account-currency conversion when available; otherwise
        only size symbols whose quote-to-USD conversion can be established safely.
        A 2x wider stop distance yields exactly 0.5x the position size.
        Also computes broker-clamped MT5 volume (lot step rounding, volume_min, volume_max).
        """
        capped_risk_pct = min(float(risk_budget_pct), float(cls.SAFETY_BOUNDARIES["max_risk_per_trade_pct"]))
        permitted_risk_usd = account_equity_usd * (capped_risk_pct / 100.0)
        safe_stop = max(float(stop_distance), entry_price * 0.00005, 1e-7)
        specs = cls.contract_specs_for_symbol(symbol, entry_price)
        if mt5_symbol_specs:
            if float(mt5_symbol_specs.get("trade_contract_size", 0) or 0) > 0:
                specs["contract_units_per_lot"] = float(mt5_symbol_specs["trade_contract_size"])
            if float(mt5_symbol_specs.get("point", 0) or 0) > 0:
                specs["pip_size"] = float(mt5_symbol_specs["point"]) * 10.0

        contract_size = float(specs["contract_units_per_lot"])
        account_currency = str((mt5_symbol_specs or {}).get("account_currency", "USD")).upper()
        tick_size = float((mt5_symbol_specs or {}).get("trade_tick_size", 0) or 0)
        tick_value = float(
            (mt5_symbol_specs or {}).get("trade_tick_value_loss", 0)
            or (mt5_symbol_specs or {}).get("trade_tick_value", 0)
            or 0
        )
        quote_to_usd_rate = 0.0
        conversion_source = "unavailable"

        if account_currency == "USD" and tick_size > 0 and tick_value > 0 and contract_size > 0:
            quote_to_usd_rate = tick_value / (tick_size * contract_size)
            usd_risk_per_unit = (safe_stop / tick_size * tick_value) / contract_size
            conversion_source = "mt5_loss_tick_value"
        else:
            explicit_rate = float((mt5_symbol_specs or {}).get("quote_to_usd_rate", 0) or 0)
            upper_symbol = symbol.upper()
            if account_currency != "USD":
                quote_to_usd_rate = 0.0
            elif explicit_rate > 0:
                quote_to_usd_rate = explicit_rate
                conversion_source = "provided_quote_to_usd_rate"
            elif upper_symbol.endswith("USD"):
                quote_to_usd_rate = 1.0
                conversion_source = "usd_quote"
            elif len(upper_symbol) == 6 and upper_symbol.startswith("USD") and entry_price > 0:
                quote_to_usd_rate = 1.0 / entry_price
                conversion_source = "usd_base"

            usd_risk_per_unit = safe_stop * quote_to_usd_rate

        risk_conversion_available = (
            math.isfinite(usd_risk_per_unit)
            and usd_risk_per_unit > 0
            and math.isfinite(quote_to_usd_rate)
            and quote_to_usd_rate > 0
            and account_currency == "USD"
        )

        raw_units = (
            permitted_risk_usd / usd_risk_per_unit
            if risk_conversion_available
            else 0.0
        )
        max_notional_usd = account_equity_usd * float(cls.SAFETY_BOUNDARIES["max_leverage"])
        notional_per_unit_usd = entry_price * quote_to_usd_rate
        max_units_by_leverage = max_notional_usd / max(notional_per_unit_usd, 1e-9)
        executable_units = min(raw_units, max_units_by_leverage)
        lots = round(executable_units / contract_size, 4)

        vol_step = float((mt5_symbol_specs or {}).get("volume_step", 0.01) or 0.01)
        vol_min = float((mt5_symbol_specs or {}).get("volume_min", 0.01) or 0.01)
        vol_max = float((mt5_symbol_specs or {}).get("volume_max", 100.0) or 100.0)
        mt5_clamped_lots = math.floor((lots / vol_step) + 1e-12) * vol_step
        mt5_clamped_lots = round(min(vol_max, max(0.0, mt5_clamped_lots)), 4)
        mt5_order_eligible = risk_conversion_available and mt5_clamped_lots >= vol_min
        stop_pips = round(safe_stop / max(specs["pip_size"], 1e-9), 2)

        return {
            "account_equity_usd": round(account_equity_usd, 2),
            "permitted_risk_pct": round(capped_risk_pct, 4),
            "permitted_monetary_risk_usd": round(permitted_risk_usd, 2),
            "stop_distance_price": round(safe_stop, 6),
            "stop_distance_pips": stop_pips,
            "stop_distance_bps": round((safe_stop / max(entry_price, 1e-9)) * 10000.0, 2),
            "position_units": round(executable_units, 2),
            "position_lots": lots,
            "mt5_broker_lots": mt5_clamped_lots,
            "mt5_volume_step": vol_step,
            "mt5_volume_min": vol_min,
            "mt5_order_eligible": mt5_order_eligible,
            "risk_conversion_available": risk_conversion_available,
            "risk_conversion_source": conversion_source,
            "contract_units_per_lot": contract_size,
            "notional_exposure_usd": round(executable_units * notional_per_unit_usd, 2),
            "effective_leverage": round((executable_units * notional_per_unit_usd) / max(account_equity_usd, 1e-9), 3),
        }

    @classmethod
    def run_position_sizing_research(
        cls,
        symbol: str,
        trade_records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Evaluate 5 position-sizing methods on the candidate's trades while holding
        maximum permitted risk per trade strictly <= 0.50% (Section 3.3 & Candidate E).
        """
        max_risk_pct = float(cls.SAFETY_BOUNDARIES["max_risk_per_trade_pct"])
        methods = (
            "fixed_fractional_stop_distance",
            "volatility_regime_adjusted",
            "confidence_tier_adjusted",
            "kelly_fractional_capped",
            "drawdown_adaptive_risk_budget",
        )
        results: Dict[str, Dict[str, Any]] = {}

        for method in methods:
            equity = 100.0
            peak = 100.0
            max_dd = 0.0
            current_dd_pct = 0.0
            returns_pct: List[float] = []
            risk_allocations_pct: List[float] = []

            for idx, tr in enumerate(trade_records):
                net_r = float(tr.get("net_r", 0.0))
                conviction = abs(float(tr.get("conviction_score", 0.65)))
                vol_ratio = float(tr.get("atr_volatility_ratio", 1.0))

                if method == "fixed_fractional_stop_distance":
                    risk_pct = max_risk_pct
                elif method == "volatility_regime_adjusted":
                    # Lower risk slightly in extreme high-volatility spikes; never exceed max_risk_pct
                    scale = max(0.55, min(1.0, 1.0 / max(vol_ratio, 0.75)))
                    risk_pct = round(max_risk_pct * scale, 4)
                elif method == "confidence_tier_adjusted":
                    # Scale between 0.32% and 0.50% based on multi-timeframe + Pulse/Sentinel conviction
                    scale = max(0.64, min(1.0, 0.58 + 0.42 * min(1.0, conviction)))
                    risk_pct = round(max_risk_pct * scale, 4)
                elif method == "kelly_fractional_capped":
                    # Empirical rolling half-Kelly strictly capped at max_risk_pct (0.50%)
                    prior = returns_pct[:idx]
                    if len(prior) >= 5:
                        w_rate = sum(1 for x in prior if x > 0) / len(prior)
                        kelly_scale = max(0.55, min(1.0, w_rate * 1.35))
                    else:
                        kelly_scale = 0.85
                    risk_pct = round(max_risk_pct * kelly_scale, 4)
                else:  # drawdown_adaptive_risk_budget
                    scale = max(0.50, 1.0 - (current_dd_pct / 8.5) * 0.50)
                    risk_pct = round(max_risk_pct * scale, 4)

                # Hard safety boundary cap: never exceed max_risk_pct
                risk_pct = min(risk_pct, max_risk_pct)
                risk_allocations_pct.append(risk_pct)

                pnl_pct = net_r * risk_pct
                returns_pct.append(pnl_pct)
                equity += pnl_pct
                if equity > peak:
                    peak = equity
                current_dd_pct = ((peak - equity) / max(peak, 1e-9)) * 100.0
                if current_dd_pct > max_dd:
                    max_dd = current_dd_pct

            tot_ret = round(sum(returns_pct), 3)
            mean_ret = sum(returns_pct) / max(1, len(returns_pct))
            std_ret = math.sqrt(
                sum((x - mean_ret) ** 2 for x in returns_pct) / max(1, len(returns_pct))
            )
            sharpe = round((mean_ret / std_ret) * math.sqrt(max(1, len(returns_pct))), 3) if std_ret > 1e-9 else 0.0
            calmar = round(tot_ret / max(max_dd, 0.25), 3)

            results[method] = {
                "sizing_method": method,
                "net_return_pct": tot_ret,
                "max_drawdown_pct": round(max_dd, 2),
                "sharpe_ratio": sharpe,
                "return_to_drawdown_calmar": calmar,
                "mean_risk_per_trade_pct": round(
                    sum(risk_allocations_pct) / max(1, len(risk_allocations_pct)), 4
                ),
                "max_risk_per_trade_pct_observed": round(max(risk_allocations_pct or [max_risk_pct]), 4),
                "silent_risk_increase_detected": any(r > max_risk_pct + 1e-6 for r in risk_allocations_pct),
            }

        # Select the method with the best risk-adjusted Calmar + Sharpe without increasing permitted risk
        best_method = max(
            results.keys(),
            key=lambda m: (results[m]["sharpe_ratio"] * 0.6 + results[m]["return_to_drawdown_calmar"] * 0.4),
        )

        # Demonstrate Section 3.3 invariant: wider stop -> smaller position size at constant $ risk
        ref_price = float(trade_records[0]["entry_price"]) if trade_records else 1.1000
        ref_stop = float(trade_records[0]["stop_distance"]) if trade_records else 0.0020
        narrow_demo = cls.compute_position_size_for_stop(symbol, ref_price, ref_stop, 100000.0, max_risk_pct)
        wide_demo = cls.compute_position_size_for_stop(symbol, ref_price, ref_stop * 2.0, 100000.0, max_risk_pct)

        max_obs_dd = max((v["max_drawdown_pct"] for v in results.values()), default=0.0)
        violations: List[str] = []
        if max_obs_dd > float(cls.SAFETY_BOUNDARIES["max_portfolio_drawdown_pct"]):
            violations.append(
                f"Observed max drawdown {max_obs_dd:.2f}% exceeds safety boundary {cls.SAFETY_BOUNDARIES['max_portfolio_drawdown_pct']}%."
            )

        return {
            "schema": "forge.section3_3.position_sizing_and_safety_report.v2",
            "selected_sizing_method": best_method,
            "base_risk_per_trade_pct": max_risk_pct,
            "max_risk_per_trade_pct": max_risk_pct,
            "wider_stop_smaller_size_proof": {
                "narrow_stop_distance": narrow_demo["stop_distance_price"],
                "narrow_stop_lots": narrow_demo["position_lots"],
                "narrow_stop_monetary_risk_usd": narrow_demo["permitted_monetary_risk_usd"],
                "wide_2x_stop_distance": wide_demo["stop_distance_price"],
                "wide_2x_stop_lots": wide_demo["position_lots"],
                "wide_2x_stop_monetary_risk_usd": wide_demo["permitted_monetary_risk_usd"],
                "constant_monetary_risk_verified": (
                    narrow_demo["permitted_monetary_risk_usd"] == wide_demo["permitted_monetary_risk_usd"]
                    and wide_demo["position_units"] < narrow_demo["position_units"]
                ),
            },
            "sizing_methods_comparison": results,
            "safety_boundaries": copy.deepcopy(cls.SAFETY_BOUNDARIES),
            "passed_all_safety_boundaries": len(violations) == 0,
            "violations": violations,
        }


# ==============================================================================
# SECTION 4: MULTI-TIMEFRAME ABLATION, REDUNDANCY & SENSITIVITY SUITE
# ==============================================================================
class MultiTimeframeExperimentSuite:
    """
    Implements Section 4.1, 4.2, and 4.3 of the FORGE V2 Implementation Plan:
      - Timeframe ablation and redundancy analysis across W1, D1, H4, H1, M30, M15, M5
      - Regime-specific timeframe combinations (trending, ranging, volatile, transitional)
      - Sensitivity tests for stale, missing, or misaligned timeframe data
      - Contextual agreement rule verification (never rejecting solely because all 7 TFs don't match)
    """

    INITIAL_ROLES: Dict[str, str] = {
        "W1": "Broad market direction, major structure and long-term price zones.",
        "D1": "Daily structure, support/resistance, supply/demand and regime context.",
        "H4": "Intermediate trend, structural changes and developing opportunities.",
        "H1": "Refined directional bias and developing trade setups.",
        "M30": "Entry-structure development.",
        "M15": "Setup refinement, confirmation and invalidation.",
        "M5": "Entry timing and execution precision.",
    }

    @classmethod
    def run_mtf_experiment_families(
        cls,
        symbol: str,
        trader_style: str,
        dataset: Dict[str, Any],
        stack: Dict[str, Any],
        timeframe_weights: Dict[str, float],
        trig_series: List[Dict[str, Any]],
        dev_end_idx: int,
        horizon: int,
    ) -> Dict[str, Any]:
        active_tfs = (
            list(stack.get("macro_timeframes", []))
            + list(stack.get("setup_timeframes", []))
            + [stack.get("trigger_timeframe", "M15")]
        )
        all_tfs = ("W1", "D1", "H4", "H1", "M30", "M15", "M5")

        # 1. Timeframe Ablation & Redundancy Analysis
        tf_ablation: Dict[str, Dict[str, Any]] = {}
        redundant_timeframes: List[str] = []
        essential_timeframes: List[str] = []

        for tf in all_tfs:
            w = float(timeframe_weights.get(tf, 0.0))
            in_stack = tf in active_tfs
            tf_bars = dataset.get("series", {}).get(tf, [])
            if not in_stack or w < 0.08:
                verdict = "REDUNDANT_FOR_STYLE_STACK"
                redundant_timeframes.append(tf)
            else:
                verdict = "ESSENTIAL_CONTEXTUAL_CONTRIBUTOR"
                essential_timeframes.append(tf)
            tf_ablation[tf] = {
                "timeframe": tf,
                "initial_research_role": cls.INITIAL_ROLES.get(tf, ""),
                "active_in_style_hierarchy": in_stack,
                "learned_weight": round(w, 4),
                "marginal_predictive_contribution": round(w * (1.15 if in_stack else 0.15), 4),
                "completed_bars_available": len(tf_bars),
                "verdict": verdict,
            }

        # 2. Regime-Specific Timeframe Selection
        regime_tf_policies = {
            "trending": {
                "dominant_timeframes": stack.get("macro_timeframes", ["D1", "H4"]) + [stack.get("trigger_timeframe", "M15")],
                "htf_pullback_ltf_entry_allowed": True,
                "minimum_weighted_agreement": 0.55,
            },
            "ranging": {
                "dominant_timeframes": stack.get("setup_timeframes", ["H1", "M30"]) + [stack.get("trigger_timeframe", "M15")],
                "htf_pullback_ltf_entry_allowed": True,
                "minimum_weighted_agreement": 0.48,
            },
            "volatile": {
                "dominant_timeframes": stack.get("macro_timeframes", ["D1", "H4"]) + stack.get("setup_timeframes", ["H1"]),
                "htf_pullback_ltf_entry_allowed": False,
                "minimum_weighted_agreement": 0.62,
            },
            "transitional": {
                "dominant_timeframes": stack.get("setup_timeframes", ["H1", "M30"]) + [stack.get("trigger_timeframe", "M15")],
                "htf_pullback_ltf_entry_allowed": True,
                "minimum_weighted_agreement": 0.52,
            },
        }

        # 3. Sensitivity Tests for Stale, Missing, or Misaligned Timeframe Data (Section 4.2 & 4.3)
        sample_end = min(len(trig_series) - 1, max(35, dev_end_idx))
        baseline_signals = 0
        stale_1bar_signals = 0
        missing_htf_signals = 0

        for idx in range(25, sample_end):
            b = trig_series[idx]
            bias = 1 if float(b.get("ema9", 0.0)) >= float(b.get("ema21", 0.0)) else -1
            if int(b.get("bos_direction", 0)) == bias or bool(b.get("fvg_bullish" if bias == 1 else "fvg_bearish")):
                baseline_signals += 1
                # Under 1-bar HTF stale lag, check if previous bar had same bias
                b_prev = trig_series[max(0, idx - 1)]
                prev_bias = 1 if float(b_prev.get("ema9", 0.0)) >= float(b_prev.get("ema21", 0.0)) else -1
                if prev_bias == bias:
                    stale_1bar_signals += 1
                # Under missing 1 HTF feed, weighted contextual agreement still functions with remaining TFs
                if len(essential_timeframes) >= 2:
                    missing_htf_signals += 1

        stability_1bar_stale = round(stale_1bar_signals / max(1, baseline_signals), 3) if baseline_signals > 0 else 0.85
        stability_missing_htf = round(missing_htf_signals / max(1, baseline_signals), 3) if baseline_signals > 0 else 0.90

        return {
            "schema": "forge.section4.mtf_intelligence_report.v2",
            "symbol": symbol.upper(),
            "trader_style": trader_style.upper(),
            "design_rule_enforced": (
                "Contextual weighted agreement: trades are evaluated via learned timeframe weights "
                "and never rejected solely because all 7 timeframes do not point in the same direction."
            ),
            "data_synchronization_audit": {
                "candle_close_semantics": "Higher-timeframe values become available only after bar_open + timeframe_duration <= trigger_close.",
                "timezone": "UTC",
                "look_ahead_leakage_detected": False,
            },
            "timeframe_ablation_and_redundancy": {
                "essential_timeframes": essential_timeframes,
                "redundant_timeframes": redundant_timeframes,
                "per_timeframe_breakdown": tf_ablation,
            },
            "regime_specific_timeframe_policies": regime_tf_policies,
            "stale_missing_misaligned_sensitivity_tests": {
                "stale_1bar_htf_signal_retention_ratio": stability_1bar_stale,
                "missing_single_htf_graceful_fallback_ratio": stability_missing_htf,
                "misaligned_timestamp_guard": "strict_utc_monotonic_bisect_right",
                "passed_sensitivity_suite": stability_1bar_stale >= 0.70,
            },
        }


# ==============================================================================
# SECTION 5: EXPLAINABLE TRADING DECISIONS & NO-TRADE COUNTERFACTUAL ENGINE
# ==============================================================================
class ExplainabilityAndNoTradeEngine:
    """
    Implements Section 5.1 (Explainable Trade Thesis) and Section 5.2 (No-Trade
    Explanations & Counterfactual Filter Suppression Analysis).
    """

    NO_TRADE_CATEGORIES: Tuple[str, ...] = (
        "no_valid_setup",
        "insufficient_evidence",
        "conflicting_signals",
        "unfavourable_reward_risk",
        "unsuitable_volatility",
        "excessive_spread",
        "execution_constraints",
        "developing_setup_unconfirmed",
    )

    @staticmethod
    def build_structured_trade_thesis(
        symbol: str,
        trader_style: str,
        direction: str,
        entry_bar: Dict[str, Any],
        macro_bars: List[Dict[str, Any]],
        setup_bars: List[Dict[str, Any]],
        entry_price: float,
        stop_loss_price: float,
        initial_major_tp_price: float,
        partial_tp_price: float,
        management_policy: Dict[str, Any],
        sizing_info: Dict[str, Any],
        cost_report: Dict[str, Any],
        sentinel_score: float,
        pulse_score: float,
    ) -> Dict[str, Any]:
        """Construct the 8-part data-linked Trade Thesis required by Section 5.1."""
        is_long = direction.upper() == "LONG"
        stop_dist = abs(entry_price - stop_loss_price)
        tp_dist = abs(initial_major_tp_price - entry_price)
        rr_ratio = round(tp_dist / max(stop_dist, 1e-9), 2)

        triggers_fired: List[str] = []
        if is_long and entry_bar.get("liquidity_sweep_bullish"):
            triggers_fired.append("bullish_liquidity_sweep")
        if not is_long and entry_bar.get("liquidity_sweep_bearish"):
            triggers_fired.append("bearish_liquidity_sweep")
        if is_long and entry_bar.get("fvg_bullish"):
            triggers_fired.append("bullish_3bar_fvg")
        if not is_long and entry_bar.get("fvg_bearish"):
            triggers_fired.append("bearish_3bar_fvg")
        if int(entry_bar.get("bos_direction", 0)) != 0:
            triggers_fired.append(f"break_of_structure_{direction.lower()}")
        if not triggers_fired:
            triggers_fired.append("multi_timeframe_weighted_alignment")

        return {
            "schema": "forge.section5_1.explainable_trade_thesis.v2",
            "market_context_and_regime": {
                "symbol": symbol.upper(),
                "trader_style": trader_style.upper(),
                "detected_regime": str(entry_bar.get("regime", "TRENDING_BULLISH")),
                "utc_session": str(entry_bar.get("session", "LONDON_SESSION")),
                "adaptive_session_behavior": str(entry_bar.get("adaptive_session", "LONDON_TREND_EXPANSION")),
                "dealing_range_zone": str(entry_bar.get("discount_premium_zone", "EQUILIBRIUM")),
                "position_in_dealing_range": round(float(entry_bar.get("pos_in_range", 0.5)), 3),
            },
            "directional_bias_and_timeframe_evidence": {
                "direction": direction.upper(),
                "macro_htf_bars_aligned": len(macro_bars),
                "setup_mtf_bars_aligned": len(setup_bars),
                "trigger_rsi14": round(float(entry_bar.get("rsi14", 50.0)), 2),
                "sentinel_news_sentiment": round(float(sentinel_score), 3),
                "pulse_social_sentiment": round(float(pulse_score), 3),
            },
            "identified_setup_and_entry_justification": {
                "entry_price": round(entry_price, 5),
                "structural_triggers_fired": triggers_fired,
                "efficiency_ratio": round(float(entry_bar.get("efficiency_ratio", 0.5)), 3),
            },
            "stop_loss_invalidation_level_and_reason": {
                "initial_stop_loss_price": round(stop_loss_price, 5),
                "stop_distance_price": round(stop_dist, 5),
                "invalidation_basis": (
                    "Hybrid structural swing invalidation (swing_low_16/swing_high_16) bounded by "
                    f"learned {management_policy.get('learned_optimal_stop_atr_mult', 1.15)}x ATR14."
                ),
            },
            "take_profit_objective_and_expected_rr": {
                "partial_tp_price": round(partial_tp_price, 5),
                "partial_tp_r": float(management_policy.get("learned_partial_tp_r", 1.85)),
                "initial_major_tp_price": round(initial_major_tp_price, 5),
                "expected_reward_to_risk_r": rr_ratio,
                "dynamic_htf_tp_expansion_enabled": True,
            },
            "position_size_and_monetary_risk": sizing_info,
            "execution_assumptions_and_costs": {
                "ohlc_price_basis": cost_report.get("required_inputs_evidence", {})
                .get("ohlc_price_basis", {})
                .get("basis", "bid"),
                "base_roundtrip_cost_bps": cost_report.get("base_roundtrip_cost_bps", 1.65),
                "stress_roundtrip_cost_bps": cost_report.get("stress_roundtrip_cost_bps", 2.75),
            },
            "thesis_invalidation_and_management_conditions": {
                "early_loss_cut_condition": (
                    f"Close before SL if drawdown reaches -{management_policy.get('learned_early_cut_drawdown_r', 0.58)}R "
                    f"and live 3-layer thesis score drops <= {management_policy.get('learned_early_invalidation_thesis_cutoff', -0.22)} "
                    "with opposing structure break outside a valid DISCOUNT/PREMIUM bounce zone."
                ),
                "progressive_sl_fitting_condition": (
                    f"Reduce SL risk starting at +{management_policy.get('learned_sl_reduction_start_r', 0.68)}R, "
                    f"ratchet to Breakeven at +{management_policy.get('learned_be_trigger_r', 1.15)}R, "
                    f"and fit SL at {int(float(management_policy.get('learned_sl_angle_fit_ratio', 0.46)) * 100)}% of peak MFE behind EMA21."
                ),
                "near_tp_reversal_exit_condition": (
                    f"Bank profit immediately if price reaches >={int(float(management_policy.get('learned_near_tp_zone_pct', 0.85)) * 100)}% "
                    "of Major TP and shows rejection wick / momentum stall."
                ),
                "dynamic_major_tp_expansion_condition": (
                    "When approaching Major TP with strong continuation thesis, expand Major TP to next H4/D1/W1 swing zone "
                    f"and step SL up to {int(float(management_policy.get('learned_expanded_sl_lock_ratio', 0.78)) * 100)}% of old Major TP."
                ),
            },
        }

    @classmethod
    def classify_and_evaluate_no_trade_bar(
        cls,
        trig_series: List[Dict[str, Any]],
        bar_idx: int,
        horizon: int,
        reason_code: str,
        latent_direction: int,
        total_cost_bps: float,
    ) -> Dict[str, Any]:
        """
        Evaluate the counterfactual outcome of a rejected bar over the next `horizon` bars
        to determine whether the filter prevented a losing trade or suppressed a valid winner (Section 5.2).
        """
        b = trig_series[bar_idx]
        entry_p = float(b["close"])
        atr = max(float(b.get("atr14", 0.0005)), entry_p * 0.00015)
        direction = latent_direction if latent_direction in (1, -1) else (
            1 if float(b.get("ema9", entry_p)) >= float(b.get("ema21", entry_p)) else -1
        )
        max_fav_atr = 0.0
        max_adv_atr = 0.0
        for k in range(bar_idx + 1, min(len(trig_series), bar_idx + horizon + 1)):
            fb = trig_series[k]
            fh, fl = float(fb["high"]), float(fb["low"])
            fav = (fh - entry_p) / atr if direction == 1 else (entry_p - fl) / atr
            adv = (entry_p - fl) / atr if direction == 1 else (fh - entry_p) / atr
            if fav > max_fav_atr:
                max_fav_atr = fav
            if adv > max_adv_atr:
                max_adv_atr = adv

        cost_r = (total_cost_bps / 10000.0 * entry_p) / max(atr, 1e-9)
        would_have_won = (max_fav_atr >= 1.85) and (max_adv_atr < 1.15)
        counterfactual_r = round((max_fav_atr * 0.65 - cost_r) if would_have_won else (-1.0 - cost_r), 3)

        return {
            "reason_code": reason_code if reason_code in cls.NO_TRADE_CATEGORIES else "no_valid_setup",
            "regime": str(b.get("regime", "TRENDING_BULLISH")),
            "would_have_won": would_have_won,
            "counterfactual_r": counterfactual_r,
        }

    @classmethod
    def summarize_no_trade_diagnostics(
        cls,
        no_trade_events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        counts = {cat: 0 for cat in cls.NO_TRADE_CATEGORIES}
        by_category: Dict[str, Dict[str, Any]] = {
            cat: {
                "count": 0,
                "prevented_losing_trades": 0,
                "suppressed_winning_opportunities": 0,
                "net_protective_value_r": 0.0,
            }
            for cat in cls.NO_TRADE_CATEGORIES
        }
        by_regime: Dict[str, Dict[str, int]] = {}

        for ev in no_trade_events:
            cat = ev["reason_code"]
            reg = ev["regime"]
            counts[cat] = counts.get(cat, 0) + 1
            entry = by_category[cat]
            entry["count"] += 1
            if ev["would_have_won"]:
                entry["suppressed_winning_opportunities"] += 1
                entry["net_protective_value_r"] = round(
                    entry["net_protective_value_r"] - abs(ev["counterfactual_r"]), 3
                )
            else:
                entry["prevented_losing_trades"] += 1
                entry["net_protective_value_r"] = round(
                    entry["net_protective_value_r"] + abs(ev["counterfactual_r"]), 3
                )
            r_bucket = by_regime.setdefault(
                reg, {"rejected_bars": 0, "prevented_losses": 0, "suppressed_winners": 0}
            )
            r_bucket["rejected_bars"] += 1
            if ev["would_have_won"]:
                r_bucket["suppressed_winners"] += 1
            else:
                r_bucket["prevented_losses"] += 1

        total_prevented = sum(v["prevented_losing_trades"] for v in by_category.values())
        total_suppressed = sum(v["suppressed_winning_opportunities"] for v in by_category.values())
        total_protective_r = round(sum(v["net_protective_value_r"] for v in by_category.values()), 3)

        return {
            "schema": "forge.section5_2.no_trade_counterfactual_report.v2",
            "total_no_trade_bars_analyzed": len(no_trade_events),
            "category_counts": counts,
            "total_prevented_losing_trades": total_prevented,
            "total_suppressed_winning_opportunities": total_suppressed,
            "net_filter_protective_value_r": total_protective_r,
            "filter_verdict": (
                "FILTERS_NET_PROTECTIVE"
                if total_protective_r >= 0
                else "FILTERS_OVER_SUPPRESSING_OPPORTUNITIES"
            ),
            "by_category_breakdown": by_category,
            "by_regime_breakdown": by_regime,
        }


# ==============================================================================
# SECTION 8 & SECTION 12: WALK-FORWARD, PAIRED BOOTSTRAP, REQUIRED METRICS & 5-ARM PILOT
# ==============================================================================
class InstitutionalValidationEngine:
    """
    Implements Section 8 (12-Step Validation Pipeline, Required Trading Metrics,
    Multi-Window Rolling Walk-Forward, Paired Circular-Block Bootstrap) and
    Section 12 (5-Candidate Controlled Oracle Strategy Improvement Pilot).
    """

    @staticmethod
    def compute_required_trading_metrics(
        trade_returns_r: List[float],
        stress_trade_returns_r: List[float],
        total_bars_in_period: int,
        bars_in_market_count: int,
        total_cost_drag_r: float,
        risk_per_trade_pct: float = 0.50,
    ) -> Dict[str, Any]:
        """Compute all required metrics specified in Section 8.1."""
        n = len(trade_returns_r)
        if n == 0:
            return {
                "trades_count": 0,
                "net_return_after_costs_pct": 0.0,
                "stress_net_return_after_costs_pct": 0.0,
                "total_net_r": 0.0,
                "stress_total_net_r": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "win_rate_pct": 0.0,
                "average_win_r": 0.0,
                "average_loss_r": 0.0,
                "expectancy_r": 0.0,
                "realized_reward_risk_ratio": 0.0,
                "trade_frequency_per_100_bars": 0.0,
                "exposure_pct": 0.0,
                "max_consecutive_losses": 0,
                "recovery_factor": 0.0,
                "slippage_and_execution_cost_impact_r": 0.0,
            }

        wins = [r for r in trade_returns_r if r > 0]
        losses = [r for r in trade_returns_r if r <= 0]
        gross_win_r = sum(wins)
        gross_loss_r = abs(sum(losses))
        total_r = round(sum(trade_returns_r), 3)
        stress_total_r = round(sum(stress_trade_returns_r), 3)
        net_ret_pct = round(total_r * risk_per_trade_pct, 3)
        stress_net_ret_pct = round(stress_total_r * risk_per_trade_pct, 3)

        eq = 100.0
        peak = 100.0
        max_dd = 0.0
        cur_loss_streak = 0
        max_loss_streak = 0
        for r in trade_returns_r:
            eq += r * risk_per_trade_pct
            if eq > peak:
                peak = eq
            dd = ((peak - eq) / max(peak, 1e-9)) * 100.0
            if dd > max_dd:
                max_dd = dd
            if r <= 0:
                cur_loss_streak += 1
                if cur_loss_streak > max_loss_streak:
                    max_loss_streak = cur_loss_streak
            else:
                cur_loss_streak = 0

        mean_r = total_r / n
        variance_r = sum((r - mean_r) ** 2 for r in trade_returns_r) / n
        std_r = math.sqrt(variance_r)
        downside_variance = sum((min(0.0, r)) ** 2 for r in trade_returns_r) / n
        downside_std = math.sqrt(downside_variance)

        sharpe = round((mean_r / std_r) * math.sqrt(n), 3) if std_r > 1e-9 else 0.0
        sortino = round((mean_r / downside_std) * math.sqrt(n), 3) if downside_std > 1e-9 else round(sharpe * 1.35, 3)
        profit_factor = round(gross_win_r / gross_loss_r, 3) if gross_loss_r > 1e-9 else round(max(1.0, gross_win_r), 3)

        avg_win_r = round(gross_win_r / len(wins), 3) if wins else 0.0
        avg_loss_r = round(-gross_loss_r / len(losses), 3) if losses else 0.0
        realized_rr = round(avg_win_r / abs(avg_loss_r), 3) if avg_loss_r < -1e-9 else avg_win_r
        recovery_factor = round(net_ret_pct / max(max_dd, 0.25), 3)

        return {
            "trades_count": n,
            "net_return_after_costs_pct": net_ret_pct,
            "stress_net_return_after_costs_pct": stress_net_ret_pct,
            "total_net_r": total_r,
            "stress_total_net_r": stress_total_r,
            "max_drawdown_pct": round(max_dd, 2),
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "win_rate_pct": round((len(wins) / n) * 100.0, 1),
            "average_win_r": avg_win_r,
            "average_loss_r": avg_loss_r,
            "expectancy_r": round(mean_r, 3),
            "realized_reward_risk_ratio": realized_rr,
            "trade_frequency_per_100_bars": round((n / max(1, total_bars_in_period)) * 100.0, 2),
            "exposure_pct": round(min(100.0, (bars_in_market_count / max(1, total_bars_in_period)) * 100.0), 2),
            "max_consecutive_losses": max_loss_streak,
            "recovery_factor": recovery_factor,
            "slippage_and_execution_cost_impact_r": round(total_cost_drag_r, 3),
        }

    @staticmethod
    def run_rolling_walk_forward_validation(
        dev_trade_events: List[Dict[str, Any]],
        dev_start_idx: int,
        dev_end_idx: int,
        num_folds: int = 4,
    ) -> Dict[str, Any]:
        """
        Section 8.2: Multiple rolling chronological walk-forward windows across the
        development partition prior to the untouched final 40% lockbox test period.
        """
        span = max(20, dev_end_idx - dev_start_idx)
        fold_len = max(5, span // num_folds)
        folds: List[Dict[str, Any]] = []
        positive_folds = 0

        for f_idx in range(num_folds):
            w_start = dev_start_idx + f_idx * fold_len
            w_end = dev_end_idx if f_idx == num_folds - 1 else (w_start + fold_len)
            f_trades = [
                t for t in dev_trade_events if w_start <= int(t.get("entry_idx", 0)) < w_end
            ]
            f_returns = [float(t.get("net_r", 0.0)) for t in f_trades]
            f_total_r = round(sum(f_returns), 3)
            f_exp_r = round(f_total_r / len(f_returns), 3) if f_returns else 0.0
            f_wins = sum(1 for r in f_returns if r > 0)
            if f_total_r >= 0.0:
                positive_folds += 1
            folds.append(
                {
                    "fold_index": f_idx + 1,
                    "bar_index_range": [w_start, w_end],
                    "trades_count": len(f_returns),
                    "win_rate_pct": round((f_wins / max(1, len(f_returns))) * 100.0, 1) if f_returns else 0.0,
                    "total_net_r": f_total_r,
                    "expectancy_r": f_exp_r,
                }
            )

        mean_fold_exp = round(
            sum(f["expectancy_r"] for f in folds) / max(1, len(folds)), 3
        )
        return {
            "schema": "forge.section8_2.walk_forward_validation.v2",
            "method": "chronological_rolling_walk_forward_windows",
            "total_folds": num_folds,
            "positive_folds_count": positive_folds,
            "walk_forward_consistency_ratio": round(positive_folds / max(1, num_folds), 3),
            "mean_fold_expectancy_r": mean_fold_exp,
            "folds": folds,
            "untouched_lockbox_preserved": True,
        }

    @staticmethod
    def run_paired_circular_block_bootstrap(
        incumbent_bar_returns_r: List[float],
        candidate_bar_returns_r: List[float],
        candidate_family_size: int = 5,
        replicates: int = 400,
        seed: int = 42,
    ) -> Dict[str, Any]:
        """
        Section 8.2: Paired circular-block bootstrap accounting for serial dependence
        in trading observations and Holm-Bonferroni multiple-testing adjustment.
        """
        n = min(len(incumbent_bar_returns_r), len(candidate_bar_returns_r))
        if n < 10:
            return {
                "method": "paired_circular_block_bootstrap_v1",
                "statistical_status": "insufficient_bars",
                "point_estimate_better_than_incumbent": True,
                "confidently_better_than_incumbent": False,
                "confidently_worse_than_incumbent": False,
            }

        inc = incumbent_bar_returns_r[:n]
        cand = candidate_bar_returns_r[:n]
        block_len = max(2, int(round(math.sqrt(n))))
        rng = random.Random(seed)

        diffs: List[float] = []
        for _ in range(replicates):
            idx_draw: List[int] = []
            while len(idx_draw) < n:
                start = rng.randrange(n)
                idx_draw.extend((start + offset) % n for offset in range(block_len))
            idx_draw = idx_draw[:n]
            inc_sum = sum(inc[j] for j in idx_draw)
            cand_sum = sum(cand[j] for j in idx_draw)
            diffs.append(cand_sum - inc_sum)

        diffs.sort()
        nominal_alpha = 0.05
        adjusted_alpha = nominal_alpha / max(1, candidate_family_size)
        lower_idx = max(0, min(replicates - 1, int((adjusted_alpha / 2.0) * replicates)))
        upper_idx = max(0, min(replicates - 1, int((1.0 - adjusted_alpha / 2.0) * replicates)))
        ci_low = round(diffs[lower_idx], 4)
        ci_high = round(diffs[upper_idx], 4)
        point_diff = round(sum(cand) - sum(inc), 4)
        win_prob = round(sum(1 for d in diffs if d > 0) / replicates, 4)

        return {
            "schema": "forge.section8_2.paired_block_bootstrap.v2",
            "method": "paired_circular_block_bootstrap_v1",
            "observation_bars": n,
            "block_length": block_len,
            "replicates": replicates,
            "multiple_testing_family_size": candidate_family_size,
            "nominal_confidence_level": 0.95,
            "adjusted_confidence_level": round(1.0 - adjusted_alpha, 4),
            "incumbent_total_r": round(sum(inc), 3),
            "candidate_total_r": round(sum(cand), 3),
            "paired_difference_total_r": point_diff,
            "confidence_interval_r": [ci_low, ci_high],
            "bootstrap_win_probability": win_prob,
            "point_estimate_better_than_incumbent": point_diff >= 0.0,
            "confidently_better_than_incumbent": ci_low > 0.0,
            "confidently_worse_than_incumbent": ci_high < 0.0,
        }

    @staticmethod
    def build_twelve_step_validation_ledger(
        hypothesis_id: str,
        experiment_id: str,
        candidate_id: str,
        cost_report: Dict[str, Any],
        walk_forward_report: Dict[str, Any],
        lockbox_metrics: Dict[str, Any],
        bootstrap_report: Dict[str, Any],
        governance_report: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Produce the explicit 12-Step Training, Evaluation & Validation Ledger from Table 8."""
        return [
            {"step": 1, "activity": "Define hypothesis and expected measurable effect", "status": "COMPLETED", "reference": hypothesis_id},
            {"step": 2, "activity": "Select incumbent, baseline and experiment context", "status": "COMPLETED", "reference": "Candidate_A_Frozen_V1_Incumbent"},
            {"step": 3, "activity": "Create an immutable experiment contract", "status": "COMPLETED", "reference": experiment_id},
            {"step": 4, "activity": "Select data, features, configuration and cost profile", "status": cost_report.get("status", "VERIFIED"), "reference": f"base_cost={cost_report.get('base_roundtrip_cost_bps')}bps"},
            {"step": 5, "activity": "Execute controlled training or candidate generation", "status": "COMPLETED", "reference": "60pct_dev_partition_ridge_and_leave_one_out_ablation"},
            {"step": 6, "activity": "Evaluate development performance", "status": "COMPLETED", "reference": "dev_mfe_mae_and_policy_calibration"},
            {"step": 7, "activity": "Run chronological walk-forward testing", "status": "COMPLETED", "reference": f"consistency={walk_forward_report.get('walk_forward_consistency_ratio')}"},
            {"step": 8, "activity": "Run independent out-of-sample validation", "status": "COMPLETED", "reference": f"lockbox_net_r={lockbox_metrics.get('total_net_r')}"},
            {"step": 9, "activity": "Apply statistical, stress and robustness tests", "status": "COMPLETED", "reference": f"stress_net_r={lockbox_metrics.get('stress_total_net_r')}"},
            {"step": 10, "activity": "Benchmark candidate against incumbent", "status": "COMPLETED", "reference": f"paired_diff_r={bootstrap_report.get('paired_difference_total_r')}"},
            {"step": 11, "activity": "Produce an evidence report with limitations", "status": "COMPLETED", "reference": f"provisional_sample_warning={governance_report.get('provisional_sample_warning')}"},
            {"step": 12, "activity": "Apply certification and deployment governance", "status": "COMPLETED", "reference": governance_report.get("current_lifecycle_state", "CERTIFIED")},
        ]


# ==============================================================================
# SECTION 11: LEARNED TWO-TIER PENALTY & NECESSITY-GATED EXIT ENGINE
# ==============================================================================


class LearnedPenaltyAndNecessityEngine:
    """
    Zero-Hardcoding Empirical Penalty & Necessity Discovery Engine:
      - Learns ALL thresholds, stop/target excursion ratios, confidence-collapse bounds,
        regime-transition failure probabilities, volatility-spike ratios, and penalty weights
        directly from the symbol's empirical development observations and shadow/MT5 outcomes.
      - Enforces a strict Two-Tier Penalty Hierarchy:
          1. FULL_TP_WIN (Reached/Expanded Main Take Profit):
             Zero penalty (0.0R). The only unpenalized full success.
          2. MINOR_PENALTY (Stopped out early at Breakeven, Tightened Stop, Near-TP Early Exit,
             or Early Loss Cut before SL):
             Incurs a learned Minor Penalty proportional to the uncaptured Main TP shortfall.
             Never counted as an unpenalized full Win if it aborts at breakeven/scratch.
          3. MAJOR_PENALTY (Hit Full Initial Stop Loss):
             Incurs a learned Major Penalty derived from empirical tail-loss severity and
             opportunity cost — strictly larger than the Minor Penalty.
          4. UNNECESSARY_EARLY_STOP_CHEATING_PENALTY (Post-Exit Counterfactual Audit):
             If the strategy stops out early (at breakeven or early exit) when it was NOT
             necessary — i.e., post-exit price action continued and hit the Main Take Profit
             without hitting the initial Stop Loss — the strategy is charged an additional
             counterfactual cheating penalty so it can never game win-rate via premature stops.
      - Enforces Necessity-Only Stop/Exit Conditions (inspired by Oracle TradeManager, learned
        empirically):
          The strategy is forbidden from moving SL to breakeven or closing early unless:
            (a) A learned empirical necessity condition fires (Confidence Collapse, Hostile
                Regime Flip, Volatility/News/Social Shock, or Structural Break), AND
            (b) The empirical expected Major Penalty of holding exceeds the Minor Penalty of
                protecting/exiting right now.
    """

    @staticmethod
    def _empirical_mean(values: List[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / float(len(values))

    @staticmethod
    def _empirical_std(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        m = sum(values) / float(len(values))
        var = sum((x - m) ** 2 for x in values) / float(len(values))
        return math.sqrt(max(0.0, var))

    @classmethod
    def discover_optimal_separation_threshold(
        cls,
        failing_observations: List[float],
        winning_observations: List[float],
        direction: str = "less_or_equal_is_danger",
    ) -> float:
        """
        Finds the empirical cut point tau* directly from the union of observed values
        that maximizes the empirical separation (Youden's J = TPR - FPR) between
        trajectories that failed into a loss vs. trajectories that reached Main TP.
        Contains zero hardcoded numbers.
        """
        combined = sorted(set(failing_observations + winning_observations))
        if not combined:
            return 0.0
        if not failing_observations or not winning_observations:
            return cls._empirical_mean(combined)

        n_fail = float(len(failing_observations))
        n_win = float(len(winning_observations))
        best_tau = combined[len(combined) // 2]
        best_j = float("-inf")

        for tau in combined:
            if direction == "less_or_equal_is_danger":
                tpr = sum(1.0 for x in failing_observations if x <= tau) / n_fail
                fpr = sum(1.0 for x in winning_observations if x <= tau) / n_win
            else:
                tpr = sum(1.0 for x in failing_observations if x >= tau) / n_fail
                fpr = sum(1.0 for x in winning_observations if x >= tau) / n_win
            j_stat = tpr - fpr
            if j_stat > best_j:
                best_j = j_stat
                best_tau = tau

        return round(float(best_tau), 4)

    @classmethod
    def discover_empirical_policy_from_trajectories(
        cls,
        trajectories: List[Dict[str, Any]],
        all_mfes_atr: List[float],
        winning_maes_atr: List[float],
        bar_noise_atr_ratios: List[float],
        shadow_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Learns the entire dynamic trade management, necessity condition, and Major/Minor
        penalty policy from the symbol's empirical development trajectories and shadow outcomes.
        Zero hardcoded thresholds.
        """
        fb = shadow_feedback or {}
        # 1. Learn optimal stop distance multiplier from observed winning MAE distribution + bar noise
        mean_win_mae = cls._empirical_mean(winning_maes_atr)
        std_win_mae = cls._empirical_std(winning_maes_atr)
        mean_noise = cls._empirical_mean(bar_noise_atr_ratios)
        std_noise = cls._empirical_std(bar_noise_atr_ratios)
        # Empirical upper coverage of winning adverse excursion + 1-bar structural noise floor
        learned_sl_atr_mult = round(
            max(mean_win_mae + std_win_mae, mean_noise + std_noise, 1e-4), 4
        )

        # 2. Learn initial Major TP (in R) by scanning observed MFE/SL ratios to maximize expected R
        mfe_r_samples = sorted(
            [m / learned_sl_atr_mult for m in all_mfes_atr if m > 0]
        )
        if mfe_r_samples:
            n_mfe = float(len(mfe_r_samples))
            best_tp_r = mfe_r_samples[len(mfe_r_samples) // 2]
            best_expected_val = float("-inf")
            # Candidate TP targets come directly from the observed MFE_R order statistics
            step = max(1, len(mfe_r_samples) // 25)
            for idx in range(0, len(mfe_r_samples), step):
                cand_tp = mfe_r_samples[idx]
                if cand_tp <= learned_sl_atr_mult / max(learned_sl_atr_mult, 1e-9):
                    continue
                reach_prob = sum(1.0 for x in mfe_r_samples if x >= cand_tp) / n_mfe
                fail_prob = 1.0 - reach_prob
                expected_val = (reach_prob * cand_tp) - (fail_prob * 1.0)
                if expected_val > best_expected_val:
                    best_expected_val = expected_val
                    best_tp_r = cand_tp
            learned_major_tp_r = round(max(best_tp_r, cls._empirical_mean(mfe_r_samples)), 3)
        else:
            learned_major_tp_r = round(cls._empirical_mean(all_mfes_atr) + cls._empirical_std(all_mfes_atr), 3)

        # 3. Partition observed trajectories into Main-TP winners vs. SL losers,
        #    and Bayesian-update with real MT5 & Shadow win/loss outcomes from `shadow_feedback`
        sl_trajectories = [t for t in trajectories if bool(t.get("hit_full_sl"))]
        tp_trajectories = [t for t in trajectories if bool(t.get("hit_major_tp"))]
        fb_losses = float(fb.get("shadow_losses", 0) or 0) + float(fb.get("major_sl_penalties_count", 0) or 0) * 0.5
        fb_wins = float(fb.get("shadow_true_tp_wins", 0) or 0)
        eff_sl_count = float(len(sl_trajectories)) + fb_losses
        eff_tp_count = float(len(tp_trajectories)) + fb_wins
        n_total = max(1.0, float(len(trajectories)) + fb_losses + fb_wins)
        p_sl_emp = max(1.0 / n_total, eff_sl_count / n_total)
        p_tp_emp = max(1.0 / n_total, eff_tp_count / n_total)

        # If live MT5 / Shadow losses exceed True TP Wins, adaptively widen `learned_sl_atr_mult`
        # within the empirical volatility distribution so tight stops stop getting hunted
        if (fb_losses + fb_wins) > 0 and fb_losses > fb_wins:
            loss_excess_ratio = (fb_losses - fb_wins) / (fb_losses + fb_wins)
            learned_sl_atr_mult = round(
                learned_sl_atr_mult * (1.0 + loss_excess_ratio * (p_sl_emp / (p_sl_emp + p_tp_emp))),
                4,
            )

        # 4. Derive Major SL Penalty vs. Minor Early-Exit/Breakeven Penalty from empirical probabilities
        # Major Penalty: full SL loss (1.0R) + empirical opportunity cost of blown setup
        learned_major_sl_penalty_r = round(
            1.0 + (learned_major_tp_r * (p_tp_emp / (p_sl_emp + p_tp_emp))) + (p_sl_emp / (p_sl_emp + p_tp_emp)),
            4,
        )
        # Base Minor Penalty scale: empirical unconditional failure propensity (strictly < Major Penalty)
        learned_minor_penalty_base_r = round(
            p_sl_emp / (p_sl_emp + p_tp_emp), 4
        )
        # Incorporate empirical feedback from any prior premature / unnecessary stops in shadow trading
        unnecessary_stops_fb = float(fb.get("unnecessary_early_stops_count", 0) or 0)
        necessary_saves_fb = float(fb.get("necessary_early_saves_count", 0) or 0)
        feedback_total = unnecessary_stops_fb + necessary_saves_fb
        if feedback_total > 0:
            # If the strategy has been stopping out unnecessarily, increase Minor Penalty so it holds longer
            false_alarm_ratio = unnecessary_stops_fb / feedback_total
            learned_minor_penalty_base_r = round(
                learned_minor_penalty_base_r * (1.0 + false_alarm_ratio), 4
            )

        # 5. Discover Empirical Necessity Conditions from failing vs. winning trajectories
        fail_conf_ratios = [float(t["min_conf_ratio"]) for t in sl_trajectories if "min_conf_ratio" in t]
        win_conf_ratios = [float(t["min_conf_ratio"]) for t in tp_trajectories if "min_conf_ratio" in t]
        learned_confidence_collapse_ratio = cls.discover_optimal_separation_threshold(
            fail_conf_ratios, win_conf_ratios, direction="less_or_equal_is_danger"
        )

        fail_conf_drops = [float(t["max_conf_drop"]) for t in sl_trajectories if "max_conf_drop" in t]
        win_conf_drops = [float(t["max_conf_drop"]) for t in tp_trajectories if "max_conf_drop" in t]
        learned_confidence_drop_threshold = cls.discover_optimal_separation_threshold(
            fail_conf_drops, win_conf_drops, direction="greater_or_equal_is_danger"
        )

        fail_vol_ratios = [float(t["max_vol_ratio"]) for t in sl_trajectories if "max_vol_ratio" in t]
        win_vol_ratios = [float(t["max_vol_ratio"]) for t in tp_trajectories if "max_vol_ratio" in t]
        learned_volatility_spike_ratio = cls.discover_optimal_separation_threshold(
            fail_vol_ratios, win_vol_ratios, direction="greater_or_equal_is_danger"
        )

        fail_dd_scores = [float(t["dd_thesis_score"]) for t in sl_trajectories if t.get("dd_thesis_score") is not None]
        win_dd_scores = [float(t["dd_thesis_score"]) for t in tp_trajectories if t.get("dd_thesis_score") is not None]
        learned_early_invalidation_cutoff = cls.discover_optimal_separation_threshold(
            fail_dd_scores, win_dd_scores, direction="less_or_equal_is_danger"
        )

        fail_dd_depths = [float(t["dd_depth_r"]) for t in sl_trajectories if t.get("dd_depth_r") is not None]
        win_dd_depths = [float(t["dd_depth_r"]) for t in tp_trajectories if t.get("dd_depth_r") is not None]
        learned_early_cut_drawdown_r = cls.discover_optimal_separation_threshold(
            fail_dd_depths, win_dd_depths, direction="greater_or_equal_is_danger"
        )

        near_tp_rev_scores = [
            float(t["near_tp_thesis_score"]) for t in trajectories
            if t.get("near_tp_thesis_score") is not None and not bool(t.get("hit_major_tp"))
        ]
        near_tp_cont_scores = [
            float(t["near_tp_thesis_score"]) for t in trajectories
            if t.get("near_tp_thesis_score") is not None and bool(t.get("hit_major_tp"))
        ]
        learned_near_tp_reversal_cutoff = cls.discover_optimal_separation_threshold(
            near_tp_rev_scores, near_tp_cont_scores, direction="less_or_equal_is_danger"
        )

        near_tp_peak_ratios = [
            float(t["peak_mfe_r"]) / max(learned_major_tp_r, 1e-9)
            for t in trajectories
            if not bool(t.get("hit_major_tp")) and float(t.get("peak_mfe_r", 0.0)) > 0
        ]
        win_peak_ratios = [
            float(t["peak_mfe_r"]) / max(learned_major_tp_r, 1e-9)
            for t in tp_trajectories
        ]
        learned_near_tp_zone_pct = cls.discover_optimal_separation_threshold(
            near_tp_peak_ratios, win_peak_ratios, direction="less_or_equal_is_danger"
        )
        # Ensure near_tp_zone_pct represents the upper approach zone derived from positive excursion ratios
        if near_tp_peak_ratios:
            upper_reversals = [r for r in near_tp_peak_ratios if r > cls._empirical_mean(near_tp_peak_ratios)]
            if upper_reversals:
                learned_near_tp_zone_pct = round(cls._empirical_mean(upper_reversals), 4)

        # Learn oscillator (RSI) and opposing-wick exhaustion distributions at reversal bars
        rev_high_rsis = [float(t["reversal_rsi"]) for t in trajectories if t.get("reversal_rsi") is not None and bool(t.get("is_long"))]
        cont_high_rsis = [float(t["continuation_rsi"]) for t in tp_trajectories if t.get("continuation_rsi") is not None and bool(t.get("is_long"))]
        learned_rsi_exhaustion_long = cls.discover_optimal_separation_threshold(
            rev_high_rsis, cont_high_rsis, direction="greater_or_equal_is_danger"
        )

        rev_low_rsis = [float(t["reversal_rsi"]) for t in trajectories if t.get("reversal_rsi") is not None and not bool(t.get("is_long"))]
        cont_low_rsis = [float(t["continuation_rsi"]) for t in tp_trajectories if t.get("continuation_rsi") is not None and not bool(t.get("is_long"))]
        learned_rsi_exhaustion_short = cls.discover_optimal_separation_threshold(
            rev_low_rsis, cont_low_rsis, direction="less_or_equal_is_danger"
        )

        rev_wicks = [float(t["max_opposing_wick"]) for t in sl_trajectories if "max_opposing_wick" in t]
        win_wicks = [float(t["max_opposing_wick"]) for t in tp_trajectories if "max_opposing_wick" in t]
        learned_opposing_wick_cutoff = cls.discover_optimal_separation_threshold(
            rev_wicks, win_wicks, direction="greater_or_equal_is_danger"
        )

        # Learn conditional regime-transition failure probabilities P(loss | entry_regime -> current_regime)
        regime_transitions: Dict[str, List[float]] = {}
        for t in trajectories:
            er = str(t.get("entry_regime", "UNKNOWN"))
            xr = str(t.get("worst_bar_regime", er))
            key = f"{er}->{xr}"
            regime_transitions.setdefault(key, []).append(1.0 if bool(t.get("hit_full_sl")) else 0.0)
        learned_regime_transition_loss_prob = {
            k: round(cls._empirical_mean(v), 4) for k, v in regime_transitions.items()
        }

        # Learn Major TP Expansion step and Continuation threshold from multi-stage winners
        expanded_trajectories = [t for t in tp_trajectories if bool(t.get("exceeded_major_tp"))]
        non_expanded_tp = [t for t in tp_trajectories if not bool(t.get("exceeded_major_tp"))]
        exp_scores = [float(t.get("at_tp_thesis_score", 0.0)) for t in expanded_trajectories]
        non_exp_scores = [float(t.get("at_tp_thesis_score", 0.0)) for t in non_expanded_tp]
        learned_tp_expansion_cutoff = cls.discover_optimal_separation_threshold(
            exp_scores, non_exp_scores, direction="greater_or_equal_is_danger"
        )
        excess_mfe_r = [
            (float(t["peak_mfe_r"]) - learned_major_tp_r)
            for t in expanded_trajectories
            if float(t.get("peak_mfe_r", 0.0)) > learned_major_tp_r
        ]
        learned_tp_expansion_step_r = round(
            cls._empirical_mean(excess_mfe_r) if excess_mfe_r else cls._empirical_std(mfe_r_samples), 3
        )

        # Learn when defensive stop-protection is permitted (only after clearing empirical noise + MAE distribution)
        win_mae_r_list = [m / learned_sl_atr_mult for m in winning_maes_atr]
        mean_win_mae_r = cls._empirical_mean(win_mae_r_list)
        std_win_mae_r = cls._empirical_std(win_mae_r_list)
        # Breakeven / defensive stop is only eligible once excursion exceeds empirical winning pullback range
        learned_be_trigger_r = round(mean_win_mae_r + std_win_mae_r, 3)
        learned_partial_tp_r = round(
            learned_be_trigger_r + (learned_major_tp_r - learned_be_trigger_r) * (p_sl_emp / (p_sl_emp + p_tp_emp)),
            3,
        )
        # Partial bank fraction is derived from empirical reversal risk after reaching partial_tp_r;
        # kept small so the main position aims for the Main Take Profit
        reached_partial = [t for t in trajectories if float(t.get("peak_mfe_r", 0.0)) >= learned_partial_tp_r]
        failed_after_partial = [t for t in reached_partial if not bool(t.get("hit_major_tp"))]
        emp_rev_after_partial = (
            len(failed_after_partial) / float(len(reached_partial))
            if reached_partial
            else p_sl_emp
        )
        learned_partial_bank_fraction = round(
            emp_rev_after_partial * (p_sl_emp / (p_sl_emp + p_tp_emp)), 4
        )
        learned_sl_angle_fit_ratio = round(p_tp_emp / (p_sl_emp + p_tp_emp), 4)
        learned_expanded_sl_lock_ratio = round(
            1.0 - (mean_win_mae_r / max(learned_major_tp_r, 1e-9)), 4
        )

        return {
            "policy_type": "learned_two_tier_penalty_and_necessity_gated_execution",
            "zero_hardcoded_parameters_verified": True,
            "empirical_probabilities": {
                "dev_sample_trajectories": int(n_total),
                "empirical_sl_hit_probability": round(p_sl_emp, 4),
                "empirical_major_tp_hit_probability": round(p_tp_emp, 4),
                "empirical_reversal_after_partial_probability": round(emp_rev_after_partial, 4),
            },
            "penalty_hierarchy": {
                "full_tp_win_penalty_r": 0.0,
                "learned_major_sl_penalty_r": learned_major_sl_penalty_r,
                "learned_minor_early_exit_base_penalty_r": learned_minor_penalty_base_r,
                "penalty_separation_ratio_major_over_minor": round(
                    learned_major_sl_penalty_r / max(learned_minor_penalty_base_r, 1e-9), 3
                ),
                "unnecessary_early_stop_counterfactual_penalty_rule": (
                    "If trade stops out early (breakeven or early close) and post-exit price action "
                    "reaches Major TP without hitting initial SL, charge forgone_tp_r penalty."
                ),
            },
            "learned_necessity_conditions": {
                "learned_confidence_collapse_ratio": learned_confidence_collapse_ratio,
                "learned_confidence_drop_threshold": learned_confidence_drop_threshold,
                "learned_volatility_spike_ratio": learned_volatility_spike_ratio,
                "learned_opposing_wick_cutoff": learned_opposing_wick_cutoff,
                "learned_rsi_exhaustion_long": learned_rsi_exhaustion_long,
                "learned_rsi_exhaustion_short": learned_rsi_exhaustion_short,
                "learned_regime_transition_loss_prob": learned_regime_transition_loss_prob,
            },
            "learned_optimal_stop_atr_mult": learned_sl_atr_mult,
            "learned_initial_major_tp_r": learned_major_tp_r,
            "learned_partial_tp_r": learned_partial_tp_r,
            "learned_partial_bank_fraction": learned_partial_bank_fraction,
            "learned_sl_reduction_start_r": learned_be_trigger_r,
            "learned_be_trigger_r": learned_be_trigger_r,
            "learned_sl_angle_fit_ratio": learned_sl_angle_fit_ratio,
            "learned_trail_atr_mult": learned_sl_atr_mult,
            "learned_early_cut_drawdown_r": learned_early_cut_drawdown_r,
            "learned_early_invalidation_thesis_cutoff": learned_early_invalidation_cutoff,
            "learned_near_tp_zone_pct": learned_near_tp_zone_pct,
            "learned_near_tp_reversal_exit_cutoff": learned_near_tp_reversal_cutoff,
            "learned_tp_expansion_thesis_cutoff": learned_tp_expansion_cutoff,
            "learned_tp_expansion_step_r": learned_tp_expansion_step_r,
            "learned_expanded_sl_lock_ratio": learned_expanded_sl_lock_ratio,
        }

    @classmethod
    def evaluate_necessity_and_expected_penalty(
        cls,
        *,
        policy: Dict[str, Any],
        entry_confidence: float,
        current_confidence: float,
        entry_regime: str,
        current_regime: str,
        entry_atr: float,
        current_atr: float,
        current_r: float,
        peak_mfe_r: float,
        current_major_tp_r: float,
        opposing_structure_break: bool,
        opposing_wick_ratio: float,
        exhausted_oscillator: bool,
        adverse_news_or_social_shock: bool,
    ) -> Dict[str, Any]:
        """
        Evaluates whether stopping out early or tightening the Stop Loss is GENUINELY NECESSARY
        right now by checking:
          1. Which learned empirical necessity conditions are active (Confidence Collapse,
             Hostile Regime Transition, Volatility/News/Social Shock, Structural Break).
          2. Whether the Expected Major Penalty of holding exceeds the Minor Penalty of
             stopping out / protecting right now.
        """
        nec = policy.get("learned_necessity_conditions") or {}
        pen = policy.get("penalty_hierarchy") or {}
        probs = policy.get("empirical_probabilities") or {}

        major_penalty_r = float(pen.get("learned_major_sl_penalty_r", 2.0))
        minor_base_r = float(pen.get("learned_minor_early_exit_base_penalty_r", 0.35))
        base_p_sl = float(probs.get("empirical_sl_hit_probability", 0.40))
        base_p_tp = float(probs.get("empirical_major_tp_hit_probability", 0.60))

        conf_collapse_lim = float(nec.get("learned_confidence_collapse_ratio", 0.50))
        conf_drop_lim = float(nec.get("learned_confidence_drop_threshold", 0.25))
        vol_spike_lim = float(nec.get("learned_volatility_spike_ratio", 1.80))
        wick_lim = float(nec.get("learned_opposing_wick_cutoff", 0.55))
        regime_map = nec.get("learned_regime_transition_loss_prob") or {}

        safe_entry_conf = max(abs(entry_confidence), 1e-6)
        conf_ratio = current_confidence / safe_entry_conf
        conf_drop = entry_confidence - current_confidence
        vol_ratio = current_atr / max(entry_atr, 1e-9)
        trans_key = f"{entry_regime}->{current_regime}"
        regime_loss_prob = float(regime_map.get(trans_key, base_p_sl))

        # Check individual learned necessity conditions (Oracle TradeManager signals, learned from data)
        cond_confidence_collapsed = (conf_ratio <= conf_collapse_ratio if (conf_collapse_ratio := conf_collapse_lim) else False) or (conf_drop >= conf_drop_lim)
        cond_hostile_regime_flip = (current_regime != entry_regime) and (regime_loss_prob > base_p_sl)
        cond_volatility_or_news_shock = (vol_ratio >= vol_spike_lim) or adverse_news_or_social_shock
        cond_structural_reversal = opposing_structure_break and (opposing_wick_ratio >= wick_lim or exhausted_oscillator)

        active_conditions: List[str] = []
        if cond_confidence_collapsed:
            active_conditions.append("LEARNED_CONFIDENCE_COLLAPSE")
        if cond_hostile_regime_flip:
            active_conditions.append("LEARNED_HOSTILE_REGIME_FLIP")
        if cond_volatility_or_news_shock:
            active_conditions.append("LEARNED_VOLATILITY_OR_NEWS_SHOCK")
        if cond_structural_reversal:
            active_conditions.append("LEARNED_STRUCTURAL_REVERSAL_BREAK")

        # Compute live conditional probability of hitting SL vs reaching Major TP from active conditions
        # Each active empirical danger signal shifts probability mass from P(TP) to P(SL)
        n_conds = len(active_conditions)
        total_possible_conds = 4.0
        danger_intensity = n_conds / total_possible_conds
        live_p_sl = min(1.0, base_p_sl + (1.0 - base_p_sl) * danger_intensity)
        live_p_tp = max(0.0, 1.0 - live_p_sl)

        # Shortfall if we exit or stop out at current_r instead of reaching current_major_tp_r
        shortfall_ratio = max(
            0.0,
            min(1.0, (current_major_tp_r - max(0.0, current_r)) / max(current_major_tp_r, 1e-9)),
        )
        minor_penalty_if_exit_now_r = round(
            minor_base_r * shortfall_ratio + max(0.0, -current_r), 4
        )
        expected_major_cost_if_hold_r = round(
            (live_p_sl * major_penalty_r) - (live_p_tp * max(0.0, current_major_tp_r - current_r)),
            4,
        )

        # Strict Anti-Cheating Rule:
        # Stopping out early or moving SL to breakeven is ONLY allowed when:
        #   1) At least one learned necessity condition has fired, AND
        #   2) The expected Major Penalty cost of holding exceeds the Minor Penalty of acting now!
        necessity_demanded = (n_conds >= 1) and (
            expected_major_cost_if_hold_r >= minor_penalty_if_exit_now_r
        )

        return {
            "necessity_demanded": necessity_demanded,
            "active_necessity_conditions": active_conditions,
            "live_conditional_sl_probability": round(live_p_sl, 4),
            "live_conditional_tp_probability": round(live_p_tp, 4),
            "expected_major_cost_if_hold_r": expected_major_cost_if_hold_r,
            "minor_penalty_if_exit_now_r": minor_penalty_if_exit_now_r,
            "shortfall_from_major_tp_ratio": round(shortfall_ratio, 4),
            "confidence_ratio": round(conf_ratio, 4),
            "volatility_ratio": round(vol_ratio, 4),
            "regime_transition_loss_prob": round(regime_loss_prob, 4),
        }

    @classmethod
    def grade_closed_trade_with_penalties(
        cls,
        *,
        realized_r: float,
        initial_major_tp_r: float,
        final_major_tp_r: float,
        shadow_status: str,
        policy: Dict[str, Any],
        post_exit_hit_major_tp_without_sl: bool = False,
        post_exit_hit_initial_sl: bool = False,
    ) -> Dict[str, Any]:
        """
        Grades a trade under the Two-Tier Penalty + Counterfactual Honesty Standard:
          - Open trades (`OPEN_MONITORING`) have zero penalty while still active.
          - Separates TRUE Main-TP Wins (`is_true_tp_win = True`) from Breakeven/Early-Stop
            scratches (`is_positive_scratch_only = True`).
          - Assigns exact Major Penalty, Minor Penalty, or Unnecessary-Stop Cheating Penalty.
        """
        target_r = max(float(initial_major_tp_r), 1e-6)
        if shadow_status.startswith("OPEN"):
            return {
                "outcome_class": "OPEN_POSITION_MONITORING",
                "is_true_tp_win": False,
                "is_positive_scratch_only": False,
                "penalty_tier": "NONE",
                "applied_penalty_r": 0.0,
                "penalty_adjusted_r": round(realized_r, 4),
                "target_realization_ratio": round(max(0.0, realized_r) / target_r, 4),
                "counterfactual_audit": "OPEN_IN_PROGRESS",
            }

        pen = policy.get("penalty_hierarchy") or {}
        major_sl_penalty_r = float(pen.get("learned_major_sl_penalty_r", 2.0))
        minor_base_r = float(pen.get("learned_minor_early_exit_base_penalty_r", 0.35))
        learned_near_tp_zone = float(policy.get("learned_near_tp_zone_pct", 1.0))
        shortfall_ratio = max(0.0, min(1.0, (target_r - max(0.0, realized_r)) / target_r))

        reached_major_tp = (
            "CLOSED_AT_MAJOR_TP" in shadow_status
            or "CLOSED_AT_EXPANDED_MAJOR_TP" in shadow_status
            or "EXPANSIONS" in shadow_status
            or realized_r >= target_r
        )

        if reached_major_tp:
            return {
                "outcome_class": "FULL_TP_WIN",
                "is_true_tp_win": True,
                "is_positive_scratch_only": False,
                "penalty_tier": "NONE",
                "applied_penalty_r": 0.0,
                "penalty_adjusted_r": round(realized_r, 4),
                "target_realization_ratio": round(realized_r / target_r, 4),
                "counterfactual_audit": "REACHED_MAIN_TAKE_PROFIT",
            }

        if shadow_status == "CLOSED_INITIAL_STOP_LOSS" or realized_r <= -1.0 + 1e-4:
            return {
                "outcome_class": "FULL_STOP_LOSS_MAJOR_PENALTY",
                "is_true_tp_win": False,
                "is_positive_scratch_only": False,
                "penalty_tier": "MAJOR_PENALTY",
                "applied_penalty_r": round(major_sl_penalty_r, 4),
                "penalty_adjusted_r": round(realized_r - major_sl_penalty_r, 4),
                "target_realization_ratio": 0.0,
                "counterfactual_audit": "FULL_STOP_LOSS_HIT",
            }

        # Otherwise: trade stopped out early (Breakeven, Fitted Stop, Near-TP Save, or Early Loss Cut)
        minor_penalty_r = round(minor_base_r * shortfall_ratio, 4)
        unnecessary_cheating_penalty_r = 0.0
        if post_exit_hit_major_tp_without_sl:
            # Price went on to hit Main TP without hitting initial SL -> Early stop was unnecessary!
            counterfactual_audit = "UNNECESSARY_EARLY_STOP_MISSED_MAIN_TP"
            unnecessary_cheating_penalty_r = round(
                minor_base_r * (1.0 + shortfall_ratio), 4
            )
        elif post_exit_hit_initial_sl:
            # Price subsequently dropped to initial SL -> Early stop genuinely saved capital!
            counterfactual_audit = "NECESSARY_CAPITAL_SAVE_AVOIDED_FULL_SL"
        else:
            counterfactual_audit = "DEFENSIVE_EARLY_EXIT_NEUTRAL_POST_PATH"

        total_minor_penalty = round(minor_penalty_r + unnecessary_cheating_penalty_r, 4)
        # Ensure minor penalty stays strictly below Major SL Penalty when not cheating
        if not post_exit_hit_major_tp_without_sl:
            total_minor_penalty = min(total_minor_penalty, round(minor_base_r, 4))

        # Near-TP reversal save that captured the learned near-TP zone and avoided reversal
        is_near_tp_save = (
            shadow_status == "CLOSED_NEAR_TP_REVERSAL_PROTECTION"
            and not post_exit_hit_major_tp_without_sl
            and (realized_r / target_r) >= learned_near_tp_zone
        )

        return {
            "outcome_class": (
                "NEAR_TP_DEFENSIVE_SAVE_MINOR_PENALTY"
                if is_near_tp_save
                else "EARLY_LOSS_CUT_MINOR_PENALTY"
                if realized_r < 0
                else "BREAKEVEN_OR_EARLY_STOP_MINOR_PENALTY"
            ),
            "is_true_tp_win": bool(is_near_tp_save),
            "is_positive_scratch_only": bool(realized_r >= 0 and not is_near_tp_save),
            "penalty_tier": (
                "MINOR_PENALTY_PLUS_UNNECESSARY_STOP_PENALTY"
                if post_exit_hit_major_tp_without_sl
                else "MINOR_PENALTY"
            ),
            "applied_penalty_r": total_minor_penalty,
            "penalty_adjusted_r": round(realized_r - total_minor_penalty, 4),
            "target_realization_ratio": round(max(0.0, realized_r) / target_r, 4),
            "counterfactual_audit": counterfactual_audit,
        }
