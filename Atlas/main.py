"""
Atlas - Research Intelligence  (institutional desk, main entry point)
====================================================================
Constitutional Name: Atlas  (formerly Research AI)
Mission: Investigate with multi-source rigor, corroborate across independent
         sources, surface genuine disagreement, and report calibrated
         confidence. (Book II Ch IV.)

This is a research desk, not a lookup. It queries many live sources in parallel
(Wikipedia, arXiv, Semantic Scholar, PubMed, Crossref, Hacker News, GDELT, web,
PDF), rewards cross-source corroboration, detects contradictions, escalates
depth automatically until a confidence target is met, and chooses its research
approach (academic / frontier / market / broad) via the reasoning loop.

Optional keys (all degrade honestly if absent):
    SEMANTIC_SCHOLAR_KEY   raises Semantic Scholar rate limits
    ANTHROPIC/OPENAI/GEMINI keys enable LLM phrasing of synthesis

Run:
    python main.py

Commands:
    investigate <query>       full multi-source investigation (auto path + depth)
    academic <query>          force peer-reviewed sources
    market <query>            force news/practitioner sources
    hypothesis <statement>    propose a hypothesis
    validate <hyp_id>         weigh evidence for AND against
    fetch <url>               fetch + analyze a page or PDF
    status | quit
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
for p in (_REPO_ROOT, _REPO_ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# B-11/12 fix: import shared utilities instead of duplicating them here
from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

# Keep local aliases so the rest of this file's call-sites are unchanged
_load_dotenv_early = load_dotenv_early
_unload_conflicting_modules = unload_conflicting_modules

from agents.research_agent import AtlasAgent, PATH_SOURCES  # type: ignore




def _try_chronicle():
    try:
        chron_root = _REPO_ROOT.parent / "Chronicle"
        if str(chron_root) not in sys.path:
            sys.path.insert(0, str(chron_root))
        
        # Unload any existing 'agents' or 'core' that might be from Atlas
        _unload_conflicting_modules()
        
        from agents.chronicle_agent import ChronicleAgent  # type: ignore
        c = ChronicleAgent(storage_dir=str(chron_root / "memory" / "store"))
        c.start()
        
        # Unload Chronicle's modules before returning to Atlas
        _unload_conflicting_modules()
        
        return c
    except Exception:
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _try_chronicle()
    agent = AtlasAgent(chronicle_client=chronicle)
    agent.start()

    print("=" * 64)
    print("  ATLAS - Institutional Research Desk")
    print("  Multi-source. Corroborated. Contradiction-aware. Calibrated.")
    print("=" * 64)
    print(f"  Chronicle: {chronicle is not None} | Brain: {agent.has_brain}")
    print("  Sources: wikipedia, arxiv, semantic_scholar, pubmed, crossref, hackernews, gdelt, web, pdf")
    print("  Commands: investigate <q> | academic <q> | market <q> | hypothesis <s> | validate <id> | fetch <url> | status | quit")

    while True:
        try:
            line = input("Atlas> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            if line.startswith("investigate "):
                print(json.dumps(agent.act("research.investigate",
                    {"query": line[12:], "domain": "general", "_sender": "user"}), indent=2))
            elif line.startswith("academic "):
                print(json.dumps(agent.engine.investigate(line[9:], domain="research",
                    sources=PATH_SOURCES["academic_rigor"]), indent=2))
            elif line.startswith("market "):
                print(json.dumps(agent.engine.investigate(line[7:], domain="trading",
                    sources=PATH_SOURCES["market_pulse"]), indent=2))
            elif line.startswith("hypothesis "):
                print(json.dumps(agent.act("hypothesis.generate",
                    {"statement": line[11:], "_sender": "user"}), indent=2))
            elif line.startswith("validate "):
                print(json.dumps(agent.act("hypothesis.test",
                    {"hypothesis_id": line[9:].strip(), "_sender": "user"}), indent=2))
            elif line.startswith("fetch "):
                print(json.dumps(agent.act("web.fetch",
                    {"url": line[6:].strip(), "_sender": "user"}), indent=2))
            elif line == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: investigate CRISPR off-target effects")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    if chronicle:
        chronicle.stop()
    print("Atlas shutdown complete.")


if __name__ == "__main__":
    main()
