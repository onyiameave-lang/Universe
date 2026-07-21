"""
Chronicle.agents.chronicle_agent
================================
Chronicle (formerly AI Memory System): an institutional, self-correcting
knowledge base, on the constitutional BaseAgent. (Book II Part III.)

Institutional capabilities:
  * Store / retrieve with real embeddings + five-stage retrieval (exact).
  * ANTICIPATION: predicts what each repository needs next and warms it.
  * CONSOLIDATION: merges duplicates, distills clusters into key points, prunes.
  * SEMANTIC CONTRADICTION + BELIEF REVISION: finds genuine conflicts, refers
    them to Atlas for an evidence verdict, supersedes the weaker belief
    (archive-not-delete), reinforces the stronger. Self-correcting.
  * PROVENANCE: full auditable lineage for any belief.
  * TIERED LIFECYCLE: hot/warm/cold tiers keep retrieval fast at scale.
  * ACTIVE IMPROVEMENT: studies strategy fits, proposes better versions to Atlas.

Deterministic ops run directly; judgment calls (which belief wins, whether an
improvement is worth adopting) run through reasoning + Atlas evidence.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# FIX-CH-01 (Phase 5f): Set socket timeout at module level to bound DNS resolution
# and socket operations. This is a nuclear option that protects against hangs in
# embedding services, external APIs, and any network calls made by Chronicle.
# Constitutional law: Book II Principle V Graceful Degradation.
import socket
socket.setdefaulttimeout(8)

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

from core.embeddings import get_embedding_model                              # type: ignore
from core.memory_record import MemoryRecord, MemoryPillar, validate_memory   # type: ignore
from core.vector_store import VectorStore                                    # type: ignore
from core.knowledge_graph import KnowledgeGraph                              # type: ignore
from intelligence.retrieval import RetrievalEngine                           # type: ignore
from intelligence.anticipation import AnticipationEngine                     # type: ignore
from intelligence.consolidation import ConsolidationEngine                   # type: ignore
from intelligence.improvement import ImprovementEngine                       # type: ignore
from intelligence.contradiction import ContradictionEngine                   # type: ignore
from intelligence.provenance import ProvenanceEngine                         # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "chronicle")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def think(self, *a, **kw): return None
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False


log = logging.getLogger("chronicle")


class ChronicleAgent(BaseAgent):
    name = "chronicle"
    repository = "Chronicle"
    domain = "memory"
    description = "Institutional, self-correcting memory and knowledge base."
    capabilities = ["memory.store", "memory.retrieve", "memory.search", "memory.answer",
                    "memory.evolve", "memory.validate", "knowledge_graph.connect",
                    "knowledge_graph.query", "contradiction.detect", "contradiction.adjudicate",
                    "belief.revise", "provenance.trace", "memory.rebalance",
                    "memory.anticipate", "memory.warm", "memory.consolidate",
                    "strategy.record_fit", "strategy.profile", "strategy.improve"]
    channels = ["ecosystem.memory", "ecosystem.knowledge", "ecosystem.broadcast"]
    memory_namespace = "chronicle_memory"
    security_level = "critical"
    mission = {"purpose": "Preserve, anticipate, reconcile, and evolve the ecosystem's knowledge."}

    def __init__(self, storage_dir: str = "memory_store", atlas_client: Any = None, **kw):
        super().__init__(chronicle_client=None, atlas_client=atlas_client,
                        storage_dir=storage_dir, **kw)
        self.embedder = get_embedding_model()
        self.store = VectorStore(storage_dir=storage_dir)
        self.graph = KnowledgeGraph(storage_dir=storage_dir)
        self.retrieval = RetrievalEngine(self.store, self.graph)
        self.anticipation = AnticipationEngine(self.store, storage_dir=storage_dir)
        self.consolidation = ConsolidationEngine(self.store, self.graph, llm=self.llm)
        self.improvement = ImprovementEngine(self.store, self.graph, reasoning=self.reasoning,
                                            atlas=atlas_client, chronicle_agent=self)
        self.contradiction = ContradictionEngine(self.store, self.graph, atlas=atlas_client,
                                                chronicle_agent=self)
        self.provenance = ProvenanceEngine(self.store, self.graph)

    def register_strategies(self) -> None:
        if self.reasoning is None:
            return
        self.reasoning.register_strategy("knowledge_improvement", "research_validate",
            "_strat_research_validate", reasons_for=["evidence-backed via Atlas"],
            reasons_against=["slower; needs Atlas"])
        self.reasoning.register_strategy("knowledge_improvement", "usage_reinforce",
            "_strat_usage_reinforce", reasons_for=["fast; uses real usage stats"],
            reasons_against=["can entrench a locally-good pattern"])

    def on_start(self) -> None:
        log.info("Chronicle institutional memory online: embeddings=%s, records=%d, atlas=%s",
                 self.embedder.backend, self.store.stats()["active"], self.atlas is not None)

    # ---- store / retrieve (deterministic) ----

    def store_memory(self, content: Any, pillar: str = "semantic", domain: str = "general",
                    summary: str = "", source_repository: str = "unknown",
                    source_agent: str = "", evidence: Optional[List[str]] = None,
                    lesson: str = "", tags: Optional[List[str]] = None,
                    autolink: bool = True) -> Dict[str, Any]:
        try:
            pillar_enum = MemoryPillar(pillar)
        except ValueError:
            pillar_enum = MemoryPillar.SEMANTIC
        text = content if isinstance(content, str) else str(content)
        if not summary and self.has_brain and len(text) > 300 and False:
            advice = self.think(f"Summarize in one sentence for a memory index:\n\n{text[:1500]}",
                               temperature=0.2, max_tokens=80)
            summary = advice.strip() if advice else text[:160]
        summary = summary or text[:160]
        record = MemoryRecord(pillar=pillar_enum, domain=domain, content=content, summary=summary,
                             embedding=self.embedder.encode(summary or text),
                             source_repository=source_repository, source_agent=source_agent,
                             evidence=evidence or [], lesson=lesson, tags=tags or [])
        self.store.add(record)
        if autolink:
            self._autolink(record)
        return {"status": "complete", "memory_id": record.memory_id,
                "confidence": record.confidence, "summary": summary,
                "validation": validate_memory(record)}

    def _autolink(self, record: MemoryRecord) -> None:
        try:
            for rec, sim in self.store.search(record.embedding, top_k=3, domain=record.domain):
                if rec.memory_id != record.memory_id and sim > 0.6:
                    self.graph.connect(record.memory_id, rec.memory_id, "related", weight=sim)
        except Exception:
            log.debug("autolink skipped")

    def _retrieve(self, query: str, requester: str, domain: Optional[str], top_k: int) -> Dict[str, Any]:
        result = self.retrieval.retrieve(query=query, requester=requester, domain=domain, top_k=top_k)
        self.anticipation.observe(requester, query, domain, [r["memory_id"] for r in result["results"]])
        return result

    def answer(self, query: str, requester: str = "unknown",
               domain: Optional[str] = None, top_k: int = 5) -> Dict[str, Any]:
        # FIX-CH-03 (Phase 5g): Set socket timeout at the start of answer() as a
        # defense-in-depth measure. socket.setdefaulttimeout(8) is already set at
        # module level and in execute(), but re-setting here ensures it's active
        # even if called directly (e.g. from tests or other agents).
        import socket as _sock
        _sock.setdefaulttimeout(5)

        import time as _time
        _t0 = _time.monotonic()
        log.debug("[chronicle] answer: starting retrieval for query=%r requester=%s", query, requester)

        retrieval = self._retrieve(query, requester, domain, top_k)
        memories = retrieval["results"]

        log.debug("[chronicle] answer: retrieval returned %d memories in %.2fs for query=%r",
                  len(memories), _time.monotonic() - _t0, query)

        if not memories:
            return {"status": "complete", "answer": None, "memories": [], "grounded": False,
                   "note": "No relevant memories; generation advised elsewhere."}
        if self.has_brain:
            ctx = "\n".join(f"- [{m['memory_id']}] {m['summary']}" for m in memories)
            # FIX-CH-04 (Phase 5g): Wrap self.think() in a ThreadPoolExecutor with 6s timeout.
            # self.think() makes an HTTP request to the LLM API. If the LLM is slow or
            # unreachable, this call can block for 30-60s even with socket.setdefaulttimeout()
            # set — because the socket is already CONNECTED (DNS resolved, TCP established)
            # and the timeout only applies to the READ phase, not to a slow streaming response.
            # The ThreadPoolExecutor timeout is the only reliable bound on this call.
            from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _TE
            advice = None
            try:
                with _TPE(max_workers=1) as _pool:
                    _fut = _pool.submit(
                        self.think,
                        f"Question: {query}\n\nMemories:\n{ctx}\n\nAnswer using ONLY these, "
                        f"cite ids, note gaps.",
                        temperature=0.2,
                        max_tokens=400,
                    )
                    advice = _fut.result(timeout=6)
                    log.debug("[chronicle] answer: LLM think() returned in %.2fs for query=%r",
                              _time.monotonic() - _t0, query)
            except _TE:
                log.warning(
                    "[chronicle] answer: LLM think() timed out after 6s for query=%r — "
                    "falling back to extractive answer. "
                    "Constitutional: Book II Principle V Graceful Degradation.",
                    query,
                )
                advice = None
            except Exception as _exc:
                log.warning("[chronicle] answer: LLM think() failed (%s) — falling back to extractive.", _exc)
                advice = None
            if advice:
                return {"status": "complete", "answer": advice.strip(), "memories": memories, "grounded": True}
        return {"status": "complete", "answer": " ".join(m["summary"] for m in memories[:3]),
               "memories": memories, "grounded": True, "provider": "extractive"}

    # ---- reasoning strategy handlers ----

    def _strat_research_validate(self, context: Dict[str, Any]) -> Dict[str, Any]:
        out = self.improvement.propose_improvement(context.get("strategy", ""),
                                                  context.get("domain", "general"))
        ok = out.get("supported", False)
        return {"status": "complete" if ok else "error", "message": out.get("verdict", ""), **out}

    def _strat_usage_reinforce(self, context: Dict[str, Any]) -> Dict[str, Any]:
        profile = self.improvement.fit_profile(context.get("strategy", ""), context.get("domain", "general"))
        ok = bool(profile.get("succeeds_when"))
        return {"status": "complete" if ok else "error",
               "message": "reinforced from usage" if ok else "no usage pattern", "profile": profile}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        # FIX-CH-01 (Phase 5f): Re-set socket timeout at start of execute() to ensure
        # it's active even if other code has changed it. This is a defense-in-depth measure.
        socket.setdefaulttimeout(8)
        
        ctx = context
        sender = ctx.get("_sender", "unknown")
        
        # FIX-CH-02 (Phase 5f): Debug logging for observability
        if task == "memory.answer":
            log.info("[chronicle] memory.answer: searching for query=%r from %s", ctx.get("query", ""), sender)

        if task == "memory.store":
            return self.store_memory(content=ctx.get("content", ""), pillar=ctx.get("pillar", "semantic"),
                                    domain=ctx.get("domain", "general"), summary=ctx.get("summary", ""),
                                    source_repository=sender, evidence=ctx.get("evidence"),
                                    lesson=ctx.get("lesson", ""), tags=ctx.get("tags"))
        if task in ("memory.retrieve", "memory.search"):
            return {"status": "complete", **self._retrieve(ctx.get("query", ""), sender,
                                                           ctx.get("domain"), ctx.get("limit", 5))}
        if task == "memory.answer":
            return self.answer(ctx.get("query", ""), sender, ctx.get("domain"), ctx.get("limit", 5))
        if task == "contradiction.detect":
            return {"status": "complete", **self.contradiction.scan(
                domain=ctx.get("domain"), auto_revise=ctx.get("auto_revise", False))}
        if task in ("contradiction.adjudicate", "belief.revise"):
            return {"status": "complete", "revision": self.contradiction.adjudicate(
                ctx.get("memory_a", ""), ctx.get("memory_b", ""))}
        if task == "provenance.trace":
            return {"status": "complete", "provenance": self.provenance.trace(ctx.get("memory_id", ""))}
        if task == "memory.rebalance":
            return {"status": "complete", **self.provenance.rebalance(domain=ctx.get("domain"))}
        if task == "memory.anticipate":
            return {"status": "complete", **self.anticipation.predict(ctx.get("repository", sender),
                                                                     ctx.get("limit", 5))}
        if task == "memory.warm":
            return {"status": "complete", **self.anticipation.warm(ctx.get("repository", sender))}
        if task == "memory.consolidate":
            return {"status": "complete", **self.consolidation.consolidate(
                domain=ctx.get("domain"), distill=ctx.get("distill", True), prune=ctx.get("prune", True))}
        if task == "strategy.record_fit":
            return {"status": "complete", **self.improvement.record_strategy_fit(
                ctx.get("strategy", ""), ctx.get("domain", "general"), ctx.get("conditions", {}),
                ctx.get("outcome", ""), ctx.get("success", False), ctx.get("reason", ""))}
        if task == "strategy.profile":
            return {"status": "complete", "profile": self.improvement.fit_profile(
                ctx.get("strategy", ""), ctx.get("domain", "general"))}
        if task == "strategy.improve":
            if self.reasoning is not None:
                return self.solve("knowledge_improvement", {"strategy": ctx.get("strategy", ""),
                                                          "domain": ctx.get("domain", "general")})
            return {"status": "complete", **self.improvement.propose_improvement(
                ctx.get("strategy", ""), ctx.get("domain", "general"))}
        if task == "memory.evolve":
            rec = self.store.get(ctx.get("memory_id", ""))
            if not rec:
                return {"status": "error", "message": "not found"}
            if ctx.get("evidence"):
                rec.evidence.extend(ctx["evidence"])
            if ctx.get("verify"):
                rec.verified = True
            self.store.update(rec)
            return {"status": "complete", "confidence": rec.confidence, "version": rec.version}
        if task == "memory.validate":
            rec = self.store.get(ctx.get("memory_id", ""))
            return ({"status": "complete", "validation": validate_memory(rec)} if rec
                   else {"status": "error", "message": "not found"})
        if task == "knowledge_graph.connect":
            return {"status": "complete", "edge": self.graph.connect(
                ctx.get("from_id", ""), ctx.get("to_id", ""),
                ctx.get("relation", "related"), ctx.get("weight", 1.0))}
        if task == "knowledge_graph.query":
            return {"status": "complete", "related": self.graph.related(
                ctx.get("memory_id", ""), max_depth=ctx.get("max_depth", 2))}
        return {"status": "error", "message": f"Unknown task: {task}"}

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base.update({"embedding_backend": self.embedder.backend, "store": self.store.stats(),
                    "graph": self.graph.stats(), "anticipation": self.anticipation.stats(),
                    "improvement": self.improvement.stats(), "contradiction": self.contradiction.stats(),
                    "provenance": self.provenance.stats()})
        return base

    # ---- in-process convenience ----

    def search(self, query: str = "", domain: Optional[str] = None, limit: int = 5, **kw):
        return self._retrieve(query, kw.get("requester", "ecosystem"), domain, limit)["results"]

    def store(self, content, memory_type="semantic", domain="general", tags=None, **kw):  # type: ignore
        return self.store_memory(content=content, pillar=memory_type, domain=domain, tags=tags,
                                source_repository=kw.get("source", "ecosystem"), evidence=kw.get("evidence"))