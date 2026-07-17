"""
Pulse.agents.pulse_agent  (v4 — fallback mode surfacing + LLM gate)
====================================================================
Changes vs v1:
  1. PULSE_LLM_MODE env var gate — "full" (default) or "essential_only".
     In essential_only mode, the reasoning/strategy-selection path (which
     calls the LLM via solve()) is bypassed entirely; the engine falls
     straight through to the broad_sweep rule-based path.
  2. All execute() return values now carry "fallback_mode" from the engine
     report so the caller (Oracle, user REPL) can see whether the result
     came from Chronicle cache, rule-based scoring, or a full LLM run.
  3. sentiment_for() surfaces fallback_mode from the engine result.
  4. get_status() includes llm_mode and llm_stats for observability.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

from core.intelligence_engine import IntelligenceEngine  # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False

    class BaseAgent:
        reasoning = None
        def __init__(self, **kw):
            self._started = False
            self._handled = 0
            self._failed  = 0
            self.llm      = None
        def act(self, task, context=None):
            return self.execute(task, context or {})
        def get_status(self):
            return {"name": getattr(self, "name", "pulse")}
        def solve(self, *a, **k):
            return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self):
            self._started = True
            self.on_start()
        def stop(self):
            self._started = False

log = logging.getLogger("pulse")

# ── LLM mode gate ─────────────────────────────────────────────────────────────
_PULSE_LLM_MODE = os.getenv("PULSE_LLM_MODE",
                             os.getenv("ORACLE_LLM_MODE", "full")).lower()
_USE_LLM_REASONING = (_PULSE_LLM_MODE != "essential_only")

PATH_SOURCES = {
    "retail_pulse": ["reddit", "stocktwits"],
    "practitioner": ["hackernews", "reddit"],
    "broad_sweep":  ["reddit", "hackernews", "stocktwits"],
}


class PulseAgent(BaseAgent):
    name             = "pulse"
    repository       = "Pulse"
    domain           = "social"
    description      = "The institutional social intelligence desk."
    capabilities     = ["social.collect", "social.report", "social.sentiment",
                        "social.for_symbol", "social.trends", "social.manipulation"]
    channels         = ["ecosystem.social", "ecosystem.intelligence",
                        "ecosystem.broadcast"]
    memory_namespace = "pulse_memory"
    security_level   = "standard"
    mission          = {"purpose": "Read authentic social sentiment; "
                                   "flag manipulation; detect trends."}

    def __init__(self, chronicle_client=None, **kw):
        super().__init__(
            chronicle_client=chronicle_client,
            storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.engine = IntelligenceEngine(
            chronicle_client=chronicle_client, llm=self.llm)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy(
            "social_path", "retail_pulse", "_strat_retail",
            reasons_for=["retail mood, fast, free"],
            reasons_against=["noisier; more hype"])
        self.reasoning.register_strategy(
            "social_path", "practitioner", "_strat_pract",
            reasons_for=["more informed discourse"],
            reasons_against=["smaller volume"])
        self.reasoning.register_strategy(
            "social_path", "broad_sweep", "_strat_broad",
            reasons_for=["max coverage + manipulation cross-check"],
            reasons_against=["slower"])

    def on_start(self) -> None:
        avail = [n for n, ok in self.engine.stats()["collectors"].items() if ok]
        log.info("Pulse desk online. Platforms: %s | Brain: %s | LLM mode: %s",
                 avail, self.has_brain, _PULSE_LLM_MODE)

    def _strat_retail(self, c): return self._report(c, "retail_pulse")
    def _strat_pract(self, c):  return self._report(c, "practitioner")
    def _strat_broad(self, c):  return self._report(c, "broad_sweep")

    def _report(self, context: Dict[str, Any],
                path: str) -> Dict[str, Any]:
        out = self.engine.report(
            topics=context.get("topics"),
            sources=PATH_SOURCES[path])
        rep = out.get("report")
        ok  = rep is not None and rep.get("post_count", 0) > 0
        return {
            "status":       "complete" if ok else "error",
            "message":      "" if ok else "no social signal via this path",
            "report":       rep,
            "path":         path,
            "source_status": out.get("source_status"),
            "fallback_mode": rep.get("fallback_mode") if rep else "no_data",
        }

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context

        if task in ("social.report", "social.collect", "social.trends"):
            # ── LLM reasoning path (skipped in essential_only) ────────────────
            if _USE_LLM_REASONING and self.reasoning is not None:
                solved = self.solve("social_path", {"topics": ctx.get("topics")})
                if solved.get("status") == "complete":
                    rep = solved.get("report") or {}
                    if task == "social.trends":
                        return {"status": "complete",
                                "trends": rep.get("trending", []),
                                "fallback_mode": solved.get("fallback_mode",
                                                            "llm_reasoning")}
                    return {
                        "status":       "complete",
                        "report":       rep,
                        "social_path":  solved.get("path"),
                        "fallback_mode": solved.get("fallback_mode",
                                                    "llm_reasoning"),
                    }
                # LLM reasoning failed — fall through to broad sweep
                log.info("Pulse: LLM reasoning failed (%s), "
                         "falling back to broad_sweep rule-based path",
                         solved.get("message", "unknown"))

            # ── Rule-based broad sweep (always available) ─────────────────────
            out = self.engine.report(topics=ctx.get("topics"))
            rep = out.get("report")
            if task == "social.trends":
                return {
                    "status":       "complete",
                    "trends":       rep.get("trending", []) if rep else [],
                    "fallback_mode": rep.get("fallback_mode", "rule_based")
                                    if rep else "no_data",
                    "note":         out.get("note", ""),
                }
            return {
                "status":       "complete",
                "report":       rep,
                "fallback_mode": rep.get("fallback_mode", "rule_based")
                                 if rep else "no_data",
                "note":         out.get("note", ""),
                **{k: v for k, v in out.items()
                   if k not in ("status", "report", "note")},
            }

        if task in ("social.sentiment", "social.for_symbol"):
            result = self.engine.sentiment_for(ctx.get("symbol", ""))
            return {"status": "complete", "sentiment": result}

        if task == "social.manipulation":
            g = self.engine.gather(topics=ctx.get("topics"))
            from intelligence.authenticity import detect_manipulation  # type: ignore
            return {
                "status": "complete",
                **detect_manipulation(g["posts"], symbol=ctx.get("symbol")),
            }

        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["engine"]   = self.engine.stats()
        base["llm_mode"] = _PULSE_LLM_MODE
        if self.llm is not None:
            base["llm_stats"] = self.llm.stats()
        return base

    # ── in-process convenience for Oracle / Nexus ─────────────────────────────
    def sentiment_for(self, symbol: str) -> Dict[str, Any]:
        return self.engine.sentiment_for(symbol)
