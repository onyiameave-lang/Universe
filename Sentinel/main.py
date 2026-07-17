"""
Sentinel - News Intelligence  (institutional, main entry point)
==============================================================
Constitutional Name: Sentinel  (formerly NewsIntel)
Mission: Acquire, validate, cluster, and distribute credible news intelligence.
(Book I Part IV Article VII; Book II Ch IV.)

An institutional news desk. It pulls from many wires in parallel (key-free RSS
from Al Jazeera/CNBC/BBC/FT/MarketWatch, The Guardian open API, Hacker News;
plus NewsAPI when a key is set), scores source CREDIBILITY and MISINFORMATION
risk, measures cross-source CORROBORATION, clusters articles into ranked EVENTS,
and reports credibility-weighted per-symbol sentiment. It chooses its acquisition
path via the reasoning loop. Offline, it says so honestly rather than inventing
news.

Optional keys (degrade honestly if absent):
    NEWSAPI_KEY       unlocks the premium_api path (free tier at newsapi.org)
    GUARDIAN_API_KEY  higher Guardian rate limits (default: "test" key, 500/day)

Run:
    python main.py

Commands:
    report [topics...]        full intelligence report (auto path choice)
    symbol <SYM>              credibility-weighted sentiment for a symbol
    credibility [topics...]   per-article credibility + misinformation flags
    events [topics...]        clustered events, ranked by importance
    status | quit

--- FIX NOTES (sentinel-fix) ---

S-4: No dotenv in main.py
  Root cause: NEWSAPI_KEY was read from _cfg at import time of collectors.py.
  If dotenv had not been called yet, os.environ["NEWSAPI_KEY"] was empty, so
  _cfg.newsapi_key was "" and NewsAPICollector.available was always False.
  Fix: _load_dotenv_early() is called as the VERY FIRST action in main(),
  before any agent or engine is constructed. This mirrors the Pulse fix.

S-2: GDELT replaced by GuardianCollector
  GDELT API times out on every call (confirmed in live test). Replaced with
  The Guardian open API which works with api-key=test (no signup, 500 req/day).
  GuardianCollector has a 5-minute circuit-breaker.

S-1: Reuters RSS dead (502)
  Replaced with Al Jazeera all.xml (confirmed 200 ✓) in DEFAULT_FEEDS.

S-5: LLM essential=False gate in analysis.py
  See analysis.py for details.

S-7: standalone import guard in collectors.py
  See collectors.py for details.

Canonical _load() pattern (from pulse-import-fix/main.py):
  Uses importlib.util.spec_from_file_location with path_added/finally cleanup.
  Identical to the Pulse canonical version — do not simplify.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
for p in (_REPO_ROOT, _REPO_ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# These are the common top-level directory names in agent repos that can cause
# import conflicts when loading multiple agents in the same process.
CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry",
]


def _unload_conflicting_modules() -> None:
    """Remove any stale top-level module entries that a sub-agent load may have
    registered, so the next bare `from agents.xxx import ...` resolves against
    the correct repo root (this agent's own directory)."""
    for mod in list(sys.modules.keys()):
        if mod in CONFLICTING_MODULES or any(
            mod.startswith(f"{m}.") for m in CONFLICTING_MODULES
        ):
            del sys.modules[mod]


def _load_dotenv_early() -> None:
    """
    S-4 fix: load .env BEFORE any agent/engine is constructed.

    shared.config reads env vars at import time. If dotenv has not been called
    yet, NEWSAPI_KEY / GUARDIAN_API_KEY are empty strings and the collectors
    that depend on them report available=False for the entire process lifetime
    (the singleton is never re-read).

    We try python-dotenv first; fall back to a manual parser so this works
    even if python-dotenv is not installed.
    """
    env_file = _REPO_ROOT.parent / ".env"
    if not env_file.exists():
        env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=str(env_file), override=False)
        return
    except ImportError:
        pass

    # Manual fallback parser (no external dependency)
    try:
        with open(env_file, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def _load(folder: str, rel: str, cls: str, **kw):
    """
    Canonical _load() — identical to pulse-import-fix/main.py.

    Loads an agent class from another repo directory using
    importlib.util.spec_from_file_location so the import is 100% path-explicit
    and immune to sys.path ordering. The finally block always removes the
    temporarily-added path so it cannot pollute subsequent imports.
    """
    root = _REPO_ROOT.parent / folder
    path_added = False
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        spec = importlib.util.spec_from_file_location(
            f"{folder}_{cls}", root / rel
        )
        if spec is None or spec.loader is None:
            return None  # file doesn't exist or is not a module
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        logging.getLogger("sentinel.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def _load_sentinel_agent(chronicle_client):
    """
    Load SentinelAgent using the same path-explicit importlib pattern as _load().

    This is immune to sys.path ordering and works regardless of the working
    directory the user launched Python from.

    The module is registered under the namespaced key "Sentinel_SentinelAgent"
    to avoid collisions with any other repo's "agents" package.
    """
    agent_path = _REPO_ROOT / "agents" / "sentinel_agent.py"

    # Re-pin Sentinel/ to sys.path[0] so relative imports inside sentinel_agent.py
    # (e.g. from core.intelligence_engine import ...) resolve correctly.
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    else:
        sys.path.remove(str(_REPO_ROOT))
        sys.path.insert(0, str(_REPO_ROOT))

    spec = importlib.util.spec_from_file_location("Sentinel_SentinelAgent", agent_path)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot find Sentinel/agents/sentinel_agent.py at {agent_path}. "
            "Make sure the Universe-oracle project structure is intact."
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules["Sentinel_SentinelAgent"] = module
    spec.loader.exec_module(module)

    SentinelAgent = getattr(module, "SentinelAgent")
    return SentinelAgent(chronicle_client=chronicle_client)


def _try_chronicle():
    """Load ChronicleAgent from the sibling Chronicle/ repo (optional)."""
    try:
        root = _REPO_ROOT.parent / "Chronicle"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from agents.chronicle_agent import ChronicleAgent  # type: ignore
        c = ChronicleAgent(storage_dir=str(root / "memory" / "store"))
        c.start()
        return c
    except Exception:
        return None


def main():
    # S-4: MUST be first — loads NEWSAPI_KEY / GUARDIAN_API_KEY into os.environ
    # before any agent or shared.config singleton is constructed.
    _load_dotenv_early()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    # Load Chronicle (optional — degrades honestly if absent)
    chronicle = _load(
        "Chronicle",
        "agents/chronicle_agent.py",
        "ChronicleAgent",
        storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"),
    )
    _unload_conflicting_modules()

    # Load SentinelAgent path-explicitly so it is immune to sys.path ordering
    agent = _load_sentinel_agent(chronicle_client=chronicle)
    agent.start()

    avail = [n for n, ok in agent.engine.stats()["collectors"].items() if ok]
    print("=" * 64)
    print("  SENTINEL - Institutional News Desk")
    print("  Multi-wire. Credibility-scored. Misinformation-flagged. Corroborated.")
    print("=" * 64)
    print(
        f"  Collectors available: {avail} | "
        f"NewsAPI: {'newsapi' in avail} | "
        f"Guardian: {'guardian' in avail} | "
        f"Chronicle: {chronicle is not None}"
    )
    print(
        "  Commands: report [topics] | symbol <SYM> | "
        "credibility [topics] | events [topics] | status | quit"
    )

    while True:
        try:
            line = input("Sentinel> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "report":
                topics = parts[1:] or None
                print(json.dumps(
                    agent.act("news.report", {"topics": topics, "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "symbol" and len(parts) >= 2:
                print(json.dumps(
                    agent.act("news.sentiment", {"symbol": parts[1], "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "credibility":
                topics = parts[1:] or None
                print(json.dumps(
                    agent.act("news.credibility", {"topics": topics, "_sender": "user"}),
                    indent=2,
                ))
            elif cmd == "events":
                topics = parts[1:] or None
                out = agent.act("news.report", {"topics": topics, "_sender": "user"})
                rep = out.get("report") or {}
                print(json.dumps(rep.get("top_events", []), indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: report forex inflation")

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    if chronicle:
        chronicle.stop()
    print("Sentinel shutdown complete.")


if __name__ == "__main__":
    main()
# S-13: "gdelt" renamed to "guardian" in collectors.py (S-2). Update all
# three paths so Guardian actually fires when wire_priority or broad_sweep
# is chosen. Previously Guardian was always skipped because the registry
# looked up "gdelt" which no longer exists.
PATH_SOURCES = {
    "wire_priority": ["rss", "guardian"],
    "premium_api":   ["newsapi", "rss"],
    "broad_sweep":   ["rss", "newsapi", "guardian", "hackernews"],
}