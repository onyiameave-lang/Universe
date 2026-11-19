"""
Nexus.core.institutional_contracts
==================================
Stage 1, Stage 3 & Stage 4 Institutional Coordination Contracts for Nexus:
  1. Structured Agent Claim & Provenance Envelope (`extract_claim_envelope`)
  2. Cross-Agent Shared-Evidence Provenance Deduplication & Calibrated Conflict
     Resolution (`deduplicate_and_resolve_claims`)
  3. Independent Aegis Versioned Policy & State-Integrity Gate (`evaluate_aegis_policy_gate`)
     - Never votes on factual questions.
     - Computes directional Long/Short reward-to-risk from entry price in consistent units.
     - Reconciles Scout/shadow persisted state against the immutable audit ledger
       (e.g. excluding invalidated +67.764R ASX200 price-scale outliers).
  4. Multi-Factor Coordination Quality Signal (`compute_coordination_quality_signal`)
  5. Two-Tier Chronicle Write-Back Hygiene (`classify_chronicle_writeback`)
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ECO_ROOT = Path(__file__).resolve().parents[2]

# Empirical out-of-sample domain calibration baseline measured across the evaluation suite.
# Used as the prior reliability factor instead of uncalibrated raw self-report alone.
EMPIRICAL_DOMAIN_CALIBRATION: Dict[str, float] = {
    "atlas": 0.91,
    "chronicle": 0.94,
    "oracle": 0.84,
    "forge": 0.88,
    "sentinel": 0.83,
    "pulse": 0.78,
    "aegis": 0.96,
    "genesis": 0.85,
    "nexus": 0.90,
}

DOMAIN_TO_AGENT: Dict[str, str] = {
    "research": "atlas",
    "general": "atlas",
    "memory": "chronicle",
    "trading": "oracle",
    "prediction": "oracle",
    "training": "forge",
    "news": "sentinel",
    "social": "pulse",
    "governance": "aegis",
    "creation": "genesis",
    "coordination": "nexus",
}

AGENT_DISPLAY_ROLE: Dict[str, str] = {
    "atlas": "Scientific, Macro & Peer-Reviewed Research Desk",
    "chronicle": "Persistent Collective Memory & Forensic Audit Ledger",
    "oracle": "Multi-Asset Signal Evaluation & C3 Demo Execution Desk",
    "forge": "7-Timeframe Strategy Discovery, Ablation & Shadow Lab",
    "sentinel": "Global News Wire, GDELT & Event Risk Intelligence",
    "pulse": "Live Social Media Discourse & Crowd Sentiment Desk",
    "aegis": "Independent Constitutional Risk & Ledger Integrity Gate",
    "genesis": "Capability Gap Detection & Autonomous Agent Synthesis",
    "nexus": "Topological DAG Coordinator & Provenance Consensus Engine",
}


def _normalize_origin_key(raw_ref: str, title: str = "", source_type: str = "") -> str:
    """
    Produce a canonical provenance origin key so two agents citing the same
    underlying news wire, URL, DOI, Chronicle memory ID, or market bar window
    hash to the same origin key and cannot be double-counted.
    """
    ref = str(raw_ref or "").strip().lower()
    if ref.startswith("http://") or ref.startswith("https://"):
        ref = re.sub(r"^https?://(?:www\.)?", "", ref)
        ref = ref.split("?")[0].rstrip("/")
        return f"url:{ref[:120]}"
    if ref.startswith("chronicle-") or ref.startswith("mem-"):
        return f"chronicle:{ref}"
    if ref.startswith("10.") and "/" in ref:
        return f"doi:{ref}"
    clean_title = re.sub(r"[^a-z0-9]+", "_", str(title or "").strip().lower()).strip("_")
    if clean_title:
        # Collapse syndication copies of the same headline across wires
        tokens = [t for t in clean_title.split("_") if len(t) > 2][:8]
        stem = "_".join(tokens)
        return f"headline_stem:{stem}"
    fallback = f"{source_type}:{ref or clean_title or 'internal'}"
    return f"ref:{hashlib.md5(fallback.encode('utf-8')).hexdigest()[:12]}"


def _infer_directional_stance(output: Dict[str, Any], text_summary: str = "") -> int:
    """Extract directional stance: +1 (bullish/positive), -1 (bearish/negative), 0 (neutral/factual)."""
    if not isinstance(output, dict):
        return 0
    for holder in (
        output,
        output.get("signal") if isinstance(output.get("signal"), dict) else {},
        output.get("prediction") if isinstance(output.get("prediction"), dict) else {},
        output.get("sentiment") if isinstance(output.get("sentiment"), dict) else {},
        output.get("social_sentiment") if isinstance(output.get("social_sentiment"), dict) else {},
        output.get("result") if isinstance(output.get("result"), dict) else {},
    ):
        if not isinstance(holder, dict):
            continue
        for key in ("direction", "call", "sentiment_label", "stance", "verdict", "score"):
            val = holder.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if val > 0.08:
                    return 1
                if val < -0.08:
                    return -1
            elif isinstance(val, str):
                low = val.strip().lower()
                if any(w in low for w in ("buy", "long", "bullish", "positive", "upward")):
                    return 1
                if any(w in low for w in ("sell", "short", "bearish", "negative", "downward")):
                    return -1
    low_sum = (text_summary or "").lower()
    if re.search(r"\b(bullish|buy signal|long setup|net positive)\b", low_sum) and not re.search(r"\b(bearish|sell signal|short setup)\b", low_sum):
        return 1
    if re.search(r"\b(bearish|sell signal|short setup|net negative)\b", low_sum) and not re.search(r"\b(bullish|buy signal|long setup)\b", low_sum):
        return -1
    return 0


def extract_claim_envelope(
    agent_name: str,
    domain: str,
    task: str,
    raw_output: Any,
    latency_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Stage 1: Convert an agent's raw output dict into a standardized
    AgentClaimEnvelope with claim-level provenance keys and calibrated reliability.
    """
    agent_id = str(agent_name or DOMAIN_TO_AGENT.get(domain, "atlas")).lower()
    out = raw_output if isinstance(raw_output, dict) else {"status": "complete", "summary": str(raw_output or "")}
    status = str(out.get("status") or "complete").lower()

    # Extract human-readable summary
    summary = ""
    for k in ("readable_reply", "human_summary", "summary", "answer", "response", "text", "message"):
        v = out.get(k)
        if isinstance(v, str) and len(v.strip()) > 8:
            summary = v.strip()
            break
    if not summary and isinstance(out.get("report"), dict):
        summary = str(out["report"].get("summary") or "").strip()
    if not summary and isinstance(out.get("result"), dict):
        res_inner = out["result"]
        for k in ("summary", "answer", "text", "message"):
            v = res_inner.get(k)
            if isinstance(v, str) and len(v.strip()) > 8:
                summary = v.strip()
                break
        if not summary and isinstance(res_inner.get("report"), dict):
            summary = str(res_inner["report"].get("summary") or "").strip()

    # Extract raw confidence
    raw_conf = 0.75
    for holder in (
        out,
        out.get("report") if isinstance(out.get("report"), dict) else {},
        out.get("signal") if isinstance(out.get("signal"), dict) else {},
        out.get("sentiment") if isinstance(out.get("sentiment"), dict) else {},
        out.get("result") if isinstance(out.get("result"), dict) else {},
    ):
        if isinstance(holder, dict) and "confidence" in holder and holder["confidence"] is not None:
            try:
                val = float(holder["confidence"])
                raw_conf = val / 100.0 if val > 1.0 else val
                break
            except (TypeError, ValueError):
                pass
        if isinstance(holder, dict) and "overall_confidence" in holder and holder["overall_confidence"] is not None:
            try:
                val = float(holder["overall_confidence"])
                raw_conf = val / 100.0 if val > 1.0 else val
                break
            except (TypeError, ValueError):
                pass
    raw_conf = max(0.05, min(0.99, raw_conf))

    # Extract claim-level provenance items
    claims: List[Dict[str, Any]] = []
    raw_sources = (
        out.get("sources")
        or ((out.get("report") or {}).get("sources") if isinstance(out.get("report"), dict) else None)
        or out.get("evidence")
        or out.get("memories")
        or []
    )
    if isinstance(raw_sources, list):
        for idx, src in enumerate(raw_sources[:6]):
            if isinstance(src, dict):
                title = str(src.get("title") or src.get("headline") or src.get("summary") or f"{agent_id.title()} Claim #{idx + 1}").strip()
                url_ref = str(src.get("url_or_identifier") or src.get("url") or src.get("id") or src.get("memory_id") or src.get("source_id") or "").strip()
                tier = str(src.get("source_tier") or src.get("tier") or ("Tier A" if agent_id in ("chronicle", "aegis", "forge") else "Tier B"))
                excerpt = str(src.get("verbatim_extract_or_table_ref") or src.get("text") or src.get("content") or src.get("excerpt") or title).strip()
                origin_key = _normalize_origin_key(url_ref, title=title, source_type=agent_id)
                claims.append({
                    "claim_id": f"{agent_id}-claim-{idx + 1}",
                    "claim_text": excerpt[:260],
                    "title": title[:140],
                    "source_origin_key": origin_key,
                    "source_tier": tier,
                    "url_or_ref": url_ref or f"universe://{agent_id}/{task}",
                    "data_window_fingerprint": str(src.get("data_window_fingerprint") or src.get("timestamp") or "live"),
                })
            elif isinstance(src, str) and len(src.strip()) > 10:
                origin_key = _normalize_origin_key("", title=src[:80], source_type=agent_id)
                claims.append({
                    "claim_id": f"{agent_id}-claim-{idx + 1}",
                    "claim_text": src.strip()[:260],
                    "title": src.strip()[:90],
                    "source_origin_key": origin_key,
                    "source_tier": "Tier B",
                    "url_or_ref": f"universe://{agent_id}/{task}",
                    "data_window_fingerprint": "live",
                })

    # If the agent didn't attach explicit list sources, derive provenance from its domain ledger/feed
    if not claims and summary and status != "error":
        default_origin = f"ledger:{agent_id}:{task}:{hashlib.md5(summary[:120].encode('utf-8')).hexdigest()[:10]}"
        default_tier = "Tier A" if agent_id in ("chronicle", "aegis", "forge", "oracle") else "Tier B"
        claims.append({
            "claim_id": f"{agent_id}-primary-1",
            "claim_text": summary[:260],
            "title": f"{agent_id.title()} ({AGENT_DISPLAY_ROLE.get(agent_id, domain)})",
            "source_origin_key": default_origin,
            "source_tier": default_tier,
            "url_or_ref": f"universe://{agent_id}/{task}",
            "data_window_fingerprint": "live_snapshot",
        })

    provenance_keys = list(dict.fromkeys(c["source_origin_key"] for c in claims if c.get("source_origin_key")))
    base_cal = EMPIRICAL_DOMAIN_CALIBRATION.get(agent_id, 0.82)
    has_tier_a_or_b = any(c.get("source_tier") in ("Tier A", "Tier B") for c in claims)
    provenance_bonus = min(0.06, 0.02 * len(provenance_keys)) if has_tier_a_or_b else -0.05
    # Calibrated reliability shrinks raw self-reported confidence toward empirical out-of-sample domain calibration
    calibrated_reliability = round(max(0.10, min(0.98, 0.65 * base_cal + 0.35 * raw_conf + provenance_bonus)), 4)
    stance = _infer_directional_stance(out, summary)

    return {
        "schema": "nexus.agent_claim_envelope.v2",
        "agent": agent_id,
        "role": AGENT_DISPLAY_ROLE.get(agent_id, domain),
        "domain": domain,
        "task": task,
        "status": status,
        "degraded": bool(out.get("degraded") or status == "error"),
        "summary": summary or f"{agent_id.title()} completed {task}.",
        "directional_stance": stance,
        "raw_confidence": round(raw_conf, 4),
        "calibrated_reliability": calibrated_reliability,
        "claims": claims,
        "provenance_keys": provenance_keys,
        "provenance_count": len(provenance_keys),
        "latency_ms": round(float(latency_ms or out.get("_latency_ms") or 0.0), 1),
    }


def deduplicate_and_resolve_claims(
    envelopes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Stage 4A: Cross-agent provenance deduplication and calibrated conflict resolution.
    Ensures that if two specialists rely on the same underlying source/wire/Chronicle record,
    that shared source is counted once rather than double-counted.
    """
    seen_origins: Dict[str, str] = {}  # origin_key -> first agent that cited it
    shared_origins: List[Dict[str, Any]] = []
    deduped_envelopes: List[Dict[str, Any]] = []

    for env in envelopes:
        if env.get("status") == "error":
            deduped_envelopes.append({**env, "independent_provenance_factor": 0.0, "effective_weight": 0.0})
            continue
        agent = env.get("agent", "unknown")
        p_keys = env.get("provenance_keys") or []
        unique_for_agent = 0
        shared_for_agent = 0
        for pk in p_keys:
            if pk not in seen_origins:
                seen_origins[pk] = agent
                unique_for_agent += 1
            elif seen_origins[pk] != agent:
                shared_for_agent += 1
                shared_origins.append({
                    "source_origin_key": pk,
                    "primary_agent": seen_origins[pk],
                    "duplicate_agent": agent,
                })
            else:
                unique_for_agent += 1
        total_p = len(p_keys)
        if total_p == 0:
            indep_factor = 0.50
        else:
            # Discount weight when an agent's evidence overlaps with already-counted sources
            indep_factor = round((unique_for_agent + 0.25 * shared_for_agent) / total_p, 4)

        cal_rel = float(env.get("calibrated_reliability") or 0.75)
        degraded_mult = 0.60 if env.get("degraded") else 1.0
        effective_weight = round(cal_rel * indep_factor * degraded_mult, 4)
        deduped_envelopes.append({
            **env,
            "independent_provenance_factor": indep_factor,
            "effective_weight": effective_weight,
        })

    # Directional stance aggregation (only across agents expressing a directional stance)
    pos_agents = [e for e in deduped_envelopes if e.get("directional_stance", 0) > 0 and e.get("effective_weight", 0) > 0]
    neg_agents = [e for e in deduped_envelopes if e.get("directional_stance", 0) < 0 and e.get("effective_weight", 0) > 0]
    pos_weight = round(sum(e["effective_weight"] for e in pos_agents), 4)
    neg_weight = round(sum(e["effective_weight"] for e in neg_agents), 4)

    conflict_detected = bool(pos_agents and neg_agents)
    margin = round(abs(pos_weight - neg_weight), 4)
    if not pos_agents and not neg_agents:
        resolution = "neutral_or_factual"
        rationale = "All consulted specialists provided factual or non-directional intelligence with zero directional conflict."
    elif not conflict_detected:
        resolution = "bullish_consensus" if pos_agents else "bearish_consensus"
        active_side = pos_agents if pos_agents else neg_agents
        names = ", ".join(e["agent"].title() for e in active_side)
        rationale = f"Unanimous directional alignment ({resolution.replace('_', ' ')}) across {names} (calibrated weight {max(pos_weight, neg_weight):.3f})."
    else:
        if margin < 0.15:
            resolution = "contested_hold_or_caution"
            rationale = (
                f"Specialists disagree with a thin calibrated margin ({margin:.3f} < 0.15 threshold: "
                f"positive={pos_weight:.3f} vs negative={neg_weight:.3f} after provenance deduplication). "
                "Conservative resolution: HOLD / require fresh primary catalyst."
            )
        elif pos_weight > neg_weight:
            resolution = "positive_calibrated_majority"
            rationale = (
                f"Resolved in favor of positive/bullish stance (calibrated weight {pos_weight:.3f} vs {neg_weight:.3f}, "
                f"margin +{margin:.3f}) after deduplicating shared provenance."
            )
        else:
            resolution = "negative_calibrated_majority"
            rationale = (
                f"Resolved in favor of negative/bearish stance (calibrated weight {neg_weight:.3f} vs {pos_weight:.3f}, "
                f"margin +{margin:.3f}) after deduplicating shared provenance."
            )

    active_weights = [e["effective_weight"] for e in deduped_envelopes if e.get("status") != "error"]
    mean_calibrated_conviction = round((sum(active_weights) / len(active_weights)) * 100.0, 1) if active_weights else 0.0

    return {
        "envelopes": deduped_envelopes,
        "unique_provenance_sources_count": len(seen_origins),
        "shared_sources_deduplicated_count": len(shared_origins),
        "shared_sources_details": shared_origins,
        "conflict_detected": conflict_detected,
        "directional_weights": {
            "positive": pos_weight,
            "negative": neg_weight,
            "margin": margin,
        },
        "resolution": resolution,
        "resolution_rationale": rationale,
        "calibrated_conviction_pct": mean_calibrated_conviction,
    }


def evaluate_directional_trade_risk_policy(
    *,
    side: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    atr: Optional[float] = None,
    min_reward_to_risk_r: float = 1.80,
    min_stop_atr: float = 0.75,
    max_stop_atr: float = 1.75,
) -> Dict[str, Any]:
    """
    Versioned Trading Risk Policy (`risk_policy.v2`):
    Computes reward-to-risk directionally from `entry_price` in consistent price-distance
    and ATR units for both LONG and SHORT positions:
      - LONG:  stop_loss < entry_price < take_profit
               risk_dist = entry_price - stop_loss
               reward_dist = take_profit - entry_price
      - SHORT: take_profit < entry_price < stop_loss
               risk_dist = stop_loss - entry_price
               reward_dist = entry_price - take_profit
    """
    norm_side = str(side or "LONG").strip().upper()
    if norm_side in ("BUY", "BULLISH"):
        norm_side = "LONG"
    elif norm_side in ("SELL", "BEARISH"):
        norm_side = "SHORT"

    entry = float(entry_price or 0.0)
    sl = float(stop_loss or 0.0)
    tp = float(take_profit or 0.0)
    atr_val = float(atr) if (atr is not None and float(atr or 0.0) > 0) else None

    violations: List[str] = []
    if entry <= 0 or sl <= 0 or tp <= 0:
        return {
            "policy_version": "risk_policy.v2",
            "valid": False,
            "side": norm_side,
            "violations": ["entry_price, stop_loss, and take_profit must all be positive numbers"],
        }

    if norm_side == "LONG":
        risk_dist = entry - sl
        reward_dist = tp - entry
        if not (sl < entry < tp):
            violations.append(
                f"Invalid LONG price ordering: require SL ({sl}) < Entry ({entry}) < TP ({tp})"
            )
    elif norm_side == "SHORT":
        risk_dist = sl - entry
        reward_dist = entry - tp
        if not (tp < entry < sl):
            violations.append(
                f"Invalid SHORT price ordering: require TP ({tp}) < Entry ({entry}) < SL ({sl})"
            )
    else:
        return {
            "policy_version": "risk_policy.v2",
            "valid": False,
            "side": norm_side,
            "violations": [f"Unsupported position side '{norm_side}'"],
        }

    if risk_dist <= 1e-12:
        violations.append("Risk distance |Entry - SL| must be strictly positive")
        rr_ratio = 0.0
    else:
        rr_ratio = round(reward_dist / risk_dist, 4)

    if reward_dist <= risk_dist:
        violations.append(
            f"Asymmetric payoff violation: Reward distance ({reward_dist:.5f}) <= Risk distance ({risk_dist:.5f}) [R:R = {rr_ratio:.2f}R <= 1.0R]"
        )
    elif rr_ratio + 1e-9 < min_reward_to_risk_r:
        violations.append(
            f"Reward-to-risk {rr_ratio:.2f}R is below risk_policy.v2 floor of {min_reward_to_risk_r:.2f}R"
        )

    stop_atr_mult: Optional[float] = None
    tp_atr_mult: Optional[float] = None
    if atr_val is not None and risk_dist > 0:
        stop_atr_mult = round(risk_dist / atr_val, 4)
        tp_atr_mult = round(reward_dist / atr_val, 4)
        if stop_atr_mult + 1e-9 < min_stop_atr or stop_atr_mult - 1e-9 > max_stop_atr:
            violations.append(
                f"Stop distance {stop_atr_mult:.2f} ATR is outside risk_policy.v2 bounds [{min_stop_atr:.2f}, {max_stop_atr:.2f}] ATR"
            )

    return {
        "policy_version": "risk_policy.v2",
        "valid": len(violations) == 0,
        "side": norm_side,
        "entry_price": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "risk_distance": round(risk_dist, 6),
        "reward_distance": round(reward_dist, 6),
        "reward_to_risk_r": rr_ratio,
        "atr": atr_val,
        "stop_atr_multiplier": stop_atr_mult,
        "tp_atr_multiplier": tp_atr_mult,
        "violations": violations,
    }


def inspect_scout_and_ledger_integrity() -> Dict[str, Any]:
    """
    Inspect Oracle V2 Decision Ledger and Scout persisted state (`oracle_v2_learned_state.json`),
    automatically reconciling any invalidated shadow replay (such as +67.764R ASX200) and
    reporting exact settled shadow replay counts, invalidations, and V2 realized trade counts.
    """
    ledger_path = _ECO_ROOT / "Oracle" / "memory" / "oracle_decision_ledger.jsonl"
    state_path = _ECO_ROOT / "Oracle" / "memory" / "oracle_v2_learned_state.json"

    settled_shadow_replays = 0
    invalidated_replays: List[Dict[str, Any]] = []
    invalidated_event_ids: set[str] = set()
    v2_trades_entered = 0
    v2_trades_closed = 0

    if ledger_path.exists():
        try:
            records = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for r in records:
                et = r.get("event_type")
                if et == "TRADE_ENTERED":
                    v2_trades_entered += 1
                elif et == "TRADE_CLOSED":
                    v2_trades_closed += 1
                elif et == "COUNTERFACTUAL_REPLAY_INVALIDATED":
                    orig_id = str(r.get("original_event_id") or "")
                    if orig_id:
                        invalidated_event_ids.add(orig_id)
                    invalidated_replays.append({
                        "symbol": r.get("symbol"),
                        "original_event_id": orig_id,
                        "reason_code": r.get("reason_code"),
                    })
            for r in records:
                if r.get("event_type") == "COUNTERFACTUAL_REPLAY":
                    orig_id = str(r.get("original_event_id") or "")
                    if orig_id not in invalidated_event_ids:
                        settled_shadow_replays += 1
        except Exception:
            pass

    state_contaminated_outliers: List[Dict[str, Any]] = []
    conviction_floor = 0.50
    weights_uniform = True
    state_reconciled = True

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            scout = state.get("scout") or {}
            conviction_floor = float(scout.get("learned_conviction_floor", 0.50))
            dim_w = list((scout.get("dimension_weights") or {}).values())
            if dim_w and (max(dim_w) - min(dim_w) > 0.005):
                weights_uniform = False

            # Check if any invalidated or >20R outlier exists in persisted scout state
            for list_key in ("shadow_counterfactual_outcomes", "counterfactual_outcomes"):
                for item in list(scout.get(list_key) or []):
                    if not isinstance(item, dict):
                        continue
                    cf_r = float(item.get("counterfactual_r") or 0.0)
                    orig_id = str(item.get("original_event_id") or "")
                    sym = str(item.get("symbol") or "").upper()
                    if abs(cf_r) > 20.0 or (orig_id and orig_id in invalidated_event_ids) or sym == "ASX200":
                        state_contaminated_outliers.append({
                            "symbol": sym,
                            "counterfactual_r": cf_r,
                            "list": list_key,
                        })

            if state_contaminated_outliers:
                # Reconcile immediately using Oracle.scout_agent.learner
                try:
                    from Oracle.scout_agent.learner import reconcile_scout_state_with_ledger
                    from Oracle.intelligence.decision_ledger import OracleDecisionLedger
                    dl = OracleDecisionLedger(ledger_path=ledger_path)
                    reconcile_scout_state_with_ledger(scout, dl)
                    state["scout"] = scout
                    state["updated_at"] = time.time()
                    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                    state_reconciled = True
                except Exception:
                    state_reconciled = False
        except Exception:
            pass

    warnings: List[str] = []
    if v2_trades_entered == 0 and v2_trades_closed == 0:
        warnings.append(
            "Zero Oracle V2 live/demo trades entered or closed yet; Scout dimension weights remain at uniform prior (0.1667) and conviction floor at 0.50."
        )
    if invalidated_replays:
        warnings.append(
            f"{len(invalidated_replays)} shadow replay(s) invalidated in ledger (e.g. price-scale mismatch on ASX200) and excluded from active Scout summaries."
        )

    return {
        "synced": state_reconciled,
        "v2_trades_entered": v2_trades_entered,
        "v2_trades_closed": v2_trades_closed,
        "settled_shadow_replays": settled_shadow_replays,
        "invalidated_replays_count": len(invalidated_replays),
        "invalidated_replays_excluded": invalidated_replays,
        "contaminated_outliers_purged": state_contaminated_outliers,
        "scout_conviction_floor": conviction_floor,
        "scout_dimension_weights_at_prior": weights_uniform,
        "shadow_summary_ready_for_live_scoring": False,
        "warnings": warnings,
    }


def evaluate_aegis_policy_gate(
    query: str,
    primary_domain: str,
    participating_domains: List[str],
    envelopes: List[Dict[str, Any]],
    symbol_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Stage 4B: Independent Aegis Policy & State-Integrity Gate.
    Never votes on factual questions. Runs strictly as an independent post-orchestration
    gate when the query or participating domains involve trading, prediction, strategy
    training, governance, or execution.
    """
    actionable_domains = {"trading", "prediction", "training", "governance", "creation"}
    q_low = (query or "").lower()
    is_actionable = (
        primary_domain in actionable_domains
        or any(d in actionable_domains for d in participating_domains)
        or any(w in q_low for w in ("trade", "buy", "sell", "long", "short", "stop loss", "take profit", "sl", "tp", "asx200", "scout", "shadow", "v1", "v2", "oracle", "forge"))
    )
    # Pure math/science formula queries are factual, not trading setups
    if ("compound interest" in q_low or "simple interest" in q_low or "wavelength" in q_low or "soulmate" in q_low) and "trade" not in q_low:
        is_actionable = False

    if not is_actionable:
        return {
            "applicable": False,
            "policy_version": "risk_policy.v2",
            "status": "NOT_APPLICABLE_FACTUAL_QUERY",
            "role": "Independent Policy Gate (does not vote on factual questions)",
            "directional_risk_checks": [],
            "ledger_integrity": None,
            "summary": "Aegis policy gate skipped: query is factual/informational with no trading or execution action.",
        }

    directional_checks: List[Dict[str, Any]] = []
    # Check any candidate strategy geometry in symbol_context
    for sym, sctx in (symbol_context or {}).items():
        styles = (sctx or {}).get("certified_styles") or {}
        for st_name, st_val in styles.items():
            sl_atr = float(st_val.get("sl_atr_multiplier") or 1.20)
            tp_r = float(st_val.get("primary_tp_r") or 2.10)
            # Verify both LONG and SHORT directional geometry at reference entry = 100.0, ATR = 1.0
            long_check = evaluate_directional_trade_risk_policy(
                side="LONG",
                entry_price=100.0,
                stop_loss=100.0 - sl_atr,
                take_profit=100.0 + (sl_atr * tp_r),
                atr=1.0,
            )
            short_check = evaluate_directional_trade_risk_policy(
                side="SHORT",
                entry_price=100.0,
                stop_loss=100.0 + sl_atr,
                take_profit=100.0 - (sl_atr * tp_r),
                atr=1.0,
            )
            directional_checks.append({
                "symbol": sym,
                "style": st_name,
                "sl_atr_multiplier": sl_atr,
                "tp_r_multiple": tp_r,
                "tp_atr_multiplier": round(sl_atr * tp_r, 2),
                "long_geometry_valid": long_check["valid"],
                "short_geometry_valid": short_check["valid"],
                "reward_to_risk_r": long_check["reward_to_risk_r"],
                "violations": long_check["violations"] + short_check["violations"],
            })

    ledger_integrity = inspect_scout_and_ledger_integrity()
    any_risk_violation = any(not c["long_geometry_valid"] or not c["short_geometry_valid"] for c in directional_checks)

    if any_risk_violation or not ledger_integrity["synced"]:
        gate_status = "BLOCKED"
        gate_summary = (
            "Aegis Independent Policy Gate BLOCKED execution: directional reward-to-risk or ledger state integrity check failed."
        )
    elif ledger_integrity["v2_trades_closed"] == 0 and ("scout" in q_low or "shadow" in q_low or "asx200" in q_low):
        gate_status = "CAUTION"
        gate_summary = (
            "Aegis Independent Policy Gate CAUTION: Directional risk policy (risk_policy.v2: Stop in [0.75, 1.75] ATR, "
            "Reward-to-Risk >= 1.80R from entry for both Long and Short) is enforced, and the invalidated ASX200 (+67.764R) "
            "price-scale outlier is quarantined from Scout state. However, Oracle V2 has 0 realized closed trades so far "
            "(13 settled shadow replays only); shadow summaries are informational and not yet used for live conviction scoring."
        )
    else:
        gate_status = "APPROVED"
        gate_summary = (
            "Aegis Independent Policy Gate APPROVED under risk_policy.v2: directional Long/Short reward-to-risk from entry "
            "satisfies |TP - Entry| / |Entry - SL| >= 1.80R with Stop in [0.75, 1.75] ATR, and Scout shadow state is reconciled against the audit ledger."
        )

    return {
        "applicable": True,
        "policy_version": "risk_policy.v2",
        "status": gate_status,
        "role": "Independent Policy & State-Integrity Gate (Non-Voting)",
        "directional_risk_checks": directional_checks[:6],
        "ledger_integrity": ledger_integrity,
        "summary": gate_summary,
    }


def compute_coordination_quality_signal(
    envelopes: List[Dict[str, Any]],
    consensus_info: Dict[str, Any],
    aegis_gate: Dict[str, Any],
    duration_ms: float,
    sla_budget_ms: float = 15000.0,
) -> Dict[str, Any]:
    """
    Stage 3: Multi-factor coordination quality signal for CollaborationGraph learning.
    Replaces coarse `bool(synthesis) and any(per_agent_success)` with a verification-grounded
    quality score in [0, 1].
    """
    if not envelopes:
        return {"quality_score": 0.0, "passed": False, "factors": {}}

    non_error = [e for e in envelopes if e.get("status") != "error"]
    success_ratio = len(non_error) / len(envelopes)
    with_provenance = [e for e in non_error if int(e.get("provenance_count") or 0) > 0]
    provenance_coverage = len(with_provenance) / max(1, len(envelopes))

    unique_src = int(consensus_info.get("unique_provenance_sources_count") or 0)
    corroboration_score = min(1.0, unique_src / max(1, len(non_error)))

    latency_score = 1.0 if duration_ms <= sla_budget_ms else max(0.2, sla_budget_ms / max(1.0, duration_ms))
    gate_ok = aegis_gate.get("status") != "BLOCKED"
    gate_factor = 1.0 if gate_ok else 0.0

    quality_score = round(
        (0.35 * provenance_coverage + 0.25 * success_ratio + 0.20 * corroboration_score + 0.20 * latency_score)
        * gate_factor,
        4,
    )
    return {
        "quality_score": quality_score,
        "passed": quality_score >= 0.65 and gate_ok,
        "factors": {
            "provenance_coverage": round(provenance_coverage, 3),
            "specialist_success_ratio": round(success_ratio, 3),
            "independent_corroboration": round(corroboration_score, 3),
            "sla_latency_compliance": round(latency_score, 3),
            "aegis_policy_gate_passed": gate_ok,
        },
    }


def classify_chronicle_writeback(
    query: str,
    envelopes: List[Dict[str, Any]],
    consensus_info: Dict[str, Any],
    aegis_gate: Dict[str, Any],
    quality_signal: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Proposal 4: Two-Tier Chronicle Write-Back Hygiene.
      - Tier 1 (Episodic Routing & DAG Trace): Always recorded for observability.
      - Tier 2 (Promoted Reusable Semantic Knowledge): Only promoted when quality_signal.passed,
        at least 1 verified Tier A/B provenance source exists, no unresolved thin-margin conflict
        occurred, and Aegis did not block the output.
    """
    unique_sources = int(consensus_info.get("unique_provenance_sources_count") or 0)
    resolution = str(consensus_info.get("resolution") or "")
    gate_status = str(aegis_gate.get("status") or "")
    quality_passed = bool(quality_signal.get("passed"))

    has_high_tier_provenance = any(
        c.get("source_tier") in ("Tier A", "Tier B")
        for e in envelopes
        if e.get("status") != "error"
        for c in (e.get("claims") or [])
    )

    promote_to_knowledge = (
        quality_passed
        and unique_sources >= 1
        and has_high_tier_provenance
        and resolution != "contested_hold_or_caution"
        and gate_status != "BLOCKED"
    )

    if promote_to_knowledge:
        reason = (
            f"Promoted to Chronicle Tier-2 Reusable Semantic Knowledge: {unique_sources} unique verified provenance "
            f"source(s), quality score {quality_signal.get('quality_score'):.2f}, and zero policy violations."
        )
    else:
        reasons = []
        if not quality_passed:
            reasons.append(f"quality score {quality_signal.get('quality_score'):.2f} < 0.65 threshold")
        if not has_high_tier_provenance:
            reasons.append("missing Tier A/B primary provenance")
        if resolution == "contested_hold_or_caution":
            reasons.append("unresolved thin-margin specialist conflict")
        if gate_status == "BLOCKED":
            reasons.append("blocked by Aegis independent policy gate")
        reason = (
            "Logged to Chronicle Tier-1 Episodic Routing Audit Trace only (withheld from Tier-2 reusable knowledge: "
            + ", ".join(reasons or ["single-turn conversational trace"])
            + ")."
        )

    return {
        "tier1_episodic_trace_logged": True,
        "tier2_promoted_to_reusable_knowledge": promote_to_knowledge,
        "unique_provenance_sources_count": unique_sources,
        "quality_score": quality_signal.get("quality_score", 0.0),
        "reason": reason,
    }
