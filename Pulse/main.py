"""
Pulse - Social Intelligence  (institutional, main entry point)
=============================================================
Constitutional Name: Pulse  (formerly SocialIntel)
Mission: Read authentic social sentiment, flag manipulation, detect trends.
(Book I Part IV Article VII; Book II Ch IV.)

An institutional social desk. It reads multiple platforms in parallel (Reddit
public JSON, Hacker News, StockTwits, all key-free), weighs every post by
AUTHENTICITY (influence x low bot-risk), flags COORDINATED MANIPULATION
(pump-and-dump / brigading), detects TRENDS with mention velocity, and reports
authenticity-weighted per-symbol sentiment so bots and hype never distort the
read. It picks its acquisition path via the reasoning loop. Offline, it says so
honestly rather than inventing posts.

Optional keys (degrade honestly if absent):
    REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET   higher Reddit limits (public JSON works without)

Run:
    python main.py

Commands:
    report [topics...]        full social report (auto path, mood, trends, manipulation)
    symbol <SYM>              authenticity-weighted sentiment for a symbol
    trends [topics...]        trending symbols with velocity
    manipulation [SYM]        coordinated-manipulation check
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
    modules_to_delete = []
    for mod_name in CONFLICTING_MODULES:
        # Find the module and all its submodules
        for m in list(sys.modules.keys()):
            if m == mod_name or m.startswith(mod_name + '.'):
                modules_to_delete.append(m)
    for m in modules_to_delete:
        if m in sys.modules:
            del sys.modules[m]

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
        logging.getLogger("pulse.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                      storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"))
    _unload_conflicting_modules()

    from agents.pulse_agent import PulseAgent  # type: ignore

    agent = PulseAgent(chronicle_client=chronicle)
    agent.start()

    avail = [n for n, ok in agent.engine.stats()["collectors"].items() if ok]
    print("=" * 64)
    print("  PULSE - Institutional Social Intelligence Desk")
    print("  Multi-platform. Authenticity-weighted. Manipulation-flagged. Trend-aware.")
    print("=" * 64)
    print(f"  Platforms available: {avail} | Chronicle: {chronicle is not None} | Brain: {agent.has_brain}")
    print("  Commands: report [topics] | symbol <SYM> | trends [topics] | manipulation [SYM] | status | quit")

    while True:
        try:
            line = input("Pulse> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "report":
                topics = parts[1:] or None
                print(json.dumps(agent.act("social.report", {"topics": topics, "_sender": "user"}), indent=2))
            elif cmd == "symbol" and len(parts) >= 2:
                print(json.dumps(agent.act("social.sentiment", {"symbol": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "trends":
                topics = parts[1:] or None
                print(json.dumps(agent.act("social.trends", {"topics": topics, "_sender": "user"}), indent=2))
            elif cmd == "manipulation":
                sym = parts[1] if len(parts) > 1 else None
                print(json.dumps(agent.act("social.manipulation", {"symbol": sym, "_sender": "user"}), indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: report crypto stocks")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    print("Pulse shutdown complete.")


if __name__ == "__main__":
    main()
