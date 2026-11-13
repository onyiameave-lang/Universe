"""
Oracle.intelligence.decision_ledger
===================================
Constitutional Audit, Counterfactual & Failure Exposure Ledger for Oracle V2.
(Book I Article IX Trial-and-Error Learning; Book IV Zero Silent Failures;
Book V Part II Decision Making & Reflection; Book VI Accountability.)

Every decision and event in Oracle V2 is recorded here with zero silent failures:
  1. TRADE_ENTERED          Why Oracle entered (Scout tournament rank, competing
                            setups beaten, Portfolio slot state, Risk correlation
                            check, Sentinel news state, Pulse social state).
  2. TRADE_CLOSED           Realized win/loss, R-multiple, exit reason, and the
                            exact learning updates applied across sub-agents.
  3. TRADE_NOT_ENTERED      Every candidate setup that was NOT entered (whether
                            outranked in the Scout tournament, gated by Portfolio
                            slot/daily budget, rejected by Risk correlation/news/
                            social, or below learned conviction floor), including
                            its counterfactual entry/stop/target so Chronicle Night
                            Research & Forge Shadow Runner can replay and learn
                            from missed winners and avoided losses.
  4. COUNTERFACTUAL_REPLAY  Result of replaying a TRADE_NOT_ENTERED setup against
                            subsequent market bars or on-demand Shadow Runner.
  5. SYSTEM_FAILURE_EXPOSED Every operational, data, broker, Sentinel, Pulse,
                            Atlas, or Forge failure. Never hidden or swallowed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.decision_ledger")

_DEFAULT_LEDGER_PATH = (
    Path(__file__).resolve().parents[1] / "memory" / "oracle_decision_ledger.jsonl"
)


class OracleDecisionLedger:
    """
    Append-only JSONL + Chronicle-synced ledger for all Oracle V2 decisions,
    outcomes, unentered counterfactuals, and exposed failures.
    """

    def __init__(
        self,
        ledger_path: Optional[Path | str] = None,
        storage_dir: Optional[Path | str] = None,
        chronicle_client: Any = None,
    ) -> None:
        if ledger_path is not None:
            resolved_path = Path(ledger_path)
        elif storage_dir is not None:
            resolved_path = Path(storage_dir) / "oracle_decision_ledger.jsonl"
        else:
            resolved_path = _DEFAULT_LEDGER_PATH
        self.ledger_path = resolved_path
        self.chronicle = chronicle_client
        self._lock = threading.RLock()
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _make_event_id(prefix: str, symbol: str, ts: float) -> str:
        digest = hashlib.sha256(f"{prefix}:{symbol}:{ts:.6f}:{os.urandom(4).hex()}".encode("utf-8")).hexdigest()[:16]
        return f"{prefix}-{symbol.upper()}-{int(ts)}-{digest}"

    def _append_record(self, record: Dict[str, Any], tags: List[str], summary: str) -> Dict[str, Any]:
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

        if self.chronicle is not None:
            try:
                store_fn = getattr(self.chronicle, "store", None)
                if callable(store_fn):
                    store_fn(
                        content=line,
                        memory_type="episodic",
                        domain="oracle_v2_ledger",
                        summary=summary,
                        tags=tags,
                        source="oracle_v2",
                    )
                elif hasattr(self.chronicle, "act"):
                    self.chronicle.act(
                        "memory.store",
                        {
                            "content": line,
                            "pillar": "episodic",
                            "domain": "oracle_v2_ledger",
                            "summary": summary,
                            "tags": tags,
                            "_sender": "oracle_v2",
                        },
                    )
            except Exception as exc:
                # Even a Chronicle sync failure must be logged visibly, never silently ignored
                log.error("[FAILURE_EXPOSED] Chronicle sync failed for %s: %s", record.get("event_id"), exc)
        return record

    def record_trade_entered(
        self,
        *,
        symbol: str,
        direction: str,
        entry_price: float = 0.0,
        stop_price: float = 0.0,
        stop: Optional[float] = None,
        target_price: float = 0.0,
        target: Optional[float] = None,
        size: float = 0.0,
        scout_score: float = 0.0,
        effective_confidence: float = 0.0,
        entry_confidence: Optional[float] = None,
        learned_conviction_floor: float = 0.5,
        tournament_rank: int = 1,
        candidates_evaluated: int = 1,
        regime: str = "ranging",
        session: str = "london",
        strategy_id: Optional[str] = None,
        streams: Optional[Dict[str, Any]] = None,
        entry_streams: Optional[Dict[str, Any]] = None,
        news_assessment: Optional[Dict[str, Any]] = None,
        news_impact: str = "none",
        news_reason: str = "",
        social_assessment: Optional[Dict[str, Any]] = None,
        social_risk: str = "none",
        social_reason: str = "",
        portfolio_state: Optional[Dict[str, Any]] = None,
        risk_assessment: Optional[Dict[str, Any]] = None,
        why_trade_summary: str = "",
        why_reasons: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        now = time.time()
        event_id = self._make_event_id("ENTERED", symbol, now)
        eff_conf = float(entry_confidence if entry_confidence is not None else effective_confidence)
        s_price = float(stop if stop is not None else stop_price)
        t_price = float(target if target is not None else target_price)
        n_assess = dict(news_assessment or {"level": news_impact, "reason": news_reason})
        s_assess = dict(social_assessment or {"level": social_risk, "reason": social_reason})
        w_summary = why_trade_summary or ("; ".join(why_reasons) if why_reasons else "Selected in CIO Scout Tournament")

        record = {
            "schema": "oracle.v2.decision_ledger.v1",
            "event_type": "TRADE_ENTERED",
            "event_id": event_id,
            "timestamp": now,
            "symbol": symbol.upper(),
            "direction": direction.lower(),
            "entry_price": float(entry_price),
            "stop_price": s_price,
            "target_price": t_price,
            "size": float(size),
            "scout_score": round(float(scout_score), 4),
            "effective_confidence": round(eff_conf, 4),
            "learned_conviction_floor": round(float(learned_conviction_floor), 4),
            "tournament_rank": int(tournament_rank),
            "candidates_evaluated": int(candidates_evaluated),
            "regime": str(regime),
            "session": str(session),
            "strategy_id": strategy_id,
            "streams": streams or entry_streams or {},
            "news_assessment": n_assess,
            "news_impact": str(n_assess.get("level") or news_impact or "none"),
            "social_assessment": s_assess,
            "social_risk": str(s_assess.get("level") or social_risk or "none"),
            "portfolio_state": portfolio_state or {},
            "risk_assessment": risk_assessment or {},
            "why_trade_summary": w_summary,
        }
        summary = (
            f"ENTERED #{tournament_rank}/{candidates_evaluated} {direction.upper()} {symbol.upper()} "
            f"(score={scout_score:.3f}, conf={eff_conf:.3f}): {w_summary[:120]}"
        )
        return self._append_record(
            record,
            tags=["oracle_v2", "trade_entered", symbol.upper(), str(regime)],
            summary=summary,
        )

    def record_trade_not_entered(
        self,
        *,
        symbol: str,
        direction: str,
        hypothetical_entry: float = 0.0,
        hypothetical_entry_price: Optional[float] = None,
        hypothetical_stop: float = 0.0,
        hypothetical_target: float = 0.0,
        scout_score: float = 0.0,
        effective_confidence: float = 0.0,
        entry_confidence: Optional[float] = None,
        learned_conviction_floor: float = 0.5,
        tournament_rank: Optional[int] = None,
        candidates_evaluated: int = 1,
        regime: str = "ranging",
        session: str = "london",
        strategy_id: Optional[str] = None,
        stage_rejected: str = "scout",
        rejection_stage: Optional[str] = None,
        reason_code: str = "",
        primary_reason: Optional[str] = None,
        reasons: Optional[List[str]] = None,
        streams: Optional[Dict[str, Any]] = None,
        entry_streams: Optional[Dict[str, Any]] = None,
        news_assessment: Optional[Dict[str, Any]] = None,
        news_impact: Optional[str] = None,
        news_reason: str = "",
        social_assessment: Optional[Dict[str, Any]] = None,
        social_risk: Optional[str] = None,
        social_reason: str = "",
        atr: float = 0.0,
        **extra: Any,
    ) -> Dict[str, Any]:
        now = time.time()
        event_id = self._make_event_id("SKIPPED", symbol, now)
        h_entry = float(hypothetical_entry_price if hypothetical_entry_price is not None else hypothetical_entry)
        eff_conf = float(entry_confidence if entry_confidence is not None else effective_confidence)
        resolved_reason_code = reason_code or rejection_stage or primary_reason or stage_rejected or "rejected"
        resolved_rejection_stage = rejection_stage or reason_code or stage_rejected or "scout"
        resolved_reasons = list(reasons) if reasons else [primary_reason or resolved_reason_code]
        n_assess = dict(news_assessment or {})
        if news_impact and "level" not in n_assess:
            n_assess["level"] = news_impact
        if news_reason and "reason" not in n_assess:
            n_assess["reason"] = news_reason
        s_assess = dict(social_assessment or {})
        if social_risk and "level" not in s_assess:
            s_assess["level"] = social_risk
        if social_reason and "reason" not in s_assess:
            s_assess["reason"] = social_reason

        resolved_news_impact = str(news_impact or n_assess.get("level") or "none")
        resolved_social_risk = str(social_risk or s_assess.get("level") or "none")

        record = {
            "schema": "oracle.v2.decision_ledger.v1",
            "event_type": "TRADE_NOT_ENTERED",
            "event_id": event_id,
            "timestamp": now,
            "symbol": symbol.upper(),
            "direction": direction.lower(),
            "hypothetical_entry": h_entry,
            "hypothetical_entry_price": h_entry,
            "hypothetical_stop": float(hypothetical_stop or 0.0),
            "hypothetical_target": float(hypothetical_target or 0.0),
            "atr": float(atr or 0.0),
            "scout_score": round(float(scout_score), 4),
            "effective_confidence": round(eff_conf, 4),
            "entry_confidence": round(eff_conf, 4),
            "learned_conviction_floor": round(float(learned_conviction_floor), 4),
            "tournament_rank": tournament_rank,
            "candidates_evaluated": int(candidates_evaluated),
            "regime": str(regime),
            "session": str(session),
            "strategy_id": strategy_id,
            "stage_rejected": stage_rejected,
            "rejection_stage": resolved_rejection_stage,
            "reason_code": resolved_reason_code,
            "primary_reason": primary_reason or (resolved_reasons[0] if resolved_reasons else resolved_reason_code),
            "reasons": resolved_reasons,
            "streams": streams or entry_streams or {},
            "news_assessment": n_assess,
            "news_impact": resolved_news_impact,
            "social_assessment": s_assess,
            "social_risk": resolved_social_risk,
            "counterfactual_evaluated": False,
        }
        summary = (
            f"NOT_ENTERED [{stage_rejected.upper()}:{resolved_reason_code}] {direction.upper()} {symbol.upper()} "
            f"(score={scout_score:.3f}, conf={eff_conf:.3f}): {'; '.join(resolved_reasons)[:120]}"
        )
        return self._append_record(
            record,
            tags=["oracle_v2", "trade_not_entered", stage_rejected, resolved_reason_code, symbol.upper()],
            summary=summary,
        )

    def record_trade_closed(
        self,
        *,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl_r: float,
        won: bool,
        exit_reason: str,
        duration_sec: float,
        entry_regime: str,
        exit_regime: str,
        strategy_id: Optional[str],
        learning_updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = time.time()
        event_id = self._make_event_id("CLOSED", symbol, now)
        outcome_label = "WIN" if won else "LOSS"
        record = {
            "schema": "oracle.v2.decision_ledger.v1",
            "event_type": "TRADE_CLOSED",
            "event_id": event_id,
            "timestamp": now,
            "symbol": symbol.upper(),
            "direction": direction.lower(),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "pnl_r": round(float(pnl_r), 4),
            "won": bool(won),
            "outcome_label": outcome_label,
            "exit_reason": str(exit_reason),
            "duration_sec": round(float(duration_sec), 1),
            "entry_regime": str(entry_regime),
            "exit_regime": str(exit_regime),
            "strategy_id": strategy_id,
            "learning_updates": learning_updates,
        }
        summary = (
            f"CLOSED {outcome_label} {direction.upper()} {symbol.upper()} "
            f"{pnl_r:+.2f}R ({exit_reason}) in {duration_sec / 60.0:.1f}m"
        )
        return self._append_record(
            record,
            tags=["oracle_v2", "trade_closed", outcome_label.lower(), symbol.upper()],
            summary=summary,
        )

    def record_counterfactual_replay(
        self,
        *,
        original_event_id: str,
        symbol: str,
        direction: str,
        stage_rejected: str,
        reason_code: str,
        would_have_won: bool,
        counterfactual_r: float,
        classification: str,  # "AVOIDED_LOSS" | "MISSED_WINNER" | "NEUTRAL"
        learning_adjustments: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = time.time()
        event_id = self._make_event_id("CF_REPLAY", symbol, now)
        record = {
            "schema": "oracle.v2.decision_ledger.v1",
            "event_type": "COUNTERFACTUAL_REPLAY",
            "event_id": event_id,
            "original_event_id": original_event_id,
            "timestamp": now,
            "symbol": symbol.upper(),
            "direction": direction.lower(),
            "stage_rejected": stage_rejected,
            "reason_code": reason_code,
            "would_have_won": bool(would_have_won),
            "counterfactual_r": round(float(counterfactual_r), 4),
            "classification": classification,
            "learning_adjustments": learning_adjustments,
        }
        summary = (
            f"COUNTERFACTUAL {classification} on {symbol.upper()} ({original_event_id}): "
            f"would have realized {counterfactual_r:+.2f}R after {stage_rejected}:{reason_code}"
        )
        return self._append_record(
            record,
            tags=["oracle_v2", "counterfactual_replay", classification.lower(), symbol.upper()],
            summary=summary,
        )

    def expose_failure(
        self,
        *,
        component: str,
        operation: str,
        error_type: str,
        error_message: str,
        symbol: Optional[str] = None,
        severity: str = "ERROR",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Constitutional Mandate: Failure is never hidden. Every failure is
        explicitly surfaced in logs, persisted to the decision ledger, and
        stored in Chronicle.
        """
        now = time.time()
        sym = (symbol or "SYSTEM").upper()
        event_id = self._make_event_id("FAILURE", sym, now)
        record = {
            "schema": "oracle.v2.decision_ledger.v1",
            "event_type": "SYSTEM_FAILURE_EXPOSED",
            "event_id": event_id,
            "timestamp": now,
            "severity": severity.upper(),
            "component": component,
            "operation": operation,
            "symbol": sym,
            "error_type": error_type,
            "error_message": error_message,
            "context": context or {},
        }
        log.error(
            "[ORACLE_V2_FAILURE_EXPOSED] [%s] component=%s op=%s symbol=%s error=%s: %s",
            severity.upper(),
            component,
            operation,
            sym,
            error_type,
            error_message,
        )
        summary = f"FAILURE EXPOSED [{severity.upper()}] {component}.{operation} ({sym}): {error_type}: {error_message[:120]}"
        return self._append_record(
            record,
            tags=["oracle_v2", "failure_exposed", component, sym],
            summary=summary,
        )

    def read_all(self, event_type: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with self._lock:
            for raw_line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    log.error("[ORACLE_V2_FAILURE_EXPOSED] Corrupted line in decision ledger: %s", exc)
                    continue
                if event_type and item.get("event_type") != event_type:
                    continue
                records.append(item)
        if limit is not None and limit > 0:
            return records[-limit:]
        return records

    def load_records(self, event_type: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Alias for read_all(event_type=..., limit=...)."""
        return self.read_all(event_type=event_type, limit=limit)

    def unentered_trades_pending_replay(self, max_items: int = 50) -> List[Dict[str, Any]]:
        """
        Return TRADE_NOT_ENTERED records that have valid hypothetical entry/stop/target
        levels and have not yet been replayed by Chronicle Night Research / Forge Shadow Runner.
        """
        all_records = self.read_all()
        replayed_ids = {
            r.get("original_event_id")
            for r in all_records
            if r.get("event_type") == "COUNTERFACTUAL_REPLAY" and r.get("original_event_id")
        }
        pending = []
        for rec in all_records:
            if rec.get("event_type") != "TRADE_NOT_ENTERED":
                continue
            if rec.get("event_id") in replayed_ids:
                continue
            if float(rec.get("hypothetical_entry") or 0.0) <= 0:
                continue
            if float(rec.get("hypothetical_stop") or 0.0) <= 0:
                continue
            pending.append(rec)
        return pending[-max_items:]

    def summarize_diagnostics(self) -> Dict[str, Any]:
        """
        Aggregate win/loss performance, unentered trade reasons, counterfactual
        lessons, and exposed failures for Oracle <-> Atlas <-> Forge reviews.
        """
        all_records = self.read_all()
        entered = [r for r in all_records if r.get("event_type") == "TRADE_ENTERED"]
        closed = [r for r in all_records if r.get("event_type") == "TRADE_CLOSED"]
        not_entered = [r for r in all_records if r.get("event_type") == "TRADE_NOT_ENTERED"]
        replays = [r for r in all_records if r.get("event_type") == "COUNTERFACTUAL_REPLAY"]
        failures = [r for r in all_records if r.get("event_type") == "SYSTEM_FAILURE_EXPOSED"]

        by_symbol_regime: Dict[str, Dict[str, Any]] = {}
        for c in closed:
            key = f"{c.get('symbol', 'UNKNOWN')}::{c.get('entry_regime', 'unknown')}"
            bucket = by_symbol_regime.setdefault(
                key,
                {
                    "symbol": c.get("symbol", "UNKNOWN"),
                    "regime": c.get("entry_regime", "unknown"),
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_r": 0.0,
                    "exit_reasons": {},
                },
            )
            bucket["trades"] += 1
            if c.get("won"):
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
            bucket["total_r"] = round(bucket["total_r"] + float(c.get("pnl_r", 0.0)), 4)
            er = str(c.get("exit_reason", "unknown"))
            bucket["exit_reasons"][er] = bucket["exit_reasons"].get(er, 0) + 1

        losing_buckets = [
            {
                **b,
                "win_rate": round(b["wins"] / b["trades"], 4) if b["trades"] else 0.0,
                "expectancy_r": round(b["total_r"] / b["trades"], 4) if b["trades"] else 0.0,
            }
            for b in by_symbol_regime.values()
            if b["trades"] > 0 and b["total_r"] < 0
        ]
        losing_buckets.sort(key=lambda item: item["total_r"])

        skip_reasons: Dict[str, int] = {}
        for ne in not_entered:
            code = f"{ne.get('stage_rejected', 'unknown')}:{ne.get('reason_code', 'unknown')}"
            skip_reasons[code] = skip_reasons.get(code, 0) + 1

        missed_winners = [r for r in replays if r.get("classification") == "MISSED_WINNER"]
        avoided_losses = [r for r in replays if r.get("classification") == "AVOIDED_LOSS"]

        wins_count = sum(1 for c in closed if c.get("won"))
        losses_count = sum(1 for c in closed if not c.get("won"))
        pending_cf = len(self.unentered_trades_pending_replay(max_items=500))

        return {
            "total_events": len(all_records),
            "trades_entered": len(entered),
            "trades_closed": len(closed),
            "wins": wins_count,
            "losses": losses_count,
            "trades_not_entered": len(not_entered),
            "counterfactuals_replayed": len(replays),
            "missed_winners_count": len(missed_winners),
            "avoided_losses_count": len(avoided_losses),
            "counterfactual_replays": {
                "total_replayed": len(replays),
                "missed_winners": len(missed_winners),
                "avoided_losses": len(avoided_losses),
                "pending_unreplayed": pending_cf,
            },
            "exposed_failures_count": len(failures),
            "exposed_system_failures": len(failures),
            "recent_failures": failures[-10:],
            "skip_reason_counts": skip_reasons,
            "no_trade_reasons": skip_reasons,
            "losing_symbol_regimes": losing_buckets,
            "by_symbol_regime": by_symbol_regime,
        }

    def summary(self, window_sec: Optional[float] = None) -> Dict[str, Any]:
        """Return full diagnostic summary of the Decision Ledger."""
        return self.summarize_diagnostics()
