"""
Sentinel.agents.sentinel_agent
=============================
Sentinel (formerly NewsIntel): the institutional news desk, on the
constitutional BaseAgent. (Book I Part IV Article VII; Book II Ch IV.)

Sentinel reasons about its acquisition STRATEGY via `solve("news_path", ...)`:
  * wire_priority  key-free financial wires (RSS) + GDELT: fast, broad, free.
  * premium_api    NewsAPI when a key is present: richer, more recent.
  * broad_sweep    everything incl. practitioner signal: maximum corroboration.

It learns which path yields credible, corroborated intelligence for which kind
of query. Real credibility scoring, misinformation flagging, event clustering,
and credibility-weighted sentiment throughout.
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
        def get_status(self): return {"name": getattr(self, "name", "sentinel")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("sentinel")

PATH_SOURCES = {
    "wire_priority": ["rss", "gdelt"],
    "premium_api": ["newsapi", "rss"],
    "broad_sweep": ["rss", "newsapi", "gdelt", "hackernews"],
}


class SentinelAgent(BaseAgent):
    name = "sentinel"
    repository = "Sentinel"
    domain = "news"
    description = "The institutional news intelligence desk."
    capabilities = ["news.collect", "news.report", "news.sentiment", "news.for_symbol",
                    "news.events", "news.credibility"]
    channels = ["ecosystem.news", "ecosystem.intelligence", "ecosystem.broadcast"]
    memory_namespace = "sentinel_memory"
    security_level = "standard"
    mission = {"purpose": "Acquire, validate, cluster, and distribute credible news intelligence."}

    def __init__(self, chronicle_client=None, **kw):
        super().__init__(chronicle_client=chronicle_client, storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.engine = IntelligenceEngine(chronicle_client=chronicle_client, llm=self.llm)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("news_path", "wire_priority", "_strat_wire",
            reasons_for=["free, fast, credible financial wires"],
            reasons_against=["misses paywalled/very recent items"])
        self.reasoning.register_strategy("news_path", "premium_api", "_strat_premium",
            reasons_for=["richer + more recent (NewsAPI)"],
            reasons_against=["needs NEWSAPI_KEY"])
        self.reasoning.register_strategy("news_path", "broad_sweep", "_strat_broad",
            reasons_for=["max corroboration across all sources"],
            reasons_against=["slower; noisier"])

    def on_start(self) -> None:
        avail = [n for n, ok in self.engine.stats()["collectors"].items() if ok]
        log.info("Sentinel desk online. Available collectors: %s | Brain: %s", avail, self.has_brain)

    # ---- news-path strategy handlers ----

    def _strat_wire(self, c): return self._report(c, "wire_priority")
    def _strat_premium(self, c): return self._report(c, "premium_api")
    def _strat_broad(self, c): return self._report(c, "broad_sweep")

    def _report(self, context, path) -> Dict[str, Any]:
        out = self.engine.report(topics=context.get("topics"), sources=PATH_SOURCES[path])
        ok = out.get("report") is not None and out["report"]["article_count"] > 0
        return {"status": "complete" if ok else "error",
               "message": "" if ok else "no credible news via this path",
               "report": out.get("report"), "path": path, "source_status": out.get("source_status")}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        if task in ("news.report", "news.collect", "news.events"):
            if self.reasoning is not None:
                solved = self.solve("news_path", {"topics": ctx.get("topics")})
                if solved.get("status") == "complete":
                    return {"status": "complete", "report": solved.get("report"),
                           "news_path": solved.get("path")}
                return {"status": "complete", **self.engine.report(
                    topics=ctx.get("topics"), sources=PATH_SOURCES["broad_sweep"]),
                    "note": "fell back to broad sweep"}
            return {"status": "complete", **self.engine.report(topics=ctx.get("topics"))}
        if task in ("news.sentiment", "news.for_symbol"):
            return {"status": "complete", "sentiment": self.engine.sentiment_for(ctx.get("symbol", ""))}
        if task == "news.credibility":
            g = self.engine.gather(topics=ctx.get("topics"))
            return {"status": "complete", "articles": [
                {"title": a["title"], "source": a["source"], "credibility": a["credibility"],
                 "misinformation_risk": a["misinformation_risk"], "misinfo_reasons": a["misinfo_reasons"]}
                for a in g["articles"]]}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["engine"] = self.engine.stats()
        return base

    # in-process convenience for Oracle / Nexus
    def sentiment_for(self, symbol: str):
        return self.engine.sentiment_for(symbol)
