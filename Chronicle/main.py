"""
Chronicle - Memory Intelligence  (institutional, main entry point)
=================================================================
Constitutional Name: Chronicle  (formerly AI Memory System)
Mission: Preserve, anticipate, reconcile, and evolve the ecosystem's knowledge.
(Book II Part III.)

An institutional, self-correcting knowledge base. It stores with real
embeddings, anticipates each repo's needs, consolidates to key points, detects
GENUINE semantic contradictions and revises belief (referring conflicts to
Atlas for an evidence verdict, superseding the weaker belief without deleting
it), traces full provenance for any belief, and keeps retrieval fast via
hot/warm/cold tiers. Auto-connects to Atlas if that repo is beside it.

Run:
    python main.py

Commands:
    store <text>                      store a semantic memory
    search <query>                    five-stage retrieval
    answer <query>                    grounded synthesis
    contradictions [domain]           detect genuine semantic conflicts
    revise <mem_a> <mem_b>            adjudicate + revise belief (via Atlas)
    provenance <mem_id>               full auditable lineage of a belief
    rebalance                         recompute hot/warm/cold tiers
    improve <strategy> <domain>       research a better strategy version
    anticipate <repository>           predict what a repo will need
    consolidate [domain]              merge/distill/prune
    status | quit
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.chronicle_agent import ChronicleAgent

_REPO_ROOT = Path(__file__).resolve().parent
for p in (_REPO_ROOT, _REPO_ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# B-11/12 fix: import shared utilities instead of duplicating them here
from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

# Keep local aliases so the rest of this file's call-sites are unchanged
_load_dotenv_early = load_dotenv_early
_unload_conflicting_modules = unload_conflicting_modules



def _load(folder, rel, cls, **kw):
    root = _REPO_ROOT.parent / folder
    path_added = False
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", root / rel)
        if spec is None or spec.loader is None:
            return None # File doesn't exist or is not a module
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        logging.getLogger("chronicle.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def _seed(agent: ChronicleAgent) -> None:
    if agent.store.stats()["active"] > 0:
        return
    # seed a genuine contradiction so `contradictions` + `revise` are demonstrable
    agent.store_memory("Momentum strategies increase returns in trending FX regimes.",
                      "semantic", "trading", source_repository="oracle",
                      evidence=["backtest 2023"], lesson="momentum works when trending")
    agent.store_memory("Momentum strategies decrease returns and fail in FX markets.",
                      "semantic", "trading", source_repository="pulse",
                      lesson="momentum fails")
    agent.store_memory("EURUSD reacts strongly to ECB rate guidance.", "semantic", "trading",
                      source_repository="chronicle", evidence=["ECB minutes"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
    _unload_conflicting_modules()

    from agents.chronicle_agent import ChronicleAgent  # type: ignore

    agent = ChronicleAgent(storage_dir=str(_REPO_ROOT / "memory" / "store"), atlas_client=atlas)
    agent.start()
    _seed(agent)

    print("=" * 64)
    print("  CHRONICLE - Institutional Memory & Knowledge Base")
    print("  Self-correcting. Provenance-tracked. Tiered. Anticipatory.")
    print("=" * 64)
    print(f"  Embeddings: {agent.embedder.backend} | Records: {agent.store.stats()['active']}"
          f" | Atlas: {atlas is not None} | Brain: {agent.has_brain}")
    print("  Commands: store | search | answer | contradictions | revise <a> <b> |")
    print("            provenance <id> | rebalance | improve <s> <d> | anticipate <r> | consolidate | status | quit")

    while True:
        try:
            line = input("Chronicle> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "store":
                print(json.dumps(agent.store_memory(content=line[6:], source_repository="user"), indent=2))
            elif cmd == "search":
                print(json.dumps(agent.act("memory.search", {"query": line[7:], "_sender": "user"}), indent=2))
            elif cmd == "answer":
                print(json.dumps(agent.act("memory.answer", {"query": line[7:], "_sender": "user"}), indent=2))
            elif cmd == "contradictions":
                dom = parts[1] if len(parts) > 1 else None
                print(json.dumps(agent.act("contradiction.detect",
                    {"domain": dom, "auto_revise": True, "_sender": "user"}), indent=2))
            elif cmd == "revise" and len(parts) >= 3:
                print(json.dumps(agent.act("belief.revise",
                    {"memory_a": parts[1], "memory_b": parts[2], "_sender": "user"}), indent=2))
            elif cmd == "provenance" and len(parts) >= 2:
                print(json.dumps(agent.act("provenance.trace",
                    {"memory_id": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "rebalance":
                print(json.dumps(agent.act("memory.rebalance", {"_sender": "user"}), indent=2))
            elif cmd == "improve" and len(parts) >= 3:
                print(json.dumps(agent.act("strategy.improve",
                    {"strategy": parts[1], "domain": parts[2], "_sender": "user"}), indent=2))
            elif cmd == "anticipate" and len(parts) >= 2:
                print(json.dumps(agent.act("memory.anticipate",
                    {"repository": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "consolidate":
                dom = parts[1] if len(parts) > 1 else None
                print(json.dumps(agent.act("memory.consolidate", {"domain": dom, "_sender": "user"}), indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command.")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    if atlas:
        atlas.stop()
    print("Chronicle shutdown complete. Memory persisted to disk.")


if __name__ == "__main__":
    main()
