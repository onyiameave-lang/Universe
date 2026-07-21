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
import socket as _socket
import sys
import time
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

# FIX-SA-01 (Phase 5e): Nuclear socket timeout at module level.
# Ensures DNS resolution is bounded even if collectors.py is imported
# before its own module-level setdefaulttimeout runs.
# Constitutional: Book II Principle V Graceful Degradation.
_socket.setdefaulttimeout(15)

log = logging.getLogger(__name__)

# FIX-SA-04 (Phase 5h): PATH_SOURCES referenced "gdelt" which was renamed to
# "guardian" in collectors.py (fix S-2). "gdelt" is silently skipped by
# CollectorRegistry.collect() because it's not in self.collectors — meaning
# wire_priority and broad_sweep never actually ran Guardian.
# Fixed: all three paths now use "guardian" (the registered collector name).
PATH_SOURCES = {
    "wire_priority": ["rss", "guardian"],
    "premium_api":   ["newsapi", "rss", "guardian"],
    "broad_sweep":   ["rss", "newsapi", "guardian", "hackernews"],
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
        # FIX-SA-02 (Phase 5e): Log before/after engine.report() so we can
        # see exactly where Sentinel hangs in production logs.
        # FIX-SA-03: Wrap engine.report() in a thread with 25s timeout so
        # Sentinel itself never hangs its caller (coordinator has 30s outer).
        import concurrent.futures as _cf
        topics = context.get("topics")
        log.info("[sentinel] _report: path='%s' topics=%r — calling engine.report()", path, topics)
        _t0 = time.time()
        def _do_report():
            _socket.setdefaulttimeout(15)
            return self.engine.report(topics=topics, sources=PATH_SOURCES[path])
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(_do_report)
                out = _fut.result(timeout=25)
            log.info("[sentinel] _report: path='%s' completed in %.2fs — %d articles",
                     path, time.time() - _t0, out.get("report", {}).get("article_count", 0) if out.get("report") else 0)
        except _cf.TimeoutError:
            elapsed = round(time.time() - _t0, 2)
            log.warning("[sentinel] _report: path='%s' TIMED OUT after %.2fs. "
                        "Returning graceful degradation. Constitutional: Book II Principle V.", path, elapsed)
            return {"status": "error", "message": f"news collection timed out after {elapsed}s (path={path})",
                    "path": path, "source_status": {}}
        except Exception as exc:
            log.error("[sentinel] _report: path='%s' raised %s", path, exc)
            return {"status": "error", "message": str(exc), "path": path, "source_status": {}}
        ok = out.get("report") is not None and out["report"]["article_count"] > 0
        return {"status": "complete" if ok else "error",
               "message": "" if ok else "no credible news via this path",
               "report": out.get("report"), "path": path, "source_status": out.get("source_status")}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        log.info("[sentinel] execute: task=%r symbol=%r topics=%r query=%r",
                 task, ctx.get("symbol"), ctx.get("topics"), ctx.get("query", "")[:60])
        if task in ("news.report", "news.collect", "news.events"):
            if self.reasoning is not None:
                solved = self.solve("news_path", {"topics": ctx.get("topics")})
                if solved.get("status") == "complete":
                    report = solved.get("report") or {}
                    return {
                        "status": "complete",
                        "report": report,
                        "news_path": solved.get("path"),
                        "summary": report.get("summary", "") if isinstance(report, dict) else "",  # FIX-SA-07
                    }
                broad = self.engine.report(topics=ctx.get("topics"), sources=PATH_SOURCES["broad_sweep"])
                report = broad.get("report") or {}
                return {
                    "status": "complete",
                    **broad,
                    "note": "fell back to broad sweep",
                    "summary": report.get("summary", "") if isinstance(report, dict) else "",  # FIX-SA-07
                }
            broad = self.engine.report(topics=ctx.get("topics"))
            report = broad.get("report") or {}
            return {
                "status": "complete",
                **broad,
                "summary": report.get("summary", "") if isinstance(report, dict) else "",  # FIX-SA-07
            }
        if task in ("news.sentiment", "news.for_symbol"):
            # FIX-SA-05 (Phase 5h): Coordinator now passes both ctx["symbol"] and
            # ctx["topics"]. Prefer topics (already a list) so collectors get the
            # right filter. Fall back to [symbol] if topics is absent/empty.
            symbol = ctx.get("symbol", "")
            topics = ctx.get("topics") or ([symbol] if symbol else None)
            log.info("[sentinel] execute: task=%r — effective symbol=%r topics=%r", task, symbol, topics)
            _t0 = time.time()
            result = self.engine.sentiment_for(symbol, topics=topics)
            log.info("[sentinel] execute: sentiment_for(%r) completed in %.2fs — article_count=%d",
                     symbol, time.time() - _t0, result.get("article_count", 0))
            # FIX-SA-07 (Phase 5i): Bubble up the plain-text 'summary' from the
            # engine result so coordinator._format_result() and main.py's
            # _extract_summary() can surface it without parsing nested dicts.
            return {
                "status": "complete",
                "sentiment": result,
                "summary": result.get("summary", ""),   # FIX-SA-07
                "symbol": symbol,
            }
        if task == "news.credibility":
            log.info("[sentinel] execute: task='news.credibility' topics=%r — calling engine.gather()", ctx.get("topics"))
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