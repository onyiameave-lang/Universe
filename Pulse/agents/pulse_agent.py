"""
Pulse.agents.pulse_agent
=======================
Pulse (formerly SocialIntel): the institutional social intelligence desk, on
the constitutional BaseAgent. (Book I Part IV Article VII; Book II Ch IV.)

Pulse reasons about its acquisition STRATEGY via `solve("social_path", ...)`:
  * retail_pulse   Reddit + StockTwits: retail trader mood (fast, free).
  * practitioner   Hacker News + Reddit tech: informed discourse.
  * broad_sweep    all platforms: maximum coverage + manipulation cross-check.

It learns which path yields authentic, low-manipulation signal for which query.
Authenticity-weighted sentiment and coordinated-manipulation flagging throughout,
so bots and pump campaigns never distort the read.
"""
from __future__ import annotations

import logging
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
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "pulse")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("pulse")

PATH_SOURCES = {
    "retail_pulse": ["reddit", "stocktwits"],
    "practitioner": ["hackernews", "reddit"],
    "broad_sweep": ["reddit", "hackernews", "stocktwits"],
}


class PulseAgent(BaseAgent):
    name = "pulse"
    repository = "Pulse"
    domain = "social"
    description = "The institutional social intelligence desk."
    capabilities = ["social.collect", "social.report", "social.sentiment", "social.for_symbol",
                    "social.trends", "social.manipulation"]
    channels = ["ecosystem.social", "ecosystem.intelligence", "ecosystem.broadcast"]
    memory_namespace = "pulse_memory"
    security_level = "standard"
    mission = {"purpose": "Read authentic social sentiment; flag manipulation; detect trends."}

    def __init__(self, chronicle_client=None, **kw):
        super().__init__(chronicle_client=chronicle_client, storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.engine = IntelligenceEngine(chronicle_client=chronicle_client, llm=self.llm)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("social_path", "retail_pulse", "_strat_retail",
            reasons_for=["retail mood, fast, free"], reasons_against=["noisier; more hype"])
        self.reasoning.register_strategy("social_path", "practitioner", "_strat_pract",
            reasons_for=["more informed discourse"], reasons_against=["smaller volume"])
        self.reasoning.register_strategy("social_path", "broad_sweep", "_strat_broad",
            reasons_for=["max coverage + manipulation cross-check"], reasons_against=["slower"])

    def on_start(self) -> None:
        avail = [n for n, ok in self.engine.stats()["collectors"].items() if ok]
        log.info("Pulse desk online. Platforms: %s | Brain: %s", avail, self.has_brain)

    def _strat_retail(self, c): return self._report(c, "retail_pulse")
    def _strat_pract(self, c): return self._report(c, "practitioner")
    def _strat_broad(self, c): return self._report(c, "broad_sweep")

    def _report(self, context, path) -> Dict[str, Any]:
        out = self.engine.report(topics=context.get("topics"), sources=PATH_SOURCES[path])
        ok = out.get("report") is not None and out["report"]["post_count"] > 0
        return {"status": "complete" if ok else "error",
               "message": "" if ok else "no social signal via this path",
               "report": out.get("report"), "path": path, "source_status": out.get("source_status")}

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        if task in ("social.report", "social.collect", "social.trends"):
            if self.reasoning is not None:
                solved = self.solve("social_path", {"topics": ctx.get("topics")})
                if solved.get("status") == "complete":
                    rep = solved.get("report") or {}
                    if task == "social.trends":
                        return {"status": "complete", "trends": rep.get("trending", [])}
                    return {"status": "complete", "report": rep, "social_path": solved.get("path")}
                return {"status": "complete", **self.engine.report(
                    topics=ctx.get("topics"), sources=PATH_SOURCES["broad_sweep"]),
                    "note": "fell back to broad sweep"}
            return {"status": "complete", **self.engine.report(topics=ctx.get("topics"))}
        if task in ("social.sentiment", "social.for_symbol"):
            return {"status": "complete", "sentiment": self.engine.sentiment_for(ctx.get("symbol", ""))}
        if task == "social.manipulation":
            g = self.engine.gather(topics=ctx.get("topics"))
            from intelligence.authenticity import detect_manipulation
            return {"status": "complete", **detect_manipulation(g["posts"], symbol=ctx.get("symbol"))}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["engine"] = self.engine.stats()
        return base

    # in-process convenience for Oracle / Nexus
    def sentiment_for(self, symbol: str):
        return self.engine.sentiment_for(symbol)
