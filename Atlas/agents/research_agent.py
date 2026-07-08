"""
Atlas.agents.research_agent
==========================
Atlas (formerly Research AI): an institutional research desk, on the
constitutional BaseAgent. (Book I Part IV Article VII; Book II Ch IV.)

Atlas reasons about its research APPROACH via `solve("research_path", ...)`,
now with institutional strategies that trade breadth, rigor, and speed:
  * academic_rigor   - peer-reviewed first (Semantic Scholar/PubMed/Crossref),
                       for scientific/medical/technical questions.
  * frontier_scan    - preprints + practitioner signal (arXiv/HN), for the
                       cutting edge before peer review.
  * market_pulse     - news + practitioner (GDELT/HN/wiki), for fast-moving,
                       real-world/financial questions.
  * broad_desk       - everything, maximum corroboration, for hard questions.

It learns which approach yields high-confidence, well-corroborated reports for
which kinds of query. If a path underperforms the confidence target, the engine
auto-escalates depth and Atlas can retry a different path.
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

from core.research_engine import ResearchEngine  # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "atlas")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("atlas")

PATH_SOURCES = {
    "academic_rigor": ["semantic_scholar", "pubmed", "crossref"],
    "frontier_scan": ["arxiv", "hackernews", "semantic_scholar"],
    "market_pulse": ["gdelt", "hackernews", "wikipedia"],
    "broad_desk": ["semantic_scholar", "arxiv", "wikipedia", "gdelt", "hackernews"],
}


class AtlasAgent(BaseAgent):
    name = "atlas"
    repository = "Atlas"
    domain = "research"
    description = "The institutional research and knowledge-synthesis desk."
    capabilities = ["research.investigate", "research.validate", "research.synthesize",
                    "research.cite", "hypothesis.generate", "hypothesis.test", "web.fetch"]
    channels = ["ecosystem.research", "ecosystem.knowledge", "ecosystem.broadcast"]
    memory_namespace = "atlas_memory"
    security_level = "standard"
    mission = {"purpose": "Investigate with multi-source rigor; corroborate; surface disagreement."}

    def __init__(self, chronicle_client=None, **kw):
        super().__init__(chronicle_client=chronicle_client, storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.engine = ResearchEngine(chronicle_client=chronicle_client, llm=self.llm)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("research_path", "academic_rigor", "_strat_academic",
            reasons_for=["peer-reviewed; highest credibility"],
            reasons_against=["slow; misses very recent work"])
        self.reasoning.register_strategy("research_path", "frontier_scan", "_strat_frontier",
            reasons_for=["catches cutting-edge preprints + practitioner signal"],
            reasons_against=["less validated than peer review"])
        self.reasoning.register_strategy("research_path", "market_pulse", "_strat_market",
            reasons_for=["fast; real-world/news coverage"],
            reasons_against=["lower credibility; noisier"])
        self.reasoning.register_strategy("research_path", "broad_desk", "_strat_broad",
            reasons_for=["maximum corroboration across all sources"],
            reasons_against=["slowest; most requests"])

    def on_start(self) -> None:
        log.info("Atlas desk online. Multi-source, corroboration-aware. Brain: %s", self.has_brain)

    # ---- research-path strategy handlers ----

    def _strat_academic(self, ctx): return self._run(ctx, "academic_rigor")
    def _strat_frontier(self, ctx): return self._run(ctx, "frontier_scan")
    def _strat_market(self, ctx): return self._run(ctx, "market_pulse")
    def _strat_broad(self, ctx): return self._run(ctx, "broad_desk")

    def _run(self, context: Dict[str, Any], path: str) -> Dict[str, Any]:
        report = self.engine.investigate(query=context.get("query", ""),
                                        domain=context.get("domain", "general"),
                                        depth=context.get("depth", "standard"),
                                        sources=PATH_SOURCES[path])
        ok = report["confidence"] >= self.engine.confidence_target and bool(report["evidence"])
        return {"status": "complete" if ok else "error",
               "message": "" if ok else "below confidence target via this path",
               "report": report, "path": path}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        if task == "research.investigate":
            if self.reasoning is not None:
                solved = self.solve("research_path", {"query": ctx.get("query", ""),
                    "domain": ctx.get("domain", "general"), "depth": ctx.get("depth", "standard")})
                if solved.get("status") == "complete":
                    return {"status": "complete", "report": solved.get("report"),
                           "research_path": solved.get("path")}
                # exhausted: return best-effort broad-desk result honestly
                return {"status": "complete", "report": self.engine.investigate(
                    ctx.get("query", ""), ctx.get("domain", "general"),
                    sources=PATH_SOURCES["broad_desk"]),
                    "note": "all paths below target; returned best-effort broad sweep"}
            return {"status": "complete", "report": self.engine.investigate(
                ctx.get("query", ""), ctx.get("domain", "general"), ctx.get("depth", "standard"))}
        if task == "hypothesis.generate":
            return {"status": "complete", "hypothesis": self.engine.generate_hypothesis(
                ctx.get("statement", ""), ctx.get("domain", "general"))}
        if task in ("hypothesis.test", "research.validate"):
            return {"status": "complete", "hypothesis": self.engine.validate_hypothesis(
                ctx.get("hypothesis_id", ""))}
        if task == "research.synthesize":
            return {"status": "complete", "synthesis": self.engine.synthesize(
                ctx.get("topics", []), ctx.get("domain", "general"))}
        if task in ("web.fetch", "research.cite"):
            return self.engine.fetch_and_analyze(ctx.get("url", ""), ctx.get("query", ""))
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["engine"] = self.engine.stats()
        return base

    def investigate(self, query: str, domain: str = "general", depth: str = "standard"):
        return self.engine.investigate(query, domain, depth)
