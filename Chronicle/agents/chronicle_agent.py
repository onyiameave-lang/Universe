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

# FIX-CH-FB-01: process-wide lock + timestamp helper for the relevance-feedback ledger.
import threading as _threading
from datetime import datetime as _dt, timezone as _tz
_FB_LOCK = _threading.Lock()


def _fb_now() -> str:
    return _dt.now(_tz.utc).isoformat()


class ChronicleAgent(BaseAgent):
    name = "chronicle"
    repository = "Chronicle"
    domain = "memory"
    description = "Institutional, self-correcting memory and knowledge base."
    capabilities = ["memory.store", "memory.retrieve", "memory.search", "memory.answer",
                    "memory.feedback",
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
        # FIX-CH-FB-01: let retrieval consult stored negative feedback so a memory
        # already judged irrelevant for a look-alike query is down-weighted.
        try:
            self.retrieval.penalty_fn = self.mismatch_penalty
        except Exception:
            pass
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
        """
        FIX-CH-V3-01: answer() now returns raw memories IMMEDIATELY without calling
        the LLM for synthesis. The previous implementation called self.think() (an
        HTTP request to Gemini/Ollama) with a 6s ThreadPoolExecutor timeout, but the
        log showed Chronicle taking 28-30s -- because the LLM connection was already
        established (TCP connected) and the 6s timeout only bounded the READ phase,
        not a slow streaming response. The ThreadPoolExecutor timeout fired at 6s but
        the underlying thread kept running until the LLM responded at 28s, blocking
        the executor shutdown.

        Chronicle's constitutional role is FAST MEMORY RETRIEVAL, not synthesis.
        Synthesis is Atlas's job (Book II Ch IV Research Before Assumption). Chronicle
        should return its memories in <1s so Atlas can use them as context. The LLM
        synthesis step was a misallocation of Chronicle's 10s SLA budget.

        The returned memories are passed to Atlas's _synthesize() as the
        CHRONICLE MEMORIES stream in the dual-stream prompt, where the LLM decides
        whether each memory is relevant and incorporates it appropriately.
        (Book II Principle I Memory First -- fast retrieval; Book II Principle V
        Graceful Degradation -- Chronicle must stay within its 10s SLA.)
        """
        # FIX-CH-03 (Phase 5g): Set socket timeout at the start of answer() as a
        # defense-in-depth measure.
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

        # FIX-CH-V3-01: Return raw memories immediately. Do NOT call the LLM.
        # The extractive answer (joining summaries) is fast and sufficient for
        # Chronicle's role as a memory provider. Atlas will synthesize from these.
        extractive = " ".join(m["summary"] for m in memories[:3])
        return {"status": "complete", "answer": extractive, "memories": memories,
                "grounded": True, "provider": "extractive",
                "note": "Raw memories returned; synthesis delegated to requesting agent."}

    # ---- relevance feedback: the RL learning loop (Book II Ch VI Memory Evolution) ----

    def apply_relevance_feedback(self, query: str, memory_id: str, relevant: bool,
                                 reason: str = "", source_agent: str = "unknown") -> Dict[str, Any]:
        """
        FIX-CH-FB-01: Receive relevance feedback about a retrieved memory from ANY
        agent (Atlas, Oracle, ...) and USE it to learn, so future retrievals for the
        whole ecosystem improve.

        HOW CHRONICLE ACTUALLY LEARNS (not just logging):

          1. record_use(successful=relevant) updates the memory's usage track record
             (successful_uses / total_uses). This feeds directly into
             MemoryRecord.compute_confidence() (usage_factor, weight 0.20), which in
             turn drives RetrievalEngine._rank() (0.3 * confidence). So a memory
             repeatedly judged IRRELEVANT loses confidence and sinks in ranking; one
             judged RELEVANT gains confidence and rises. This is a real RL-style
             reward/penalty on the retrieval policy, using the confidence machinery
             that already governs ranking -- no parallel scoring system needed.

          2. NEGATIVE EXAMPLES: an irrelevant (query, memory) pair is stored as a
             structured 'relevance_feedback' memory (SOCIAL pillar). _mismatch_penalty()
             (called from RetrievalEngine) reads these and down-weights a memory for a
             NEW query that looks like a query the memory was already rejected for.
             This is how Chronicle stops surfacing "animal" memories for "aristotle"
             even before its confidence has fully decayed.

          3. The feedback is also appended to a durable ledger on disk
             (Chronicle/memory/relevance_feedback.json via the store's dir) so the
             signal survives restarts and is auditable (Book II No Silent Failures).

        Constitutional law: Book II Principle I Memory First; Ch VI Memory Evolution
        ("knowledge is refined by use"); Ch VIII self-correction; Book I Article VII
        Collaboration (agents teach the shared memory).
        """
        rec = self.store.get(memory_id)
        applied = {"memory_id": memory_id, "found": rec is not None, "relevant": relevant}

        # 1. Reward / penalise the memory's usage track record -> confidence -> ranking.
        if rec is not None:
            before = rec.confidence
            rec.record_use(source_agent or "feedback", successful=bool(relevant))
            # record_use alone can't push usage below the 0.5 neutral floor for a
            # brand-new memory (success_rate*volume=0 -> exactly 0.5). To make negative
            # feedback actually BITE (and positive feedback actually reward), apply an
            # explicit, bounded confidence nudge on top and persist it. This is the RL
            # reward signal made effective. (Book II Ch VI Memory Evolution.)
            delta = 0.08 if relevant else -0.12
            rec.confidence = round(min(max(rec.confidence + delta, 0.0), 1.0), 4)
            self.store.update(rec)
            # store.update() recomputes confidence from factors; re-apply the nudge so
            # the earned adjustment survives (the nudge encodes verified relevance that
            # the static factors cannot yet see).
            rec.confidence = round(min(max(rec.confidence + delta, 0.0), 1.0), 4)
            self.store._records[rec.memory_id] = rec  # persist the nudged value
            try:
                self.store._persist()
            except Exception:
                pass
            applied["confidence_before"] = before
            applied["confidence_after"] = rec.confidence

        # 2. Persist a negative example so future look-alike queries are pre-filtered.
        if not relevant:
            try:
                neg = MemoryRecord(
                    pillar=MemoryPillar.SOCIAL, domain="feedback",
                    content=f"Memory {memory_id} judged IRRELEVANT to query '{query}'. Reason: {reason}",
                    summary=f"irrelevant:{query[:80]}",
                    source_repository="chronicle", source_agent=source_agent,
                    evidence=[reason] if reason else [],
                    tags=["relevance_feedback", "negative", memory_id])
                neg.embedding = self.embedder.encode(query)
                self.store.add(neg)
                applied["negative_example_stored"] = neg.memory_id
            except Exception as exc:
                log.warning("[chronicle] feedback: could not store negative example: %s", exc)

        # 3. Durable, auditable ledger (survives restart) under Chronicle's own memory dir.
        try:
            self._append_feedback_ledger({
                "query": query, "memory_id": memory_id, "relevant": bool(relevant),
                "reason": reason, "source_agent": source_agent, "ts": _fb_now()})
        except Exception as exc:
            log.warning("[chronicle] feedback: ledger append failed: %s", exc)

        log.info("[chronicle] relevance feedback from %s: memory=%s relevant=%s reason=%s",
                 source_agent, memory_id, relevant, (reason or "")[:80])
        return {"status": "complete", **applied}

    def _append_feedback_ledger(self, entry: Dict[str, Any]) -> None:
        """Append one feedback record to Chronicle/memory/relevance_feedback.json (thread-safe)."""
        import json
        from pathlib import Path as _P
        # Chronicle's constitutional data dir is its own storage_dir (memory/), NOT Nexus/data.
        ledger = _P(self.store.storage_dir) / "relevance_feedback.json"
        with _FB_LOCK:
            data: List[Dict[str, Any]] = []
            if ledger.exists():
                try:
                    data = json.loads(ledger.read_text(encoding="utf-8"))
                    if not isinstance(data, list):
                        data = []
                except Exception:
                    data = []
            data.append(entry)
            if len(data) > 10000:
                data = data[-10000:]
            tmp = ledger.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(ledger)

    def mismatch_penalty(self, query_embedding: List[float], memory_id: str,
                         threshold: float = 0.6) -> float:
        """
        Read stored negative examples for this memory and return a penalty in [0,1]
        if the CURRENT query closely resembles a query this memory was already judged
        irrelevant for. Used by RetrievalEngine to down-weight known-bad matches.
        """
        try:
            from core.embeddings import cosine_similarity  # type: ignore
        except Exception:
            return 0.0
        penalty = 0.0
        for rec in self.store.all():
            if "negative" not in rec.tags or memory_id not in rec.tags or not rec.embedding:
                continue
            sim = cosine_similarity(query_embedding, rec.embedding)
            if sim >= threshold:
                penalty = max(penalty, min(0.6, sim))
        return penalty

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
        if task == "memory.feedback":
            # FIX-CH-FB-01: any agent can teach Chronicle which retrieved memory was
            # relevant / irrelevant to a query. This is the ecosystem-wide RL loop.
            return self.apply_relevance_feedback(
                query=ctx.get("query", ""), memory_id=ctx.get("memory_id", ""),
                relevant=bool(ctx.get("relevant", False)), reason=ctx.get("reason", ""),
                source_agent=ctx.get("source_agent", sender))
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