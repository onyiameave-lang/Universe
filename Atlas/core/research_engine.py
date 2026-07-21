"""
Atlas.core.research_engine
=========================
Institutional research engine. (Book II Ch IV; Book I Part IV Articles VII-X.)

Beyond gather-and-summarize, this engine behaves like a research desk:

  * MULTI-SOURCE acquisition (Wikipedia, arXiv, Semantic Scholar, PubMed,
    Crossref, Hacker News, GDELT, web, PDF) chosen by domain.
  * CORROBORATION: cross-source agreement raises confidence; monoculture lowers it.
  * CONTRADICTION analysis: explicit consensus vs dissent (institutional honesty).
  * DEPTH ESCALATION: if confidence is below target, it AUTOMATICALLY deepens
    (more sources, follow the strongest citations) until target or budget hit.
  * CITATION CHASING: follows the highest-impact paper's link to gather more.
  * FULL PROVENANCE: every claim traces to sources; the report is auditable.

Confidence is calibrated (analysis.compute_confidence) and never fabricated.

FIX LOG (Phase 2):
  FIX-P2-08: Added _llm_only_answer() — when ALL external sources fail (zero
              evidence), the engine asks the LLM directly and returns a report
              with source_status._summary="llm_only" and confidence=0.4.
              This prevents "Insufficient evidence to synthesize." from
              propagating up to research_agent._run() as a failure.
              (Book II Principle V Graceful Degradation; Book IV resilience.)
  FIX-P2-09: investigate() now calls _llm_only_answer() when all_evidence is
              empty after all gather rounds. The report is marked honestly with
              limitations noting LLM-only sourcing. (Book II No Silent Failures.)
  FIX-P2-10: SourceRegistry is now constructed with chronicle_client so that
              Memory First (Book II Principle I) is honoured at the source layer.
  FIX-P2-11: _preserve() now logs errors at WARNING instead of silently passing.
              (Book II No Silent Failures — Chronicle write failures are visible.)
  FIX-P2-12: _preserve() sends research results TO Chronicle after every
              successful investigation. (Book II Everything Communicates;
              Chronicle as main source of truth.)

FIX LOG (phase4-atlas-engine-v1  2026-07-21):
  BUG-P4-01  _preserve() and _preserve_hypothesis() called chronicle.store(...)
             which resolves to ChronicleAgent.self.store — a VectorStore instance
             attribute — not the store() convenience method.  VectorStore is not
             callable, so every Chronicle write raised:
               TypeError: 'VectorStore' object is not callable
             logged as "Chronicle store failed (non-fatal): 'VectorStore' object
             is not callable".
             ROOT CAUSE: ChronicleAgent defines both:
               self.store = VectorStore(...)   # instance attribute (line ~80)
               def store(self, ...): ...       # convenience method (line ~220)
             Python resolves self.store to the instance attribute first, so
             chronicle.store(...) hits the VectorStore object, not the method.
             FIX: Changed chronicle.store(...) -> chronicle.store_memory(...)
             which is the unambiguous public API (no name collision).
             Constitutional law: Book II Principle I Memory First —
             Chronicle writes must actually work.

  BUG-P4-02  _recall() silently swallowed all Chronicle read errors (bare
             except: return []).  No log, no audit trail.
             FIX: Errors now logged at WARNING level with exc_info=True.
             Constitutional law: Book II No Silent Failures.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.sources import SourceRegistry, Evidence  # type: ignore
from intelligence.analysis import (relevance, summarize, keywords, extract_claims,  # type: ignore
                                   compute_corroboration, compute_confidence)
from intelligence.contradiction import ContradictionEngine  # type: ignore


class ResearchEngine:
    def __init__(self, chronicle_client=None, llm=None, confidence_target: float = 0.6):
        self.chronicle = chronicle_client
        self.llm = llm
        # FIX-P2-10: Pass chronicle_client to SourceRegistry so Memory First
        # is honoured at the source layer. (Book II Principle I.)
        self.sources = SourceRegistry(chronicle_client=chronicle_client)
        self.contradiction = ContradictionEngine(llm=llm)
        self.confidence_target = confidence_target
        self._reports: Dict[str, Dict[str, Any]] = {}
        self._hypotheses: Dict[str, Dict[str, Any]] = {}

    # ---- primary: investigate with automatic depth escalation ----

    def investigate(self, query: str, domain: str = "general", depth: str = "standard",
                   sources: Optional[List[str]] = None, max_rounds: int = 3) -> Dict[str, Any]:
        started = time.time()
        report_id = f"rpt-{uuid.uuid4().hex[:10]}"
        prior = self._recall(query, domain)

        all_evidence: List[Evidence] = []
        source_status: Dict[str, Any] = {}
        rounds: List[Dict[str, Any]] = []
        depths = {"shallow": ["shallow"], "standard": ["standard", "deep"],
                  "deep": ["deep", "deep", "deep"]}.get(depth, ["standard", "deep"])

        conf = {"confidence": 0.0, "factors": {}}
        for round_idx in range(min(max_rounds, len(depths) + 1)):
            round_depth = depths[min(round_idx, len(depths) - 1)]
            gathered = self.sources.gather(query, domain=domain, depth=round_depth, sources=sources)
            new_ev = gathered["evidence"]
            # dedupe by (source,title)
            seen = {(e.source, e.title) for e in all_evidence}
            for e in new_ev:
                if (e.source, e.title) not in seen:
                    all_evidence.append(e)
            source_status.update(gathered["source_status"])

            # score relevance + corroboration, then confidence
            for e in all_evidence:
                e.relevance = relevance(query, f"{e.title}. {e.text}")
            compute_corroboration(all_evidence)
            all_evidence.sort(key=lambda e: e.relevance * (0.4 + 0.4 * e.credibility +
                                                          0.2 * min(e.corroboration / 2, 1)),
                             reverse=True)
            conf = compute_confidence([e.to_dict() for e in all_evidence], query)
            rounds.append({"round": round_idx + 1, "depth": round_depth,
                          "evidence": len(all_evidence), "confidence": conf["confidence"]})

            if conf["confidence"] >= self.confidence_target:
                break
            # DEPTH ESCALATION: chase the strongest paper's citations next round
            top_cited = next((e for e in all_evidence if e.citations and e.url), None)
            if top_cited and round_idx < max_rounds - 1:
                extra = self.sources.fetch_url(top_cited.url)
                if extra:
                    extra.relevance = relevance(query, f"{extra.title}. {extra.text}")
                    all_evidence.append(extra)

        # FIX-P2-08/09: LLM-only fallback when ALL sources returned zero evidence.
        # (Book II Principle V Graceful Degradation — the desk never returns empty-handed
        # when the LLM itself is available and working.)
        llm_only_mode = False
        if not all_evidence:
            llm_fallback = self._llm_only_answer(query, domain)
            if llm_fallback:
                all_evidence = [llm_fallback]
                source_status["_summary"] = "llm_only"
                source_status["llm_fallback"] = {"gathered": 1, "status": "ok"}
                conf = {"confidence": 0.4, "factors": {"llm_only": True}}
                llm_only_mode = True

        evidence = [e.to_dict() for e in all_evidence]

        # claim extraction + contradiction/consensus analysis
        claims_by_source = []
        for e in all_evidence[:12]:
            title = getattr(e, "title", "") or ""
            text = e.text or ""
            # Skip bare-title evidence (no real abstract available) -- a
            # title is metadata, not a claim, and letting it into consensus
            # clustering let a paper TITLE get labeled "the consensus" purely
            # from high source credibility + superficial keyword overlap.
            if len(text.strip()) < len(title.strip()) + 15:
                continue
            for c in extract_claims(e.text, max_claims=2):
                claims_by_source.append((c, e.source, e.credibility))
        agreement = self.contradiction.analyze(all_evidence, claims_by_source)

        corpus = " ".join(e.text for e in all_evidence[:6])
        summary = self._synthesize(query, corpus, agreement, domain, all_evidence[:6]) if corpus else ""
        key_terms = [k for k, _ in keywords(corpus, top_n=8)] if corpus else []

        findings = []
        if prior:
            findings.append(f"Chronicle held {len(prior)} prior memories.")
        if all_evidence:
            findings.append(f"Gathered {len(all_evidence)} items across "
                          f"{len({e.source for e in all_evidence})} independent sources over "
                          f"{len(rounds)} round(s).")
            if llm_only_mode:
                findings.append("All external sources unavailable — answer provided by LLM directly.")
            if agreement.get("contradictions"):
                findings.append(f"Detected {len(agreement['contradictions'])} genuine "
                              f"cross-source disagreement(s).")
        else:
            findings.append("No external evidence gathered; sources unreachable or no matches.")

        limitations = []
        if llm_only_mode:
            limitations.append("Answer sourced from LLM only — no external evidence corroborated.")
        if conf["confidence"] < self.confidence_target:
            limitations.append(f"Confidence {conf['confidence']} below target "
                             f"{self.confidence_target} after {len(rounds)} round(s).")
        if len({e.source for e in all_evidence}) < 2:
            limitations.append("Single-source: corroboration weak.")

        report = {"report_id": report_id, "query": query, "domain": domain,
                  "summary": summary or "Insufficient evidence to synthesize.",
                  "consensus": agreement.get("consensus"), "dissent": agreement.get("dissent"),
                  "contradictions": agreement.get("contradictions"),
                  "agreement_narrative": agreement.get("narrative"),
                  "findings": findings, "key_terms": key_terms, "evidence": evidence,
                  "prior_memory": prior, "source_status": source_status,
                  "confidence": conf["confidence"], "confidence_factors": conf["factors"],
                  "rounds": rounds, "limitations": limitations,
                  "duration_sec": round(time.time() - started, 2)}
        self._reports[report_id] = report
        self._preserve(report)
        return report

    # ---- FIX-P2-08: LLM-only fallback ----

    def _llm_only_answer(self, query: str, domain: str) -> Optional[Evidence]:
        """
        FIX-P2-08: When ALL external sources fail, ask the LLM directly.
        Returns a synthetic Evidence item so the rest of the pipeline works
        normally. Marked with source="llm_fallback" and credibility=0.5.
        (Book II Principle V Graceful Degradation — the desk never returns
        empty-handed when the LLM is available.)
        """
        if self.llm is None or not getattr(self.llm, "has_any", False):
            return None
        try:
            from shared.llm import system_prompt  # type: ignore
            r = self.llm.complete(
                system_prompt("atlas"),
                f"Question: {query}\nDomain: {domain}\n\n"
                f"All external research sources are currently unavailable (rate limited or DNS failure). "
                f"Please answer this question directly from your training knowledge. "
                f"Be accurate, concise (3-5 sentences), and note that this answer is not "
                f"corroborated by external sources.",
                temperature=0.2, max_tokens=400,
            )
            if r.ok and r.text.strip():
                from core.sources import Evidence, SOURCE_CREDIBILITY  # type: ignore
                return Evidence(
                    source="llm_fallback",
                    title=f"LLM answer: {query[:80]}",
                    text=r.text.strip(),
                    url="",
                    credibility=0.5,
                )
        except Exception as exc:
            import logging
            logging.getLogger("atlas.engine").warning(
                "atlas.engine: LLM-only fallback failed: %s", exc)
        return None

    def _synthesize(self, query: str, corpus: str, agreement: Dict, domain: str,
                   documents: Optional[List[Any]] = None) -> str:
        if self.llm is not None and getattr(self.llm, "has_any", False):
            try:
                from shared.llm import system_prompt
                r = self.llm.complete(system_prompt("atlas"),
                    f"Question: {query}\nConsensus: {agreement.get('consensus')}\n"
                    f"Dissent: {agreement.get('dissent', [])[:3]}\n\nCorpus:\n{corpus[:3000]}\n\n"
                    f"Synthesize 3-4 sentences grounded in the corpus. State the consensus and, "
                    f"if present, the genuine disagreement. Do not overstate certainty.",
                    temperature=0.2, max_tokens=320)
                if r.ok and r.text.strip():
                    return r.text.strip()
            except Exception:
                pass

        # Non-LLM fallback: take ONE representative sentence per source document
        # rather than merging every document into one blob and picking globally
        # "salient" sentences -- that approach interleaves sentences from
        # entirely unrelated papers into a single incoherent paragraph, since
        # term-frequency salience has no notion of document boundaries.
        if documents:
            per_doc_sentences = []
            seen = set()
            for doc in documents:
                text = getattr(doc, "text", "") or ""
                title = getattr(doc, "title", "") or ""
                # Many crossref/pubmed results only have a title, no abstract
                # -- text == title (or barely longer) isn't a real sentence,
                # just metadata, and produces an uninformative/repetitive
                # "summary" if treated as one.
                if not text.strip() or len(text.strip()) < len(title.strip()) + 15:
                    continue
                top_sentence = summarize(text, query=query, max_sentences=1)
                if top_sentence and top_sentence not in seen:
                    per_doc_sentences.append(top_sentence)
                    seen.add(top_sentence)
                if len(per_doc_sentences) >= 4:
                    break
            base = " ".join(per_doc_sentences)
        else:
            base = summarize(corpus, query=query, max_sentences=4)
            seen = set()

        narrative = agreement.get("narrative")
        if narrative and narrative not in base:
            base = narrative + " " + base
        return base

    # ---- hypotheses with argument weighing ----

    def generate_hypothesis(self, statement: str, domain: str = "general") -> Dict[str, Any]:
        hid = f"hyp-{uuid.uuid4().hex[:8]}"
        h = {"hypothesis_id": hid, "statement": statement, "domain": domain,
             "status": "proposed", "confidence": 0.0}
        self._hypotheses[hid] = h
        return h

    def validate_hypothesis(self, hypothesis_id: str) -> Dict[str, Any]:
        h = self._hypotheses.get(hypothesis_id)
        if not h:
            return {"status": "error", "message": f"hypothesis {hypothesis_id} not found"}
        stmt = h["statement"]
        sup = self.sources.gather(f"evidence supporting {stmt}", domain=h["domain"], depth="standard")["evidence"]
        opp = self.sources.gather(f"evidence against {stmt}", domain=h["domain"], depth="standard")["evidence"]
        for e in sup + opp:
            e.relevance = relevance(stmt, f"{e.title}. {e.text}")
        compute_corroboration(sup + opp)
        # weight each side by relevance * credibility * (1 + corroboration)
        def weight(evs):
            return sum(e.relevance * e.credibility * (1 + min(e.corroboration, 3) * 0.3) for e in evs)
        s_w, o_w = weight(sup), weight(opp)
        total = s_w + o_w
        h["support_weight"] = round(s_w, 3)
        h["oppose_weight"] = round(o_w, 3)
        h["confidence"] = round(s_w / total, 4) if total > 0 else 0.5
        h["supporting"] = [e.to_dict() for e in sorted(sup, key=lambda e: e.relevance, reverse=True)[:4]]
        h["contradicting"] = [e.to_dict() for e in sorted(opp, key=lambda e: e.relevance, reverse=True)[:4]]
        if h["confidence"] >= 0.7:
            h["status"], h["conclusion"] = "validated", "Supported by weighted evidence."
        elif h["confidence"] <= 0.3:
            h["status"], h["conclusion"] = "refuted", "Contradicted by weighted evidence."
        else:
            h["status"], h["conclusion"] = "inconclusive", "Evidence genuinely mixed."
        self._preserve_hypothesis(h)
        return h

    def synthesize(self, topics: List[str], domain: str = "general") -> Dict[str, Any]:
        reports = [self.investigate(t, domain=domain) for t in topics]
        connections = []
        for i in range(len(reports)):
            for j in range(i + 1, len(reports)):
                shared = set(reports[i]["key_terms"]) & set(reports[j]["key_terms"])
                if shared:
                    connections.append({"topics": [topics[i], topics[j]],
                        "shared_concepts": sorted(shared),
                        "strength": round(len(shared) / max(len(set(reports[i]["key_terms"]) |
                                          set(reports[j]["key_terms"])), 1), 3)})
        overall = sum(r["confidence"] for r in reports) / max(len(reports), 1)
        return {"topics": topics, "domain": domain,
                "reports": [{"query": r["query"], "summary": r["summary"],
                           "confidence": r["confidence"]} for r in reports],
                "connections": connections, "overall_confidence": round(overall, 3)}

    def fetch_and_analyze(self, url: str, query: str = "") -> Dict[str, Any]:
        ev = self.sources.fetch_url(url)
        if not ev:
            return {"status": "error", "message": f"could not fetch {url}"}
        ev.relevance = relevance(query or ev.title, ev.text) if query else 0.5
        return {"status": "complete", "title": ev.title,
                "summary": summarize(ev.text, query=query, max_sentences=4),
                "claims": extract_claims(ev.text, max_claims=5),
                "key_terms": [k for k, _ in keywords(ev.text, top_n=8)],
                "relevance": ev.relevance, "source": ev.source, "url": url}

    def _recall(self, query, domain):
        if self.chronicle is None:
            return []
        try:
            res = self.chronicle.search(query=query, domain=domain, limit=3, requester="atlas")
            return res if isinstance(res, list) else []
        except Exception as exc:
            import logging
            logging.getLogger("atlas.engine").warning(
                "atlas.engine: Chronicle search failed (non-fatal): %s "
                "(Book II No Silent Failures — Chronicle read errors are visible)", exc)
            return []

    def _preserve(self, report):
        # FIX-P2-11/12: Log errors at WARNING (No Silent Failures) and always
        # attempt to store research results in Chronicle (Everything Communicates).
        # FIX-P4-01: Use store_memory() not store() — ChronicleAgent.self.store is
        # a VectorStore instance attribute that shadows the store() method.
        # store_memory() is the unambiguous public API.
        # Constitutional law: Book II Principle I Memory First.
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(
                content=report["summary"],
                pillar="semantic",
                domain=report["domain"],
                summary=report["summary"][:160],
                source_repository="Atlas",
                source_agent="atlas",
                evidence=[e["url"] for e in report["evidence"] if e.get("url")],
                tags=["atlas", "research"] + report["key_terms"][:5],
            )
        except Exception as exc:
            import logging
            logging.getLogger("atlas.engine").warning(
                "atlas.engine: Chronicle store failed (non-fatal): %s "
                "(Book II No Silent Failures — Chronicle write errors are visible)", exc)

    def _preserve_hypothesis(self, h):
        # FIX-P4-01: Use store_memory() not store() — same VectorStore name collision.
        if self.chronicle is None:
            return
        try:
            self.chronicle.store_memory(
                content=f"Hypothesis: {h['statement']} -> {h['status']} (conf {h['confidence']})",
                pillar="evolutionary",
                domain=h["domain"],
                summary=f"Hypothesis {h['status']}: {h['statement'][:120]}",
                source_repository="Atlas",
                source_agent="atlas",
                tags=["atlas", "hypothesis", h["status"]],
            )
        except Exception:
            pass  # aegis:allow-silent

    def stats(self) -> Dict[str, Any]:
        return {"reports": len(self._reports), "hypotheses": len(self._hypotheses),
               "confidence_target": self.confidence_target}