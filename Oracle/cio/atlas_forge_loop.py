"""
Oracle.cio.atlas_forge_loop
===========================
Oracle <-> Atlas <-> Forge Research & Accountability Loop.
Inspects the Decision Ledger for losing (symbol, regime) clusters, queries
Atlas for new quantitative advancements, and dispatches concrete deficiency
and retraining mandates back to Forge.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def conduct_atlas_forge_review(
    *,
    oracle_agent: Any,
    decision_ledger: Any,
    focus_symbol: Optional[str] = None,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Execute the Oracle <-> Atlas <-> Forge Research & Accountability Loop.
    """
    diag = decision_ledger.summarize_diagnostics()
    losing_clusters = diag.get("losing_symbol_regimes") or []
    if symbols:
        wanted = {str(s).strip().upper() for s in symbols if str(s).strip()}
        matching = [c for c in losing_clusters if str(c.get("symbol") or "").upper() in wanted]
        if not matching:
            matching = [
                {
                    "symbol": sym_up,
                    "regime": "ranging",
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_r": 0.0,
                    "expectancy_r": 0.0,
                    "exit_reasons": {},
                }
                for sym_up in sorted(wanted)
            ]
        targets = matching[:3]
    elif focus_symbol:
        sym_up = focus_symbol.upper()
        matching = [c for c in losing_clusters if c.get("symbol") == sym_up]
        if not matching:
            matching = [{
                "symbol": sym_up,
                "regime": "ranging",
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_r": 0.0,
                "expectancy_r": 0.0,
                "exit_reasons": {},
            }]
        targets = matching[:3]
    else:
        targets = losing_clusters[:3] if losing_clusters else [{
            "symbol": "EURUSD",
            "regime": "ranging",
            "trades": diag.get("trades_closed", 0),
            "wins": 0,
            "losses": 0,
            "total_r": 0.0,
            "expectancy_r": 0.0,
            "exit_reasons": {},
        }]

    atlas_client = getattr(oracle_agent, "atlas", None)
    forge_client = getattr(oracle_agent, "forge", None)

    atlas_findings: List[Dict[str, Any]] = []
    forge_mandates: List[Dict[str, Any]] = []

    for cluster in targets:
        sym = str(cluster.get("symbol") or "EURUSD").upper()
        regime = str(cluster.get("regime") or "ranging")
        exp_r = float(cluster.get("expectancy_r") or 0.0)
        query = (
            f"Institutional quantitative trading advancements and regime adaptation for {sym} "
            f"in {regime} market conditions (realized expectancy {exp_r:+.2f}R, "
            f"missed winners={diag.get('missed_winners_count', 0)}). "
            f"Identify concrete feature, multi-timeframe structure, and execution-filter improvements."
        )
        atlas_summary = "Atlas unavailable; mandate generated from Oracle empirical ledger."
        atlas_sources: List[Any] = []
        if atlas_client is not None:
            try:
                if hasattr(atlas_client, "act"):
                    res = atlas_client.act(
                        "research.investigate",
                        {
                            "query": query,
                            "domain": "trading",
                            "depth": "standard",
                            "_sender": "oracle_v2_cio",
                        },
                    )
                elif hasattr(atlas_client, "handle"):
                    res = atlas_client.handle({"context": {"query": query, "domain": "trading"}})
                else:
                    res = {}
                rep = (res or {}).get("report") or (res or {})
                if isinstance(rep, dict) and rep.get("summary"):
                    atlas_summary = str(rep["summary"])
                    atlas_sources = list(rep.get("sources") or [])
            except Exception as exc:
                decision_ledger.expose_failure(
                    component="Atlas",
                    operation="research.investigate",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=sym,
                )

        atlas_findings.append({
            "symbol": sym,
            "regime": regime,
            "query": query,
            "summary": atlas_summary,
            "sources": atlas_sources,
        })

        mandate_statement = (
            f"Oracle V2 CIO Deficiency Mandate for {sym}::{regime} "
            f"(realized E[R]={exp_r:+.2f}R): integrate Atlas research findings "
            f"({atlas_summary[:140]}) and retrain multi-timeframe entry/exit blueprint."
        )
        forge_status = "forge_not_connected"
        forge_response: Dict[str, Any] = {}
        if forge_client is not None:
            try:
                if hasattr(forge_client, "act"):
                    forge_response = forge_client.act(
                        "oracle.improvement.request",
                        {
                            "request": {
                                "symbol": sym,
                                "regime": regime,
                                "reason": mandate_statement,
                                "performance_metrics": cluster,
                                "atlas_research_summary": atlas_summary,
                            },
                            "statement": mandate_statement,
                            "proposed_by": "oracle_v2_cio",
                        },
                    ) or {}
                    forge_status = str(forge_response.get("status") or "submitted")
            except Exception as exc:
                decision_ledger.expose_failure(
                    component="Forge",
                    operation="oracle.improvement.request",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=sym,
                )
                forge_status = f"error:{type(exc).__name__}"

        forge_mandates.append({
            "symbol": sym,
            "regime": regime,
            "deficiency_metrics": cluster,
            "atlas_research_summary": atlas_summary,
            "mandate_statement": mandate_statement,
            "forge_status": forge_status,
            "forge_response": forge_response,
        })

    return {
        "status": "complete",
        "schema": "oracle.v2.atlas_forge_review.v1",
        "diagnostics_summary": diag,
        "deficiencies_detected": targets,
        "atlas_findings": atlas_findings,
        "atlas_insights": atlas_findings,
        "forge_mandates": forge_mandates,
    }
