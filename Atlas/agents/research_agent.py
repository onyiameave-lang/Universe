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

FIX LOG (Phase 2):
  FIX-P2-13: _run() success check fixed — the original check required BOTH
              confidence >= target AND evidence present. This caused every
              query to return status="error" when external sources were
              rate-limited (zero evidence), even when the LLM fallback
              produced a valid answer. New check: status="complete" whenever
              the report has a non-empty summary (covers llm_only fallback,
              partial evidence, and full evidence). (Book II Graceful
              Degradation; Book IV No Silent Failures.)
  FIX-P2-14: execute() no longer triggers a second broad-desk sweep when
              solve() is exhausted. Instead it returns the best available
              report from the reasoning trace. This prevents the "no approach
              succeeded in 3 attempts" cascade from repeating indefinitely.
              (Book IV resilience — exhausted strategies return best effort,
              not another doomed attempt.)
  FIX-P2-15: Chronicle integration hooks added — Atlas sends completed
              research results to Chronicle and retrieves prior research
              before starting a new investigation. (Book II Memory First;
              Book II Everything Communicates; Chronicle as source of truth.)
"""
from __future__ import annotations

import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
        summary = report.get("summary", "")
        source_summary = report.get("source_status", {}).get("_summary", "")

        # FIX-P2-13: Accept the report as "complete" when:
        #   (a) summary is non-empty and not the sentinel failure string, OR
        #   (b) source_status._summary is "llm_only" (LLM fallback succeeded), OR
        #   (c) confidence >= target with any evidence present.
        # The original check (confidence >= target AND evidence) caused every
        # rate-limited query to return status="error" even when the LLM answered.
        # (Book II Graceful Degradation — partial answers are better than errors.)
        has_answer = bool(summary and summary != "Insufficient evidence to synthesize.")
        llm_only = source_summary == "llm_only"
        above_target = (report["confidence"] >= self.engine.confidence_target
                        and bool(report["evidence"]))
        ok = has_answer and (above_target or llm_only or bool(report.get("evidence")))

        # FIX-P2-15: Send completed research to Chronicle (Everything Communicates).
        if ok:
            self._send_to_chronicle(
                content=summary,
                memory_type="semantic",
                domain=context.get("domain", "general"),
                tags=["atlas", "research", path] + report.get("key_terms", [])[:3],
            )

        return {"status": "complete" if ok else "error",
               "message": "" if ok else "below confidence target via this path",
               "report": report, "path": path}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        if task == "research.investigate":
            # FIX-P2-15: Memory First — retrieve prior research from Chronicle
            # before starting a new investigation.
            prior = self._receive_from_chronicle(
                query=ctx.get("query", ""),
                domain=ctx.get("domain", "general"),
            )
            if prior:
                log.info("atlas: Chronicle returned %d prior memories for '%s'",
                         len(prior), ctx.get("query", "")[:60])

            query = ctx.get("query", "")
            domain = ctx.get("domain", "general")
            if self._is_explanatory_query(query) and not self._needs_fresh_evidence(query):
                fallback_report = self._best_effort_report(query, domain)
                self._remember_best_effort(fallback_report, domain)
                return {
                    "status": "complete",
                    "report": fallback_report,
                    "research_path": "memory_or_llm_explanation",
                    "degraded": fallback_report.get("source_status", {}).get("_summary") != "chronicle_memory",
                }

            if self.reasoning is not None:
                solved = self.solve("research_path", {"query": ctx.get("query", ""),
                    "domain": ctx.get("domain", "general"), "depth": ctx.get("depth", "standard")})
                if solved.get("status") == "complete":
                    return {"status": "complete", "report": solved.get("report"),
                           "research_path": solved.get("path")}

                # FIX-P2-14: solve() exhausted — return best available report
                # from the reasoning trace instead of triggering another doomed
                # broad-desk sweep. (Book IV resilience.)
                trace = solved.get("trace", [])
                best_report = None
                for step in reversed(trace):
                    candidate = step.get("report") if isinstance(step, dict) else None
                    if candidate and candidate.get("summary") and \
                       candidate["summary"] != "Insufficient evidence to synthesize.":
                        best_report = candidate
                        break
                if best_report:
                    log.info("atlas: all paths below target — returning best-effort report "
                             "from trace (conf=%.2f). (FIX-P2-14)", best_report.get("confidence", 0))
                    return {"status": "complete", "report": best_report,
                            "note": "all paths below target; returned best-effort from trace"}

                # FIX-AGT-V6-01 (2026-07-22): REMOVED broad-desk sweep fallback.
                # ROOT CAUSE: When solve() exhausted all 3 strategies AND the trace had
                # no usable report, this code fired a FOURTH engine.investigate() call
                # with ALL 5 sources (broad_desk). This was the 37-second second gap
                # visible in the logs: "no usable trace report — falling back to broad-desk sweep"
                # followed by 37s of silence before the Gemini call.
                # The broad-desk sweep is ALWAYS doomed when the 3 strategy paths already
                # failed — if wikipedia/arxiv/semantic_scholar couldn't answer in 3 attempts,
                # querying all 5 sources won't help. It just wastes 37 more seconds.
                # FIX: Return a graceful degradation response immediately. The LLM-only
                # fallback in research_engine._llm_only_answer() already handles the case
                # where all external sources fail. If that also failed, we return an honest
                # "insufficient evidence" response rather than burning 37 more seconds.
                # Constitutional law: Book II Principle V Graceful Degradation — return
                # partial results immediately rather than retrying a doomed path.
                # Book IV No Silent Failures — log the failure clearly.
                log.warning(
                    "atlas: no usable trace report after %d strategies — returning graceful degradation. "
                    "FIX-AGT-V6-01: broad-desk sweep removed (was adding 37s with no benefit). "
                    "Constitutional: Book II Principle V Graceful Degradation.",
                    3,
                )
                fallback_report = self._best_effort_report(ctx.get("query", ""), ctx.get("domain", "general"))
                self._remember_best_effort(fallback_report, ctx.get("domain", "general"))
                return {
                    "status": "complete",
                    "report": fallback_report,
                    "note": "all paths below target; graceful degradation (FIX-AGT-V6-01)",
                    "degraded": True,
                }

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

    def _best_effort_report(self, query: str, domain: str = "general") -> Dict[str, Any]:
        """Return the best non-hardcoded answer available when live research fails."""
        cleaned = (query or "").strip()
        answer, source, confidence = self._best_effort_answer(cleaned, domain)
        return {
            "query": cleaned,
            "domain": domain,
            "summary": answer,
            "confidence": confidence,
            "findings": [
                "Live research paths were exhausted or unavailable.",
                f"Atlas used {source} instead of a hardcoded topic response.",
                "This degraded answer should be replaced by corroborated sources when connectivity or API limits recover.",
            ],
            "limitations": [
                "No fresh external evidence was gathered for this response.",
                "Treat this as degraded output unless Chronicle or LLM evidence is shown.",
            ],
            "evidence": [],
            "key_terms": re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", cleaned.lower())[:8],
            "source_status": {"_summary": source},
        }

    def _best_effort_answer(self, query: str, domain: str) -> tuple[str, str, float]:
        memory_answer = self._chronicle_best_effort(query, domain)
        if memory_answer:
            return memory_answer, "chronicle_memory", 0.35

        llm_answer = self._llm_best_effort(query, domain)
        if llm_answer:
            return llm_answer, "llm_best_effort", 0.45

        return (
            f"Atlas could not gather fresh evidence or a relevant learned memory for: '{query}'. "
            "I will not invent a research answer. The failed path has been recorded so Atlas can improve its source "
            "selection and retry strategy before answering this topic with confidence.",
            "insufficient_evidence_recorded",
            0.1,
        )

    def _llm_best_effort(self, query: str, domain: str) -> Optional[str]:
        if not getattr(self, "llm", None) or not getattr(self.llm, "has_any", False):
            return None
        try:
            from shared.llm import system_prompt  # type: ignore
            prompt = (
                "Live research collectors failed. Answer the user's question directly from model knowledge only. "
                "Do not claim fresh citations or live source verification. Keep it concise, useful, and mark uncertainty.\n\n"
                f"Domain: {domain}\nQuestion: {query}"
            )
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(
                self.llm.complete,
                system_prompt("atlas"),
                prompt,
                0.2,
                450,
                None,
                True,
            )
            try:
                result = future.result(timeout=30)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            text = getattr(result, "text", "") if getattr(result, "ok", False) else ""
            return text.strip() if text and len(text.strip()) > 20 else None
        except FuturesTimeoutError:
            log.warning("atlas: LLM best-effort answer timed out for query=%r", query[:80])
            return None
        except Exception as exc:
            log.warning("atlas: LLM best-effort answer unavailable: %s", exc)
            return None

    def _chronicle_best_effort(self, query: str, domain: str) -> Optional[str]:
        memories = self._receive_from_chronicle(query=query, domain=domain, limit=12) or []
        if not memories and domain and domain != "general":
            memories = self._receive_from_chronicle(query=query, domain="general", limit=12) or []
        query_terms = {
            term for term in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", (query or "").lower())
            if term not in {
                "the", "and", "for", "with", "what", "are", "how", "why", "when",
                "where", "who", "explain", "research", "about", "from", "that",
                "this", "your", "into", "give", "tell",
            }
        }
        parts: List[str] = []
        for memory in memories:
            if isinstance(memory, dict):
                if memory.get("pillar") in {"evolutionary", "operational", "strategy"}:
                    continue
                text = memory.get("content") or memory.get("summary") or memory.get("answer") or ""
            else:
                text = str(memory)
            text = " ".join(str(text).split())
            lowered = text.lower()
            if any(marker in lowered for marker in (
                "could not gather fresh",
                "live research paths were exhausted",
                "nexus routed query",
                "chronicle fast-path hit",
                "specialist agent unavailable",
                "strategy '" ,
                "experiencing delays",
                "no approach succeeded",
            )):
                continue
            memory_terms = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower()))
            if query_terms and not (query_terms & memory_terms):
                continue
            if len(text) > 20:
                parts.append(text[:700])
        if not parts:
            return None
        joined = "\n\n".join(parts[:3])
        return (
            "Atlas could not gather fresh sources, so it is answering from relevant Chronicle memory "
            f"(may be incomplete or outdated):\n\n{joined}"
        )

    def _is_explanatory_query(self, query: str) -> bool:
        q = (query or "").lower()
        return bool(re.search(r"\b(explain|what is|what are|define|tell me about|how does|how do)\b", q))

    def _needs_fresh_evidence(self, query: str) -> bool:
        q = (query or "").lower()
        return bool(re.search(
            r"\b(latest|current|today|now|recent|news|source|sources|citation|citations|cite|"
            r"paper|papers|study|studies|price|market|trend|trending|breaking)\b",
            q,
        ))

    def _remember_best_effort(self, report: Dict[str, Any], domain: str) -> None:
        source = report.get("source_status", {}).get("_summary", "")
        if source == "insufficient_evidence_recorded":
            memory_type = "operational"
            tags = ["atlas", "failed_retrieval", "needs_retry"]
        else:
            memory_type = "semantic"
            tags = ["atlas", "best_effort", source]
        self._send_to_chronicle(
            content=report.get("summary", ""),
            memory_type=memory_type,
            domain=domain,
            tags=tags + report.get("key_terms", [])[:3],
        )
