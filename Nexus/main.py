"""
Nexus - Ecosystem Coordination  (institutional, main entry point)
================================================================
Constitutional Name: Nexus  (formerly Universal AI)
Mission: Route, orchestrate in parallel under SLAs, resolve conflicts by
         confidence, and learn how the whole civilization cooperates.
(Book II Part I; Book II Part II Ch VIII.)

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

FIX LOG (nexus-full-fix-v1):
  FIX-1  _extract_summary(): result.get("result", {}) -> result.get("result")
         Removes {} default so absent key returns None, not {}.
         isinstance(None, dict) is False -> recursion stops. This was the
         root cause of "RecursionError: maximum recursion depth exceeded"
         for EVERY query including "hello".
  FIX-2  _extract_summary(): same fix for result.get("session", {}) -> result.get("session")
  FIX-3  except block: bare print(f"Error: {exc}") -> traceback.print_exc()
         Full stack trace now visible on errors.
  FIX-4  Added _print_result() helper for human-readable output (non-JSON mode).
  FIX-5  Added ' --json' suffix support to any query for raw JSON output.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent
_ECO_ROOT = _REPO_ROOT.parent
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

_load_dotenv_early = load_dotenv_early
_unload_conflicting_modules = unload_conflicting_modules

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
    path_added = str(root) not in sys.path
    if path_added:
        sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # type: ignore
        return getattr(m, cls)
    except Exception as exc:
        logging.getLogger("nexus").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added and str(root) in sys.path:
            sys.path.remove(str(root))


def boot():
    log = logging.getLogger("nexus")
    _unload_conflicting_modules()
    chronicle = None
    Ch = _load_class("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent")
    if Ch:
        try:
            chronicle = Ch(storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
            chronicle.start()
        except Exception as exc:
            log.warning("Chronicle failed: %s", exc)
    _unload_conflicting_modules()
    atlas = None
    At = _load_class("Atlas", "agents/research_agent.py", "AtlasAgent")
    if At:
        try:
            atlas = At(chronicle_client=chronicle)
            atlas.start()
        except Exception:
            atlas = None
    _unload_conflicting_modules()
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
        _unload_conflicting_modules()
    return nexus


# ---------------------------------------------------------------------------
# FIX-1 / FIX-2: _extract_summary — remove {} defaults to break infinite recursion
# ---------------------------------------------------------------------------

def _extract_summary(result: dict) -> Optional[str]:
    """
    Walk a result dict looking for a human-readable summary string.

    ROOT CAUSE OF RECURSION BUG:
      result.get("result", {}) returns {} when the key is absent.
      isinstance({}, dict) is True -> _extract_summary({}) called forever.

    FIX: result.get("result") returns None when absent.
      isinstance(None, dict) is False -> recursion stops immediately.
    """
    if not isinstance(result, dict):
        return None

    # 1. Prefer a structured "report" dict
    report = result.get("report")
    if isinstance(report, dict):
        parts = []
        if report.get("summary"):
            parts.append(str(report["summary"]))
        findings = report.get("findings") or []
        if isinstance(findings, list):
            parts.extend(str(f) for f in findings[:3] if f)
        if parts:
            return "\n".join(parts)

    # 2. Recurse into a nested "result" dict
    # FIX-1: was result.get("result", {}) — the {} default caused infinite recursion
    inner = result.get("result")
    if isinstance(inner, dict):
        return _extract_summary(inner)

    # 3. Check a "session" synthesis
    # FIX-2: was result.get("session", {}) — same pattern, fixed for consistency
    session = result.get("session")
    if isinstance(session, dict) and session.get("synthesis"):
        return str(session["synthesis"])
    if isinstance(session, dict) and session.get("summary"):
        return str(session["summary"])

    # 4. Plain text fields (only for non-error results)
    for key in ("text", "answer", "message", "summary"):
        val = result.get(key)
        if val and isinstance(val, str) and result.get("status") != "error":
            return val

    return None


def _print_result(result: dict, use_json: bool) -> None:
    """Pretty-print a routing result to the terminal."""
    if use_json:
        print(json.dumps(result, indent=2, default=str))
        return

    status = result.get("status", "unknown")
    strategy = result.get("_strategy") or result.get("_reasoning", {}).get("chosen", "")
    routed = result.get("routed_to", "")
    priority = result.get("priority", "")

    header_parts = [f"[{status.upper()}]"]
    if strategy:
        header_parts.append(f"via {strategy}")
    if routed:
        header_parts.append(f"-> {routed}")
    if priority:
        header_parts.append(f"(priority {priority})")
    print(" ".join(header_parts))

    summary = _extract_summary(result)
    if summary:
        print(summary)
    elif status == "error":
        msg = result.get("message", "")
        if msg:
            print(f"  Error: {msg}")
    else:
        compact = json.dumps(result, default=str)
        print(compact[:500] + ("..." if len(compact) > 500 else ""))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    nexus = boot()

    print("=" * 64)
    print("  NEXUS - Institutional Coordinator")
    print("  SLAs. Circuit breakers. Parallel orchestration. Learned collaboration.")
    print("=" * 64)
    print(f"  Live agents: {list(nexus.registry.all().keys())}")
    print("  Commands: <query> | urgent <query> | classify <q> | agents | breakers | execution | monitor | quit")
    print("  Tip: append ' --json' to any query for raw JSON output")

    while True:
        try:
            line = input("Nexus> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break

            use_json = line.endswith(" --json")
            if use_json:
                line = line[:-7].strip()

            if line.startswith("classify "):
                result = nexus.act("domain.classify", {"query": line[9:], "_sender": "user"})
                print(json.dumps(result, indent=2, default=str))
            elif line.startswith("urgent "):
                result = nexus.act("ecosystem.route",
                    {"query": line[7:], "priority": 2, "_sender": "user"})
                _print_result(result, use_json)
            elif line == "agents":
                print(json.dumps(nexus.registry.health_summary(), indent=2, default=str))
            elif line == "breakers":
                print(json.dumps(nexus.executor.breaker_states(), indent=2, default=str))
            elif line == "execution":
                result = nexus.act("execution.stats", {"_sender": "user"})
                print(json.dumps(result, indent=2, default=str))
            elif line == "monitor":
                result = nexus.act("ecosystem.monitor", {"_sender": "user"})
                print(json.dumps(result, indent=2, default=str))
            else:
                result = nexus.act("ecosystem.route", {"query": line, "_sender": "user"})
                _print_result(result, use_json)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            # FIX-3: was print(f"Error: {exc}") — now shows full traceback
            traceback.print_exc()

    nexus.stop()
    print("Nexus shutdown complete.")


if __name__ == "__main__":
    main()
