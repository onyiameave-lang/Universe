"""
Pulse - Social Intelligence  (Universe-oracle deep-fix v5)
==========================================================
Constitutional Name: Pulse  (formerly SocialIntel)
Mission: Read authentic social sentiment, flag manipulation, detect trends.

CRITICAL FIX in this version:
  load_dotenv() is called as the ABSOLUTE FIRST ACTION before any other import.
  Previously, PULSE_LLM_MODE / ORACLE_LLM_MODE were set in .env but never
  loaded into os.environ before shared/llm/client.py read them at module import
  time — so the essential gate was always reading an empty string and defaulting
  to "full" mode regardless of what was in .env.

  The fix: dotenv is loaded here, before any shared.* import, so client.py
  reads the correct value when it is first imported.

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

# ============================================================
# STEP 0: Load .env BEFORE any other import.
# This is the root fix for PULSE_LLM_MODE not being read.
# ============================================================
import os as _os
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent

# Try python-dotenv; if not installed, fall back to a manual parser
# so the fix works even without the package.
def _load_dotenv_early() -> None:
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        # Also check parent directory (project root)
        env_file = _REPO_ROOT.parent / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=str(env_file), override=False)
        return
    except ImportError:
        pass
    # Manual fallback: parse KEY=VALUE lines
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in _os.environ:
                _os.environ[key] = val
    except Exception:
        pass

_load_dotenv_early()

# ============================================================
# STEP 1: Now safe to import everything else
# ============================================================
import importlib.util
import json
import logging

# Add Pulse/ and project root to sys.path
for _p in (_REPO_ROOT, _REPO_ROOT.parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

# These top-level directory names can cause import conflicts when loading
# multiple agents in the same process.
CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry",
]


def _unload_conflicting_modules() -> None:
    """Forcibly unload modules that cause namespace collisions between repositories."""
    to_delete = []
    for mod_name in CONFLICTING_MODULES:
        for m in list(_sys.modules.keys()):
            if m == mod_name or m.startswith(mod_name + "."):
                to_delete.append(m)
    for m in to_delete:
        _sys.modules.pop(m, None)


def _load(folder: str, rel: str, cls: str, **kw):
    root = _REPO_ROOT.parent / folder
    path_added = False
    try:
        if str(root) not in _sys.path:
            _sys.path.insert(0, str(root))
            path_added = True
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", root / rel)
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        logging.getLogger("pulse.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added and str(root) in _sys.path:
            _sys.path.remove(str(root))


def _load_pulse_agent(chronicle_client):
    """
    Path-explicit loader for PulseAgent — immune to sys.path ordering on any OS.
    Uses a namespaced module key so it never collides with Chronicle's 'agents' package.
    """
    pulse_agent_path = _REPO_ROOT / "agents" / "pulse_agent.py"

    # Re-pin Pulse/ to sys.path[0 AFTER _load() has run
    if str(_REPO_ROOT) in _sys.path:
        _sys.path.remove(str(_REPO_ROOT))
    _sys.path.insert(0, str(_REPO_ROOT))

    ns_key = "Pulse_PulseAgent"
    spec = importlib.util.spec_from_file_location(ns_key, pulse_agent_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot find {pulse_agent_path}")
    module = importlib.util.module_from_spec(spec)
    _sys.modules[ns_key] = module
    spec.loader.exec_module(module)
    return getattr(module, "PulseAgent")(chronicle_client=chronicle_client)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    # Log the active LLM mode so the user can confirm it was read
    llm_mode = _os.getenv("PULSE_LLM_MODE") or _os.getenv("ORACLE_LLM_MODE") or "full"
    logging.getLogger("pulse.main").info(
        "LLM mode: %s  (set PULSE_LLM_MODE=essential_only in .env to disable advisory calls)",
        llm_mode,
    )

    chronicle = _load(
        "Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
        storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"),
    )
    _unload_conflicting_modules()

    agent = _load_pulse_agent(chronicle)
    agent.start()

    avail = [n for n, ok in agent.engine.stats()["collectors"].items() if ok]
    print("=" * 64)
    print("  PULSE - Institutional Social Intelligence Desk")
    print("  Multi-platform. Authenticity-weighted. Manipulation-flagged.")
    print("=" * 64)
    print(f"  Platforms : {avail}")
    print(f"  Chronicle : {chronicle is not None}")
    print(f"  Brain     : {agent.has_brain}")
    print(f"  LLM mode  : {llm_mode}")
    print("  Commands  : report [topics] | symbol <SYM> | trends [topics]")
    print("              manipulation [SYM] | status | quit")
    print()

    while True:
        try:
            line = input("Pulse> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd   = parts[0]

            if cmd == "report":
                topics = parts[1:] or None
                print(json.dumps(
                    agent.act("social.report", {"topics": topics, "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "symbol" and len(parts) >= 2:
                print(json.dumps(
                    agent.act("social.sentiment", {"symbol": parts[1], "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "trends":
                topics = parts[1:] or None
                print(json.dumps(
                    agent.act("social.trends", {"topics": topics, "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "manipulation":
                sym = parts[1] if len(parts) > 1 else None
                print(json.dumps(
                    agent.act("social.manipulation", {"symbol": sym, "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            elif cmd == "llmstats":
                # Convenience: show LLM gate counters
                if agent.llm:
                    print(json.dumps(agent.llm.stats(), indent=2))
                else:
                    print("No LLM configured.")
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
