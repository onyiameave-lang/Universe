#!/usr/bin/env python3
"""
ecosystem.py - Root orchestrator. Boots all 9 repositories (Chronicle first so
memory is shared), wires them into Nexus, and exposes one query prompt.
"""
from __future__ import annotations
import importlib.util
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from shared.config import get_config
    get_config() # Triggers load_dotenv in shared.config
except ImportError:
    pass

REPO_MAP = {
    "chronicle": ("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent"),
    "oracle": ("Oracle", "agents/oracle_agent.py", "OracleAgent"),
    "nexus": ("Nexus", "agents/coordinator_agent.py", "NexusAgent"),
    "sentinel": ("Sentinel", "agents/sentinel_agent.py", "SentinelAgent"),
    "pulse": ("Pulse", "agents/pulse_agent.py", "PulseAgent"),
    "atlas": ("Atlas", "agents/research_agent.py", "AtlasAgent"),
    "forge": ("Forge", "agents/training_agent.py", "ForgeAgent"),
    "genesis": ("Genesis", "agents/creator_agent.py", "GenesisAgent"),
    "aegis": ("Aegis", "agents/auditor_agent.py", "AegisAgent")
}

# These are the common top-level directory names in agent repos that can cause import conflicts
# when loading multiple agents in the same process.
CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry"
]

def _unload_conflicting_modules():
    """Forcibly unload modules that cause namespace collisions between repositories."""
    for mod_name in CONFLICTING_MODULES:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    mods_to_del = []
    # Find all modules and sub-modules that match the conflicting names.
    for mod in list(sys.modules.keys()):
        for conflict in CONFLICTING_MODULES:
            if mod == conflict or mod.startswith(conflict + '.'):
                mods_to_del.append(mod)
    
    for mod in mods_to_del:
        if mod in sys.modules:
            del sys.modules[mod]


def _load(folder, rel, cls, **kw):
    path = ROOT / folder / rel
    if not path.exists():
        return None
    r = ROOT / folder
    if str(r) not in sys.path:
        sys.path.insert(0, str(r))
    try:
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", path)
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return getattr(m, cls)
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        logging.getLogger("ecosystem").warning("load %s failed: %s", folder, exc)
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    print("=" * 64)
    print("  AI ECOSYSTEM - booting the civilization")
    print("=" * 64)

    BOOT_ORDER = ["chronicle", "atlas", "nexus", "aegis", "sentinel", "pulse", "forge", "genesis", "oracle"]
    agents = {}
    chronicle = None

    for name in BOOT_ORDER:
        _unload_conflicting_modules()
        if name not in REPO_MAP: continue
        folder, rel, cls = REPO_MAP[name]
        C = _load(folder, rel, cls)
        if not C:
            continue

        inst = None
        try:
            if name == "chronicle":
                inst = C(storage_dir=str(ROOT / "Chronicle" / "memory" / "store"))
                chronicle = inst # Set for subsequent agents
            elif name == "atlas":
                inst = C(chronicle_client=chronicle)
            else:
                # Generic case for most agents
                try:
                    inst = C(chronicle_client=chronicle)
                except TypeError:
                    inst = C()
            
            if inst:
                inst.start()
                agents[name] = inst

        except Exception as exc:
            logging.warning("%s failed: %s", name, exc)

    nexus = agents.get("nexus")
    if nexus and hasattr(nexus, "register_agent"):
        for n, a in agents.items():
            if n != "nexus":
                try: nexus.register_agent(n, a)
                except Exception: pass

    print(f"  Live agents: {list(agents.keys())}")
    print("  Type a query (routed via Nexus), 'status', or 'quit'.")
    while True:
        try:
            line = input("Ecosystem> ").strip()
            if not line: continue
            if line.lower() in ("quit", "exit", "q"): break
            if line == "status":
                print(json.dumps({n: a.get_status() for n, a in agents.items()}, indent=2)[:3000]); continue
            if nexus:
                print(json.dumps(nexus.act("ecosystem.route", {"query": line, "_sender": "user"}), indent=2)[:4000])
            else:
                print("Nexus not available; run individual repos via their main.py")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("Error:", exc)
    for a in agents.values():
        try: a.stop()
        except Exception: pass
    print("Ecosystem shutdown complete.")


if __name__ == "__main__":
    main()
