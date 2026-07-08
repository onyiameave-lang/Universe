"""
Nexus - Ecosystem Coordination  (institutional, main entry point)
================================================================
Constitutional Name: Nexus  (formerly Universal AI)
Mission: Route, orchestrate in parallel under SLAs, resolve conflicts by
         confidence, and learn how the whole civilization cooperates.
(Book II Part I; Book II Part II Ch VIII.)

The institutional coordinator. Boots the live agents (Chronicle first for shared
memory), then routes/orchestrates with: SLA budgets + priority lanes, per-agent
circuit breakers, TTL result caching, PARALLEL execution of independent
sub-tasks, confidence-weighted conflict resolution, and a learned collaboration
graph.

Run:
    python main.py

Commands:
    <query>                       route (auto: direct / memory-first / orchestrate)
    urgent <query>                route on the fast priority lane
    classify <query>              classification only
    agents                        live agents + health
    breakers                      circuit-breaker states
    execution                     cache + breaker stats
    monitor                       full coordination + learning stats
    quit
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent
_ECO_ROOT = _REPO_ROOT.parent
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agents.coordinator_agent import NexusAgent  # type: ignore

LIVE_REPOS = {
    "chronicle": ("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent"),
    "atlas": ("Atlas", "agents/research_agent.py", "AtlasAgent"),
    "aegis": ("Aegis", "agents/auditor_agent.py", "AegisAgent"),
}


def _load_class(folder, rel, cls):
    path = _ECO_ROOT / folder / rel
    if not path.exists():
        return None
    root = _ECO_ROOT / folder
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # type: ignore
        return getattr(m, cls)
    except Exception as exc:
        logging.getLogger("nexus").warning("load %s failed: %s", folder, exc)
        return None


def boot():
    log = logging.getLogger("nexus")
    chronicle = None
    Ch = _load_class("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent")
    if Ch:
        try:
            chronicle = Ch(storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
            chronicle.start()
        except Exception as exc:
            log.warning("Chronicle failed: %s", exc)
    atlas = None
    At = _load_class("Atlas", "agents/research_agent.py", "AtlasAgent")
    if At:
        try:
            atlas = At(chronicle_client=chronicle)
            atlas.start()
        except Exception:
            atlas = None
    nexus = NexusAgent(chronicle_client=chronicle, atlas_client=atlas)
    nexus.start()
    if chronicle:
        nexus.register_agent("chronicle", chronicle)
    if atlas:
        nexus.register_agent("atlas", atlas)
    for name, (folder, rel, cls_name) in LIVE_REPOS.items():
        if name in ("chronicle", "atlas"):
            continue
        Cls = _load_class(folder, rel, cls_name)
        if not Cls:
            continue
        try:
            try:
                agent = Cls(chronicle_client=chronicle)
            except TypeError:
                agent = Cls()
            agent.start()
            nexus.register_agent(name, agent)
        except Exception as exc:
            log.warning("%s failed: %s", name, exc)
    return nexus


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    nexus = boot()

    print("=" * 64)
    print("  NEXUS - Institutional Coordinator")
    print("  SLAs. Circuit breakers. Parallel orchestration. Learned collaboration.")
    print("=" * 64)
    print(f"  Live agents: {list(nexus.registry.all().keys())}")
    print("  Commands: <query> | urgent <query> | classify <q> | agents | breakers | execution | monitor | quit")

    while True:
        try:
            line = input("Nexus> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            if line.startswith("classify "):
                print(json.dumps(nexus.act("domain.classify", {"query": line[9:], "_sender": "user"}), indent=2))
            elif line.startswith("urgent "):
                print(json.dumps(nexus.act("ecosystem.route",
                    {"query": line[7:], "priority": 2, "_sender": "user"}), indent=2))
            elif line == "agents":
                print(json.dumps(nexus.registry.health_summary(), indent=2))
            elif line == "breakers":
                print(json.dumps(nexus.executor.breaker_states(), indent=2))
            elif line == "execution":
                print(json.dumps(nexus.act("execution.stats", {"_sender": "user"}), indent=2))
            elif line == "monitor":
                print(json.dumps(nexus.act("ecosystem.monitor", {"_sender": "user"}), indent=2))
            else:
                print(json.dumps(nexus.act("ecosystem.route", {"query": line, "_sender": "user"}), indent=2))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    nexus.stop()
    print("Nexus shutdown complete.")


if __name__ == "__main__":
    main()
