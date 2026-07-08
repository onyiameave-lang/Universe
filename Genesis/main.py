"""
Genesis - Agent Factory  (institutional, main entry point)
=========================================================
Constitutional Name: Genesis  (formerly Agent Factory)
Mission: Create, certify, and responsibly deploy new autonomous agents.
(Book I Part IV Article II.)

An institutional factory. It runs the full nine-stage birth process, gated at
every step: capability-gap analysis -> Nexus confirmation -> Chronicle reuse ->
Atlas research -> design -> code synthesis (AST + safety + lint) -> SANDBOX
certification (the agent must run its own tests in isolation) -> Aegis
compliance -> human-approved deploy. Created agents are versioned with rollback.

Deployment is never automatic: a new agent reaches the live tree only on
explicit human confirmation (Book VI human sovereignty).

Run:
    python main.py

Commands:
    analyze <domain>                          is a new agent warranted?
    gap <domain>                              full gap response (create/evolve/research)
    design <Name> <domain> <purpose...>       create a blueprint
    create <blueprint_id>                     run birth stages 1-8 (certify + validate)
    deploy <record_id>                        human-approve + materialize the agent
    registry                                  created-agent registry + versions
    rollback <Name> <version>                 restore a prior version
    status | quit
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_ECO_ROOT = _REPO_ROOT.parent
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agents.creator_agent import GenesisAgent  # type: ignore


def _load(folder, rel, cls):
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
        inst = getattr(m, cls)()
        inst.start()
        return inst
    except Exception:
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent")
    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
    aegis = _load("Aegis", "agents/auditor_agent.py", "AegisAgent")
    agent = GenesisAgent(chronicle_client=chronicle, atlas_client=atlas, aegis_client=aegis,
                        output_root=str(_ECO_ROOT))
    agent.start()

    print("=" * 64)
    print("  GENESIS - Institutional Agent Factory")
    print("  Synthesize -> sandbox-certify -> Aegis -> human-approved deploy.")
    print("=" * 64)
    print(f"  Chronicle: {chronicle is not None} | Atlas: {atlas is not None} | Aegis: {aegis is not None}"
          f" | Brain: {agent.has_brain}")
    print("  Commands: analyze <d> | gap <d> | design <Name> <d> <purpose> | create <bp> |")
    print("            deploy <record> | registry | rollback <Name> <v> | status | quit")

    last_bp = last_record = None
    while True:
        try:
            line = input("Genesis> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "analyze" and len(parts) >= 2:
                print(json.dumps(agent.act("capability.analyze", {"domain": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "gap" and len(parts) >= 2:
                print(json.dumps(agent.act("gap.respond", {"domain": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "design" and len(parts) >= 4:
                out = agent.act("agent.design", {"name": parts[1], "domain": parts[2],
                    "purpose": " ".join(parts[3:]), "_sender": "user"})
                last_bp = out["blueprint"]["blueprint_id"]
                print(json.dumps(out, indent=2))
                print(f"\n(blueprint_id = {last_bp}; run: create {last_bp})")
            elif cmd == "create" and len(parts) >= 2:
                bp_id = parts[1] if parts[1] != "last" else last_bp
                out = agent.act("agent.create", {"blueprint_id": bp_id, "reason": "user", "_sender": "user"})
                last_record = out.get("record_id")
                print(json.dumps(out, indent=2))
                if last_record:
                    print(f"\n(record_id = {last_record}; run: deploy {last_record})")
            elif cmd == "deploy" and len(parts) >= 2:
                rid = parts[1] if parts[1] != "last" else last_record
                out = agent.act("agent.deploy", {"record_id": rid, "human_confirm": True, "_sender": "user"})
                print(json.dumps(out, indent=2))
            elif cmd == "registry":
                print(json.dumps(agent.act("registry.list", {"_sender": "user"}), indent=2))
            elif cmd == "rollback" and len(parts) >= 3:
                print(json.dumps(agent.act("agent.rollback",
                    {"name": parts[1], "to_version": int(parts[2]), "_sender": "user"}), indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: design WeatherWatcher weather Forecast weather risk")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    for peer in (aegis, atlas, chronicle):
        if peer:
            try:
                peer.stop()
            except Exception:
                pass
    print("Genesis shutdown complete.")


if __name__ == "__main__":
    main()
