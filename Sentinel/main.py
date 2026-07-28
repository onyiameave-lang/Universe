"""
Sentinel - Institutional News Intelligence Desk (main entry point)
====================================================================
Constitutional Name: Sentinel (formerly NewsIntel)
Mission: Acquire, validate, cluster, and distribute credible news
         intelligence.

REPLACES A COPY-PASTE BUG: this file previously was a near-verbatim copy of
Nexus/main.py (the ecosystem coordinator's CLI), left over from when Sentinel
was wired into the ecosystem. It imported `from agents.coordinator_agent
import NexusAgent` -- a module that only exists under Nexus/agents/, not
Sentinel/agents/ (which only has sentinel_agent.py / class SentinelAgent).
Running `python main.py` from Sentinel/ raised:
    ModuleNotFoundError: No module named 'agents.coordinator_agent'
This file now follows the same single-repo entry-point pattern as
Atlas/main.py and Oracle/main.py: boot Sentinel's own agent (with Chronicle
as its only dependency, matching SentinelAgent.__init__(chronicle_client=...)),
and expose a CLI over Sentinel's own capabilities.

Run:
    python main.py

Commands:
    report [topics...]      full news report (wire-priority path, falls back
                             to broad sweep) -- topics optional, e.g.
                             "report EURUSD inflation"
    sentiment <symbol>      sentiment analysis for a symbol/topic
    events [topics...]      alias for report (same underlying task)
    credibility [topics...] per-article credibility + misinformation risk
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

from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

_load_dotenv_early = load_dotenv_early
_unload_conflicting_modules = unload_conflicting_modules

from agents.sentinel_agent import SentinelAgent  # type: ignore


def _try_chronicle():
    try:
        chron_root = _REPO_ROOT.parent / "Chronicle"
        if str(chron_root) not in sys.path:
            sys.path.insert(0, str(chron_root))

        _unload_conflicting_modules()

        from agents.chronicle_agent import ChronicleAgent  # type: ignore
        c = ChronicleAgent(storage_dir=str(chron_root / "memory" / "store"))
        c.start()

        _unload_conflicting_modules()

        # FIX: sys.modules gets purged above, but chron_root itself was
        # never removed from sys.path — it would sit there for the rest of
        # the process, permanently ahead of Sentinel's own directory. Any
        # LATER deferred/lazy import of a name in CONFLICTING_MODULES
        # (e.g. "intelligence", "core") would then incorrectly resolve
        # against Chronicle's directory instead of Sentinel's own, since
        # sys.path order — unlike sys.modules — was never restored. This
        # is exactly what broke Sentinel's own "intelligence.term_reliability"
        # import after Chronicle loaded. Removing it here restores
        # Sentinel's own directory as the effective "home" for imports.
        try:
            sys.path.remove(str(chron_root))
        except ValueError:
            pass

        return c
    except Exception:
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    _load_dotenv_early()

    chronicle = _try_chronicle()
    agent = SentinelAgent(chronicle_client=chronicle)
    agent.start()

    print("=" * 64)
    print("  SENTINEL - Institutional News Intelligence Desk")
    print("  Acquire. Validate. Cluster. Distribute credible intelligence.")
    print("=" * 64)
    print(f"  Chronicle: {chronicle is not None} | Brain: {agent.has_brain}")
    print("  Commands: report [topics] | sentiment <symbol> | events [topics] |")
    print("            credibility [topics] | status | suggestions | approve <i> | dismiss <i> | quit")

    while True:
        try:
            line = input("\nSentinel> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break

            parts = line.split()
            cmd = parts[0]
            rest = line[len(cmd):].strip()
            topics = rest.split() if rest else None

            if cmd in ("report", "events"):
                print(json.dumps(agent.act("news.report",
                    {"topics": topics, "_sender": "user"}), indent=2))

            elif cmd == "sentiment" and len(parts) >= 2:
                symbol = parts[1].upper()
                print(json.dumps(agent.act("news.sentiment",
                    {"symbol": symbol, "_sender": "user"}), indent=2))

            elif cmd == "credibility":
                print(json.dumps(agent.act("news.credibility",
                    {"topics": topics, "_sender": "user"}), indent=2))

            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))

            elif cmd == "suggestions":
                # Tier 0 mechanism 2 (LLM-as-teacher vocabulary discovery):
                # review candidate keywords queued when Deep Analysis caught
                # something the lexical classifier missed. Never
                # auto-applied — approve/dismiss explicitly below.
                from intelligence.term_reliability import get_tracker  # type: ignore
                pending = get_tracker().get_suggested_terms()
                if not pending:
                    print(" No pending suggestions.")
                else:
                    print(f"\n {len(pending)} pending suggestion(s):")
                    for i, s in enumerate(pending):
                        print(f"  [{i}] {s['term']!r} -> {s['event_type']}   "
                              f"(from: \"{s['example'][:70]}\")")
                    print(" Use: approve <index>  |  dismiss <index>")

            elif cmd == "approve" and len(parts) >= 2:
                from intelligence.term_reliability import get_tracker  # type: ignore
                tracker = get_tracker()
                pending = tracker.get_suggested_terms()
                try:
                    idx = int(parts[1])
                    term = pending[idx]["term"]
                except (ValueError, IndexError):
                    print(f" No suggestion at index {parts[1]!r}. Run 'suggestions' to see valid indices.")
                else:
                    approved = tracker.approve_suggestion(term)
                    if approved:
                        print(f" Approved {term!r} for event_type={approved['event_type']!r}. "
                              f"classify_event() will now recognize it.")
                    else:
                        print(f" Could not find suggestion {term!r} (already handled?).")

            elif cmd == "dismiss" and len(parts) >= 2:
                from intelligence.term_reliability import get_tracker  # type: ignore
                tracker = get_tracker()
                pending = tracker.get_suggested_terms()
                try:
                    idx = int(parts[1])
                    term = pending[idx]["term"]
                except (ValueError, IndexError):
                    print(f" No suggestion at index {parts[1]!r}. Run 'suggestions' to see valid indices.")
                else:
                    tracker.dismiss_suggestion(term)
                    print(f" Dismissed {term!r}.")

            else:
                print(" Unknown command. Try: report EURUSD inflation")

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f" Error: {exc}")

    agent.stop()
    if chronicle:
        chronicle.stop()
    print("\nSentinel shutdown complete.")


if __name__ == "__main__":
    main()