"""
Sentinel - News Intelligence  (institutional, main entry point)
==============================================================
Constitutional Name: Sentinel  (formerly NewsIntel)
Mission: Acquire, validate, cluster, and distribute credible news intelligence.
(Book I Part IV Article VII; Book II Ch IV.)

An institutional news desk. It pulls from many wires in parallel (key-free RSS
from Reuters/CNBC/BBC/FT/MarketWatch, GDELT, Hacker News; plus NewsAPI when a
key is set), scores source CREDIBILITY and MISINFORMATION risk, measures
cross-source CORROBORATION, clusters articles into ranked EVENTS, and reports
credibility-weighted per-symbol sentiment. It chooses its acquisition path via
the reasoning loop. Offline, it says so honestly rather than inventing news.

Optional key (degrades honestly if absent):
    NEWSAPI_KEY   unlocks the premium_api path (free tier at newsapi.org)

Run:
    python main.py

Commands:
    report [topics...]        full intelligence report (auto path choice)
    symbol <SYM>              credibility-weighted sentiment for a symbol
    credibility [topics...]   per-article credibility + misinformation flags
    events [topics...]        clustered events, ranked by importance
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

from agents.sentinel_agent import SentinelAgent  # type: ignore


def _try_chronicle():
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _try_chronicle()
    agent = SentinelAgent(chronicle_client=chronicle)
    agent.start()

    avail = [n for n, ok in agent.engine.stats()["collectors"].items() if ok]
    print("=" * 64)
    print("  SENTINEL - Institutional News Desk")
    print("  Multi-wire. Credibility-scored. Misinformation-flagged. Corroborated.")
    print("=" * 64)
    print(f"  Collectors available: {avail} | NewsAPI: {'newsapi' in avail} | Chronicle: {chronicle is not None}")
    print("  Commands: report [topics] | symbol <SYM> | credibility [topics] | events [topics] | status | quit")

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
                print(json.dumps(agent.act("news.report", {"topics": topics, "_sender": "user"}), indent=2))
            elif cmd == "symbol" and len(parts) >= 2:
                print(json.dumps(agent.act("news.sentiment", {"symbol": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "credibility":
                topics = parts[1:] or None
                print(json.dumps(agent.act("news.credibility", {"topics": topics, "_sender": "user"}), indent=2))
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
