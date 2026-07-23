"""
Pulse.agents.pulse_agent  (Universe-oracle social-upgrade v6)
=============================================================
Multi-category, region-aware social intelligence agent.

Changes vs deep-fix v5
----------------------
  1. `social.report` now accepts `category` in context to filter by category
     (e.g. {"task": "social.report", "category": "Finance"}).
  2. `social.trends` returns trends broken down by category.
  3. `social.regional` new task — returns only Regional category posts
     (Nigerian content when PULSE_USER_REGION=NG).
  4. `get_status()` includes per-category post counts and region.
  5. All LLM mode gate logic from v4/v5 preserved exactly.
  6. REPL commands updated: `report [category] [topics...]`
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
_PULSE_LLM_MODE    = os.getenv("PULSE_LLM_MODE",
                                os.getenv("ORACLE_LLM_MODE", "full")).lower()
_USE_LLM_REASONING = (_PULSE_LLM_MODE != "essential_only")

PATH_SOURCES = {
    "retail_pulse": ["reddit", "stocktwits"],
    "practitioner": ["hackernews", "reddit"],
    "broad_sweep":  ["reddit", "hackernews", "stocktwits",
                     "googletrends", "nairaland"],
}

# Valid category names (case-insensitive lookup)
_VALID_CATEGORIES = {
    "finance", "tech", "technology", "entertainment", "sports",
    "politics", "regional", "general",
}
_CAT_NORMALISE = {
    "technology": "Tech",
    "finance":    "Finance",
    "tech":       "Tech",
    "entertainment": "Entertainment",
    "sports":     "Sports",
    "politics":   "Politics",
    "regional":   "Regional",
    "general":    "General",
}


class PulseAgent(BaseAgent):
    name             = "pulse"
    repository       = "Pulse"
    domain           = "social"
    description      = ("Multi-category, region-aware institutional social "
                        "intelligence desk.")
    capabilities     = [
        "social.collect", "social.report", "social.sentiment",
        "social.for_symbol", "social.trends", "social.manipulation",
        "social.regional",   # NEW
    ]
    channels         = ["ecosystem.social", "ecosystem.intelligence",
                        "ecosystem.broadcast"]
    memory_namespace = "pulse_memory"
    security_level   = "standard"
    mission          = {
        "purpose": (
            "Read authentic social sentiment across Finance, Tech, "
            "Entertainment, Sports, Politics, and Regional (Nigerian) "
            "categories; flag manipulation; detect trends."
        )
    }

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
            reasons_for=["max coverage + manipulation cross-check + regional"],
            reasons_against=["slower"])

    def on_start(self) -> None:
        stats = self.engine.stats()
        avail = [n for n, ok in stats["collectors"].items() if ok]
        log.info(
            "Pulse desk online. Platforms: %s | Region: %s | "
            "Brain: %s | LLM mode: %s",
            avail, stats.get("region", "?"), self.has_brain, _PULSE_LLM_MODE,
        )

    def _strat_retail(self, c): return self._report(c, "retail_pulse")
    def _strat_pract(self, c):  return self._report(c, "practitioner")
    def _strat_broad(self, c):  return self._report(c, "broad_sweep")

    def _report(self, context: Dict[str, Any],
                path: str) -> Dict[str, Any]:
        cat_filter = context.get("category")
        out = self.engine.report(
            topics=context.get("topics"),
            sources=PATH_SOURCES[path],
            category_filter=cat_filter,
        )
        rep = out.get("report")
        ok  = rep is not None and rep.get("post_count", 0) > 0
        return {
            "status":        "complete" if ok else "error",
            "message":       "" if ok else "no social signal via this path",
            "report":        rep,
            "path":          path,
            "source_status": out.get("source_status"),
            "fallback_mode": rep.get("fallback_mode") if rep else "no_data",
        }

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context

        # ── social.report / social.collect ────────────────────────────────────
        if task in ("social.report", "social.collect", "social.trends",
                    "social.regional"):

            # Normalise category filter
            cat_filter: Optional[str] = None
            raw_cat = ctx.get("category", "")
            if raw_cat and raw_cat.lower() in _VALID_CATEGORIES:
                cat_filter = _CAT_NORMALISE.get(raw_cat.lower(), raw_cat.title())
            elif task == "social.regional":
                cat_filter = "Regional"

            # ── LLM reasoning path (skipped in essential_only) ────────────────
            if _USE_LLM_REASONING and self.reasoning is not None:
                solved = self.solve(
                    "social_path",
                    {"topics": ctx.get("topics"), "category": cat_filter},
                )
                if solved.get("status") == "complete":
                    rep = solved.get("report") or {}
                    if task == "social.trends":
                        return {
                            "status":        "complete",
                            "trends":        rep.get("trending", []),
                            "by_category":   _trends_by_category(rep),
                            "fallback_mode": solved.get("fallback_mode",
                                                        "llm_reasoning"),
                        }
                    return {
                        "status":        "complete",
                        "report":        rep,
                        "social_path":   solved.get("path"),
                        "fallback_mode": solved.get("fallback_mode",
                                                    "llm_reasoning"),
                    }
                log.info(
                    "Pulse: LLM reasoning failed (%s), "
                    "falling back to broad_sweep rule-based path",
                    solved.get("message", "unknown"),
                )

            # ── Rule-based broad sweep (always available) ─────────────────────
            out = self.engine.report(
                topics=ctx.get("topics"),
                category_filter=cat_filter,
            )
            rep = out.get("report")

            if task == "social.trends":
                return {
                    "status":        "complete",
                    "trends":        rep.get("trending", []) if rep else [],
                    "by_category":   _trends_by_category(rep) if rep else {},
                    "region":        rep.get("region") if rep else None,
                    "fallback_mode": (rep.get("fallback_mode", "rule_based")
                                     if rep else "no_data"),
                    "note":          out.get("note", ""),
                }

            return {
                "status":        "complete",
                "report":        rep,
                "fallback_mode": (rep.get("fallback_mode", "rule_based")
                                  if rep else "no_data"),
                "note":          out.get("note", ""),
                **{k: v for k, v in out.items()
                   if k not in ("status", "report", "note")},
            }

        # ── social.sentiment / social.for_symbol ──────────────────────────────
        if task in ("social.sentiment", "social.for_symbol"):
            result = self.engine.sentiment_for(ctx.get("symbol", ""))
            return {"status": "complete", "sentiment": result}

        # ── social.manipulation ───────────────────────────────────────────────
        if task == "social.manipulation":
            g = self.engine.gather(topics=ctx.get("topics"))
            try:
                from intelligence.authenticity import detect_manipulation  # type: ignore
            except ImportError:
                from Pulse.intelligence.authenticity import detect_manipulation  # type: ignore
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


# ─────────────────────────────────────────────────────────────────────────────
# Helper: extract per-category trends from a report dict
# ─────────────────────────────────────────────────────────────────────────────
def _trends_by_category(report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build a category → trending_topics map from a report's categories block.
    Used by the `trends` command to show what's trending in each category.
    """
    if not report:
        return {}
    cats = report.get("categories", {})
    result: Dict[str, Any] = {}
    for cat, data in cats.items():
        top = data.get("top_posts", [])
        if top:
            result[cat] = {
                "mood":      data.get("mood", "neutral"),
                "sentiment": data.get("overall_sentiment", 0.0),
                "top":       [p["title"] for p in top[:3]],
            }
    return result
