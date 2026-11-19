"""Stage 3: Nexus Institutional Multi-Dimension Coordination Evaluation Suite.

Evaluates Nexus coordination across four separate, audited benchmark dimensions:
  1. Routing Accuracy (single-intent and multi-clause DAG decomposition)
  2. Evidence & Provenance Deduplication Quality (detecting shared evidence sources and preventing double-counting)
  3. Safety & Aegis Policy Gate Enforcement (independent policy veto on inverted Long/Short R:R and Scout/Ledger desync)
  4. Learning Signal & Two-Tier Chronicle Write-Back Hygiene

Persists the evaluation artifact to:
  Chronicle/memory/research/nexus_coordination_benchmark.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from Nexus.intelligence.domain_classifier import DomainClassifier  # type: ignore
from Nexus.intelligence.collaboration_graph import CollaborationGraph  # type: ignore
from Nexus.core.institutional_contracts import (  # type: ignore
    extract_claim_envelope,
    deduplicate_and_resolve_claims,
    evaluate_directional_trade_risk_policy,
    evaluate_aegis_policy_gate,
    compute_coordination_quality_signal,
    classify_chronicle_writeback,
    inspect_scout_and_ledger_integrity,
)


def run_routing_accuracy_suite(classifier: DomainClassifier) -> Dict[str, Any]:
    """Evaluate single-intent and multi-clause intent routing accuracy."""
    cases = [
        {"query": "What is the formula for wavelength and frequency?", "expected_primary": "research", "expected_repo": "atlas"},
        {"query": "Show our open trades, win rate, and GBPUSD execution setups", "expected_primary": "trading", "expected_repo": "oracle"},
        {"query": "What is happening in world news and headlines today?", "expected_primary": "news", "expected_repo": "sentinel"},
        {"query": "Who posted on social media about bitcoin and crowd sentiment?", "expected_primary": "social", "expected_repo": "pulse"},
        {"query": "Check security compliance, risk veto rules, and firewall status", "expected_primary": "governance", "expected_repo": "aegis"},
        {"query": "Retrain the strategy from the last trading session in the mistake clinic", "expected_primary": "training", "expected_repo": "forge"},
        {"query": "Evolve a new trading strategy genome and backtest its fitness", "expected_primary": "creation", "expected_repo": "genesis"},
        {"query": "Recall our stored lessons and past episodes from memory", "expected_primary": "memory", "expected_repo": "chronicle"},
    ]

    passed = 0
    case_reports: List[Dict[str, Any]] = []
    for c in cases:
        res = classifier.classify(c["query"])
        actual_dom = res.get("domain")
        actual_repo = res.get("repository")
        ok = (actual_dom == c["expected_primary"]) and (actual_repo == c["expected_repo"])
        if ok:
            passed += 1
        case_reports.append({
            "query": c["query"],
            "expected_domain": c["expected_primary"],
            "actual_domain": actual_dom,
            "expected_repo": c["expected_repo"],
            "actual_repo": actual_repo,
            "confidence": round(float(res.get("confidence", 0.0)), 4),
            "passed": ok,
        })

    # Multi-clause decomposition check
    multi_cases = [
        {
            "query": "Check Sentinel breaking news on EURUSD, Pulse social media sentiment, and Oracle trading win rate",
            "expected_domains": {"news", "social", "trading"},
        },
        {
            "query": "Research macroeconomic inflation on Atlas and audit our last session retraining on Forge",
            "expected_domains": {"research", "training"},
        },
    ]
    multi_passed = 0
    for mc in multi_cases:
        res = classifier.classify(mc["query"])
        scores_map = res.get("scores") or {}
        ranked = {d for d, score in scores_map.items() if float(score) >= 1.5}
        ok = mc["expected_domains"].issubset(ranked)
        if ok:
            multi_passed += 1
        case_reports.append({
            "query": mc["query"],
            "expected_domains": sorted(list(mc["expected_domains"])),
            "detected_domains": sorted(list(ranked)),
            "passed": ok,
        })

    total = len(cases) + len(multi_cases)
    hit_rate = round(((passed + multi_passed) / max(1, total)) * 100.0, 2)
    return {
        "dimension": "routing_accuracy",
        "cases_total": total,
        "cases_passed": passed + multi_passed,
        "accuracy_pct": hit_rate,
        "details": case_reports,
    }


def run_provenance_deduplication_suite() -> Dict[str, Any]:
    """Evaluate claim-level provenance extraction and shared-source deduplication."""
    shared_url = "https://www.cnbc.com/2026/11/19/ecb-rate-decision-eurusd.html"
    env_sentinel = extract_claim_envelope(
        agent_name="sentinel",
        domain="news",
        task="news.report",
        raw_output={
            "status": "complete",
            "summary": "ECB signals cautious hold on EURUSD as Eurozone inflation moderates.",
            "sources": [{"url": shared_url, "title": "ECB rate decision on EURUSD"}],
            "sentiment": {"sentiment_label": "BEARISH", "confidence": 0.78},
        },
        latency_ms=42.0,
    )
    env_pulse = extract_claim_envelope(
        agent_name="pulse",
        domain="social",
        task="social.sentiment",
        raw_output={
            "status": "complete",
            "summary": "Traders reposting CNBC ECB article are bearish on EURUSD short term.",
            "sources": [{"url": shared_url, "title": "ECB rate decision on EURUSD"}],
            "social_sentiment": {"sentiment_label": "BEARISH", "confidence": 0.74},
        },
        latency_ms=38.0,
    )
    env_oracle = extract_claim_envelope(
        agent_name="oracle",
        domain="trading",
        task="trading.signal",
        raw_output={
            "status": "complete",
            "summary": "EURUSD H1 structure remains bullish above 1.0950 support on C3/MT5 ledger.",
            "sources": [
                {"url": "universe://oracle/mt5_demo_trades.csv", "title": "Oracle C3/MT5 Demo Trade Ledger", "source_tier": "Tier A"},
                {"url": "chronicle-eurusd-h1-structure", "title": "Chronicle EURUSD H1 Structure", "source_tier": "Tier A"},
            ],
            "prediction": {"symbol": "EURUSD", "direction": "LONG", "confidence": 0.81},
        },
        latency_ms=29.0,
    )

    dedup_1 = deduplicate_and_resolve_claims([env_sentinel, env_pulse, env_oracle])
    shared_detected = dedup_1.get("shared_sources_deduplicated_count", 0) >= 1
    pulse_dedup_env = next((e for e in dedup_1.get("envelopes", []) if e.get("agent") == "pulse"), {})
    discount_applied = float(pulse_dedup_env.get("independent_provenance_factor", 1.0)) < 1.0

    # Case 2: Independent sources across Atlas and Oracle
    env_atlas_ind = extract_claim_envelope(
        agent_name="atlas",
        domain="research",
        task="research.investigate",
        raw_output={
            "status": "complete",
            "report": {
                "summary": "Peer-reviewed macro policy desk confirms positive USD rate differential.",
                "sources": [
                    {"url": "https://arxiv.org/abs/2501.01234", "title": "Macro Rate Differentials", "source_tier": "Tier A"},
                    {"url": "chronicle-atlas-desk-catalog", "title": "Atlas Desk Catalog", "source_tier": "Tier A"},
                ],
                "confidence": 0.89,
            },
        },
        latency_ms=55.0,
    )
    dedup_2 = deduplicate_and_resolve_claims([env_atlas_ind, env_oracle])
    independent_ok = dedup_2.get("unique_provenance_sources_count", 0) >= 3

    passed_count = int(shared_detected) + int(discount_applied) + int(independent_ok)
    return {
        "dimension": "evidence_and_provenance_quality",
        "cases_total": 3,
        "cases_passed": passed_count,
        "accuracy_pct": round((passed_count / 3.0) * 100.0, 2),
        "shared_sources_deduplicated_count": dedup_1.get("shared_sources_deduplicated_count", 0),
        "unique_provenance_sources_count": dedup_1.get("unique_provenance_sources_count", 0),
        "resolution_after_dedup": dedup_1.get("resolution"),
        "conflict_detected": dedup_1.get("conflict_detected"),
    }


def run_aegis_policy_gate_suite() -> Dict[str, Any]:
    """Evaluate Aegis independent policy gate on Long/Short trade setups & Scout ledger reconciliation."""
    import tempfile
    from pathlib import Path
    from Oracle.intelligence.decision_ledger import OracleDecisionLedger  # type: ignore
    from Oracle.scout_agent.learner import reconcile_scout_state_with_ledger  # type: ignore

    # Verify live ledger integrity AND explicitly test purging an invalidated +67.764R ASX200 shadow replay
    with tempfile.TemporaryDirectory() as tmpdir:
        test_ledger = OracleDecisionLedger(ledger_path=Path(tmpdir) / "test_ledger.jsonl")
        test_ledger.record_counterfactual_replay(
            original_event_id="SKIPPED-ASX200-1",
            symbol="ASX200",
            direction="long",
            stage_rejected="shadow",
            reason_code="shadow_observation",
            would_have_won=True,
            counterfactual_r=67.764,
            classification="MISSED_WINNER",
            learning_adjustments={},
            strategy_id="v2_asx",
        )
        test_ledger.record_counterfactual_replay_invalidated(
            original_event_id="SKIPPED-ASX200-1",
            symbol="ASX200",
            reason_code="price_scale_mismatch",
        )
        contaminated_scout = {
            "shadow_counterfactual_outcomes": [
                {"original_event_id": "SKIPPED-ASX200-1", "symbol": "ASX200", "counterfactual_r": 67.764, "won": True, "strategy_id": "v2_asx"},
                {"original_event_id": "SKIPPED-EURUSD-1", "symbol": "EURUSD", "counterfactual_r": 2.15, "won": True, "strategy_id": "v2_eurusd"},
            ],
            "shadow_strategy_stats": {
                "v2_asx": {"replays": 1, "missed_winners": 1, "avoided_losses": 0, "sum_cf_r": 67.764},
            },
        }
        recon = reconcile_scout_state_with_ledger(contaminated_scout, test_ledger)
        asx_purged_ok = (
            recon["removed_invalid_outcomes_count"] >= 1
            and len(contaminated_scout["shadow_counterfactual_outcomes"]) == 1
            and contaminated_scout["shadow_counterfactual_outcomes"][0]["symbol"] == "EURUSD"
            and "v2_asx" not in contaminated_scout["shadow_strategy_stats"]
        )

    ledger_integrity = inspect_scout_and_ledger_integrity()

    # Test 1: Valid SHORT setup (EURUSD SHORT @ 1.09963, SL 1.10035, TP 1.09626 -> R:R = 4.68R)
    short_valid = evaluate_directional_trade_risk_policy(
        side="SHORT",
        entry_price=1.09963,
        stop_loss=1.10035,
        take_profit=1.09626,
        atr=0.00060,
    )

    # Test 2: Valid LONG setup (GBPUSD LONG @ 1.26500, SL 1.26380, TP 1.26764 -> R:R = 2.20R)
    long_valid = evaluate_directional_trade_risk_policy(
        side="LONG",
        entry_price=1.26500,
        stop_loss=1.26380,
        take_profit=1.26764,
        atr=0.00100,
    )

    # Test 3: Invalid LONG setup where SL > TP distance (V1 reward-gaming style: risking $38 to make $18)
    long_inverted_rr = evaluate_directional_trade_risk_policy(
        side="LONG",
        entry_price=2650.0,
        stop_loss=2612.0,   # Risk = $38
        take_profit=2668.0, # Reward = $18 -> R:R = 0.47R (< 1.80R constitutional floor)
        atr=25.0,
    )

    # Test 4: Invalid SHORT setup where stop_loss is on the wrong side of entry (SL < Entry on a SHORT)
    short_bad_geometry = evaluate_directional_trade_risk_policy(
        side="SHORT",
        entry_price=1.09963,
        stop_loss=1.09800,  # Wrong side for a short!
        take_profit=1.09626,
        atr=0.00060,
    )

    # Test 5: Ordinary factual research query must NOT be adjudicated or blocked by Aegis
    env_fact = extract_claim_envelope(
        agent_name="atlas",
        domain="research",
        task="research.investigate",
        raw_output={"status": "complete", "report": {"summary": "Wavelength lambda = v / f.", "sources": ["wikipedia:Wavelength"]}},
    )
    gate_fact = evaluate_aegis_policy_gate(
        query="What is the formula for wavelength?",
        primary_domain="research",
        participating_domains=["research"],
        envelopes=[env_fact],
    )

    checks = [
        ("short_valid_passes", short_valid["valid"] is True and short_valid["reward_to_risk_r"] >= 4.6),
        ("long_valid_passes", long_valid["valid"] is True and long_valid["reward_to_risk_r"] >= 2.1),
        ("long_inverted_rr_vetoed", long_inverted_rr["valid"] is False),
        ("short_bad_geometry_vetoed", short_bad_geometry["valid"] is False),
        ("factual_query_not_adjudicated_by_aegis", gate_fact["applicable"] is False and gate_fact["status"] == "NOT_APPLICABLE_FACTUAL_QUERY"),
        ("scout_asx200_outlier_reconciled", ledger_integrity["synced"] is True and asx_purged_ok),
    ]
    passed = sum(1 for _, ok in checks if ok)
    return {
        "dimension": "safety_and_aegis_policy_gate",
        "policy_version": "risk_policy.v2",
        "cases_total": len(checks),
        "cases_passed": passed,
        "accuracy_pct": round((passed / len(checks)) * 100.0, 2),
        "ledger_integrity": ledger_integrity,
        "checks": [{"check": name, "passed": ok} for name, ok in checks],
    }


def run_learning_and_hygiene_suite() -> Dict[str, Any]:
    """Evaluate quality-gated CollaborationGraph learning and Two-Tier Chronicle write-back hygiene."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        graph = CollaborationGraph(storage_dir=tmpdir)

        # 1. Low-quality unverified session must NOT inflate collaboration ordering quality
        graph.record_session(
            ["research", "social"],
            per_agent_success={"research": True, "social": True},
            overall_success=True,
            quality_score=0.35,
        )
        order_q_after_low = graph.order_quality(["research", "social"])

        # 2. High-quality provenance-verified session MUST reinforce collaboration ordering quality
        for _ in range(3):
            graph.record_session(
                ["research", "trading"],
                per_agent_success={"research": True, "trading": True},
                overall_success=True,
                quality_score=0.88,
            )
        order_q_after_high = graph.order_quality(["research", "trading"])

    # 3. Chronicle write-back hygiene: noisy answer stays Tier-1 episodic trace only
    noisy_env = extract_claim_envelope(
        agent_name="pulse",
        domain="social",
        task="social.sentiment",
        raw_output={"status": "error", "summary": "Unverified rumor"},
    )
    noisy_wb = classify_chronicle_writeback(
        query="Random unverified rumor",
        envelopes=[noisy_env],
        consensus_info={"unique_provenance_sources_count": 0, "resolution": "contested_hold_or_caution"},
        aegis_gate={"status": "CAUTION"},
        quality_signal={"quality_score": 0.42, "passed": False},
    )

    # 4. Validated multi-source synthesis is promoted to Tier-2 reusable semantic knowledge
    valid_env = extract_claim_envelope(
        agent_name="oracle",
        domain="trading",
        task="trading.signal",
        raw_output={
            "status": "complete",
            "summary": "Verified EURUSD short session won 3/3 (+6.336R) with SL 1.10035 < TP 1.09626 (4.68R).",
            "sources": [{"url": "universe://oracle/mt5_demo_trades.csv", "title": "MT5 Demo Ledger", "source_tier": "Tier A"}],
        },
    )
    promoted_wb = classify_chronicle_writeback(
        query="EURUSD session audit and macro policy alignment",
        envelopes=[valid_env],
        consensus_info={"unique_provenance_sources_count": 3, "resolution": "bearish_consensus"},
        aegis_gate={"status": "APPROVED"},
        quality_signal={"quality_score": 0.89, "passed": True},
    )

    checks = [
        ("low_quality_does_not_inflate_synergy", order_q_after_low == 0.0),
        ("high_quality_reinforces_synergy", order_q_after_high > 0.40),
        ("noisy_synthesis_kept_tier1_trace_only", noisy_wb["tier2_promoted_to_reusable_knowledge"] is False),
        ("verified_synthesis_promoted_to_tier2", promoted_wb["tier2_promoted_to_reusable_knowledge"] is True),
    ]
    passed = sum(1 for _, ok in checks if ok)
    return {
        "dimension": "learning_signal_and_chronicle_hygiene",
        "cases_total": len(checks),
        "cases_passed": passed,
        "accuracy_pct": round((passed / len(checks)) * 100.0, 2),
        "checks": [{"check": name, "passed": ok} for name, ok in checks],
    }


def run_full_benchmark_suite() -> Dict[str, Any]:
    t0 = time.time()
    classifier = DomainClassifier()
    d1 = run_routing_accuracy_suite(classifier)
    d2 = run_provenance_deduplication_suite()
    d3 = run_aegis_policy_gate_suite()
    d4 = run_learning_and_hygiene_suite()
    elapsed_ms = round((time.time() - t0) * 1000.0, 2)

    dimensions = [d1, d2, d3, d4]
    total_cases = sum(d["cases_total"] for d in dimensions)
    total_passed = sum(d["cases_passed"] for d in dimensions)
    overall_pct = round((total_passed / max(1, total_cases)) * 100.0, 2)

    report = {
        "benchmark_id": f"nexus_v2_benchmark_{int(time.time())}",
        "evaluated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "execution_latency_ms": elapsed_ms,
        "overall_cases_total": total_cases,
        "overall_cases_passed": total_passed,
        "overall_pass_rate_pct": overall_pct,
        "dimensions": {
            "routing_accuracy": d1,
            "evidence_and_provenance_quality": d2,
            "safety_and_aegis_policy_gate": d3,
            "learning_signal_and_chronicle_hygiene": d4,
        },
    }

    out_path = os.path.join(ROOT_DIR, "Chronicle", "memory", "research", "nexus_coordination_benchmark.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return report


if __name__ == "__main__":
    rep = run_full_benchmark_suite()
    print(json.dumps({
        "benchmark_id": rep["benchmark_id"],
        "overall_pass_rate_pct": rep["overall_pass_rate_pct"],
        "overall_cases_passed": f"{rep['overall_cases_passed']}/{rep['overall_cases_total']}",
        "execution_latency_ms": rep["execution_latency_ms"],
        "scout_ledger_in_sync": rep["dimensions"]["safety_and_aegis_policy_gate"]["ledger_integrity"]["synced"],
        "settled_shadow_replays": rep["dimensions"]["safety_and_aegis_policy_gate"]["ledger_integrity"]["settled_shadow_replays"],
        "v2_trades_closed": rep["dimensions"]["safety_and_aegis_policy_gate"]["ledger_integrity"]["v2_trades_closed"],
    }, indent=2))
