"""
Pulse - Social Intelligence  (Universe-oracle social-upgrade v6)
================================================================
Multi-category, region-aware institutional social intelligence desk.

New REPL commands vs v4/v5:
    report [category] [topics...]   full report, optionally filtered by category
                                    e.g. "report finance" or "report nigeria crypto"
    regional                        Nigerian / regional trending topics
    trends [topics...]              trending symbols + per-category breakdown
    symbol <SYM>                    authenticity-weighted sentiment for a symbol
    manipulation [SYM]              coordinated-manipulation check
    status                          collector status + per-category post counts
    quit

Category names (case-insensitive):
    finance  tech  entertainment  sports  politics  regional  general

Env vars:
    PULSE_USER_REGION       ISO country code (default "NG" = Nigeria)
    PULSE_LLM_MODE          "full" | "essential_only" (default "full")
    ORACLE_LLM_MODE         fallback if PULSE_LLM_MODE not set
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
for p in (_REPO_ROOT, _REPO_ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ── Load .env FIRST — before any other import that reads env vars ─────────────
def _load_dotenv_early() -> None:
    """
    Load .env from the Pulse directory or any parent up to the repo root.
    Must run before any module that reads os.getenv() at import time
    (shared/config.py, shared/llm/client.py, collectors.py, etc.).
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        # Search: Pulse/.env → Universe-oracle-vN/.env → parent/.env
        for candidate in [
            _REPO_ROOT / ".env",
            _REPO_ROOT.parent / ".env",
            _REPO_ROOT.parent.parent / ".env",
        ]:
            if candidate.exists():
                load_dotenv(candidate, override=False)
                logging.getLogger("pulse.main").info(
                    "Loaded .env from %s", candidate)
                break
    except ImportError:
        pass  # python-dotenv not installed — env vars must be set externally


_load_dotenv_early()

# ── Log LLM mode immediately so user can see it ───────────────────────────────
_LLM_MODE = os.getenv("PULSE_LLM_MODE",
                       os.getenv("ORACLE_LLM_MODE", "full")).lower()
_REGION   = os.getenv("PULSE_USER_REGION", "NG").upper()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(message)s")
logging.getLogger("pulse.main").info(
    "LLM mode: %s | Region: %s", _LLM_MODE, _REGION)


# ── Module conflict cleanup (same as v4) ──────────────────────────────────────
CONFLICTING_MODULES = [
    "core", "agents", "intelligence", "memory", "research", "models",
    "training", "optimization", "communication", "infrastructure",
    "security", "api", "interfaces", "dashboard", "testing", "benchmarks",
    "simulations", "datasets", "documentation", "configs", "logs",
    "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry",
]


def _unload_conflicting_modules() -> None:
    to_del = []
    for mod in CONFLICTING_MODULES:
        for m in list(sys.modules):
            if m == mod or m.startswith(mod + "."):
                to_del.append(m)
    for m in to_del:
        sys.modules.pop(m, None)


def _load(folder, rel, cls, **kw):
    import importlib.util
    root = _REPO_ROOT.parent / folder
    path_added = False
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        spec = importlib.util.spec_from_file_location(
            f"{folder}_{cls}", root / rel)
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except (ImportError, AttributeError, FileNotFoundError) as exc:
        logging.getLogger("pulse.main").warning(
            "load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


# ── REPL helpers ──────────────────────────────────────────────────────────────
_VALID_CATEGORIES = {
    "finance", "tech", "technology", "entertainment",
    "sports", "politics", "regional", "general",
}


def _parse_report_args(parts: list) -> tuple:
    """
    Parse `report [category] [topics...]` arguments.
    Returns (category_filter, topics_list).
    """
    if not parts:
        return None, None
    cat    = None
    topics = []
    for i, p in enumerate(parts):
        if p.lower() in _VALID_CATEGORIES:
            cat    = p
            topics = parts[i + 1:] or None
            break
        else:
            topics.append(p)
    return cat, topics or None


def main() -> None:
    chronicle = _load(
        "Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
        storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"),
    )
    _unload_conflicting_modules()

    from agents.pulse_agent import PulseAgent  # type: ignore

    agent = PulseAgent(chronicle_client=chronicle)
    agent.start()

    stats = agent.engine.stats()
    avail = [n for n, ok in stats["collectors"].items() if ok]

    print("=" * 70)
    print("  PULSE v6 — Multi-Category Social Intelligence Desk")
    print(f"  Region: {_REGION} | LLM mode: {_LLM_MODE}")
    print("=" * 70)
    print(f"  Platforms: {avail}")
    print(f"  Chronicle: {chronicle is not None} | Brain: {agent.has_brain}")
    print()
    print("  Commands:")
    print("    report [category] [topics...]  — full report")
    print("    regional                       — Nigerian/regional trending")
    print("    trends [topics...]             — trending + per-category")
    print("    symbol <SYM>                   — symbol sentiment")
    print("    manipulation [SYM]             — manipulation check")
    print("    status                         — collector status")
    print("    quit")
    print()
    print("  Categories: finance  tech  entertainment  sports  politics  regional")
    print("=" * 70)

    while True:
        try:
            line = input("Pulse> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break

            parts = line.split()
            cmd   = parts[0].lower()

            if cmd == "report":
                cat, topics = _parse_report_args(parts[1:])
                ctx = {"topics": topics, "_sender": "user"}
                if cat:
                    ctx["category"] = cat
                print(json.dumps(
                    agent.act("social.report", ctx), indent=2))

            elif cmd == "regional":
                print(json.dumps(
                    agent.act("social.regional",
                               {"_sender": "user"}), indent=2))

            elif cmd == "symbol" and len(parts) >= 2:
                print(json.dumps(
                    agent.act("social.sentiment",
                               {"symbol": parts[1], "_sender": "user"}),
                    indent=2))

            elif cmd == "trends":
                topics = parts[1:] or None
                print(json.dumps(
                    agent.act("social.trends",
                               {"topics": topics, "_sender": "user"}),
                    indent=2))

            elif cmd == "manipulation":
                sym = parts[1] if len(parts) > 1 else None
                print(json.dumps(
                    agent.act("social.manipulation",
                               {"symbol": sym, "_sender": "user"}),
                    indent=2))

            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))

            else:
                print("Unknown command.")
                print("Try: report | report finance | report nigeria | "
                      "regional | trends | symbol BTCUSD | status")

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    print("Pulse shutdown complete.")


if __name__ == "__main__":
    main()
