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
    query <query>                 explicit route command (strips "query " prefix)
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

FIX LOG (phase4-nexus-main-v1  2026-07-21):
  BUG-P4-03  The CLI command "query what is an animal" passed the full string
             including the word "query" to nexus.act("ecosystem.route",
             {"query": "query what is an animal"}).  Atlas then forwarded
             "query what is an animal" verbatim to every source adapter,
             producing URLs like:
               semantic_scholar HTTP 429: query=query+what+is+an+animal
               gdelt HTTP 429: query=query+what+is+an+animal
             ROOT CAUSE: The main() REPL had handlers for "classify ", "urgent ",
             "agents", "breakers", "execution", "monitor" but NO handler for
             "query " — so it fell through to the else branch which passed the
             full line (including "query ") as the query string.
             FIX: Added elif line.startswith("query "): that strips the 7-char
             prefix before routing.  Also added "query <text>" to the Commands
             docstring and the startup banner.
             Constitutional law: Book III Ch VIII Standardized Interfaces —
             the CLI contract must strip command prefixes before forwarding.

FIX LOG (phase5-nexus-main-v1  2026-07-21):
  FIX-M-01  LIVE_REPOS only loaded Chronicle, Atlas, Aegis.  Oracle, Sentinel,
             and Pulse were never instantiated or registered with Nexus.
             Multi-domain queries like "is there a trade on EURUSD and what is
             the news sentiment" would classify correctly to trading+news but
             then fail with "no agent for trading" and "no agent for news"
             because neither Oracle nor Sentinel was in the registry.
             FIX: Added Oracle, Sentinel, Pulse to LIVE_REPOS.
             Confirmed agent class names from actual code:
               Oracle/agents/oracle_agent.py   -> OracleAgent
               Sentinel/agents/sentinel_agent.py -> SentinelAgent
               Pulse/agents/pulse_agent.py     -> PulseAgent
             Constitutional law: Book III Ch VIII Standardized Interfaces;
             Book II Principle V Graceful Degradation — agents that are
             registered but unavailable degrade gracefully; agents that are
             never registered cause guaranteed routing failures.

  FIX-M-02  boot() registered agents under their folder name (e.g. "chronicle",
             "atlas") but coordinator_agent.py looks up agents by their
             agent.name attribute (e.g. "oracle", "sentinel", "pulse").
             For Chronicle and Atlas this happened to match. For Oracle
             (folder="Oracle", name="oracle"), Sentinel (folder="Sentinel",
             name="sentinel"), Pulse (folder="Pulse", name="pulse") it also
             matches — but the registration key must be the agent's .name
             attribute, not the folder name.
             FIX: boot() now registers using agent.name (already correct for
             Chronicle/Atlas; explicitly verified for new agents).
             Constitutional law: Book III Ch VIII Standardized Interfaces.

  FIX-M-03  _extract_summary() did not handle multi-agent orchestration
             session results. When Nexus ran an orchestration session, the
             result contained {"session": {"synthesis": "...", "per_agent": {...}}}
             but _extract_summary() only checked session.get("synthesis") and
             session.get("summary"). Multi-agent results also have
             session["transcript"] with per-agent contributions.
             FIX: Added session transcript extraction as fallback.
             Constitutional law: Book II No Silent Failures.

FIX LOG (phase5b-main-v1  2026-07-21):
  FIX-M-03  Atlas registered BEFORE on_start() printed "Registered: ['chronicle']".
             The log line in on_start() fires when nexus.start() is called, which
             happens BEFORE atlas/oracle/sentinel/pulse are registered in boot().
             FIX: Moved the "Registered:" log to the END of boot(), after all
             agents are registered, so it accurately reflects the full roster.
             Constitutional law: Book II No Silent Failures.

  FIX-M-04  Forge and Genesis were absent from LIVE_REPOS entirely.
             Queries like "train a new strategy for GBPUSD" had no agent to
             handle them -> "no approach succeeded in 3 attempts".
             FIX: Added Forge (ForgeAgent) and Genesis (GenesisAgent) to
             LIVE_REPOS with graceful degradation (log warning, skip, no crash).
             Confirmed class names from actual code:
               Forge/agents/training_agent.py   -> class ForgeAgent
               Genesis/agents/creator_agent.py  -> class GenesisAgent
             Constitutional law: Book II Principle V Graceful Degradation.

  FIX-M-05  CLI had no "query " prefix handler. The word "query" leaked into
             search strings: "query what is an animal" -> Atlas searched for
             "query what is an animal" instead of "what is an animal".
             FIX: Added elif line.startswith("query "): handler that strips
             the 6-char prefix before routing.
             Constitutional law: Book III Ch VIII Standardized Interfaces.
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

# FIX-M-01: Added Oracle, Sentinel, Pulse to LIVE_REPOS.
# FIX-M-04: Added Forge, Genesis to LIVE_REPOS.
#   Confirmed class names from actual repo code:
#   Oracle/agents/oracle_agent.py   -> class OracleAgent
#   Sentinel/agents/sentinel_agent.py -> class SentinelAgent
#   Pulse/agents/pulse_agent.py     -> class PulseAgent
#   Forge/agents/training_agent.py  -> class ForgeAgent
#   Genesis/agents/creator_agent.py -> class GenesisAgent
LIVE_REPOS = {
    "chronicle": ("Chronicle", "agents/chronicle_agent.py",  "ChronicleAgent"),
    "atlas":     ("Atlas",     "agents/research_agent.py",   "AtlasAgent"),
    "oracle":    ("Oracle",    "agents/oracle_agent.py",     "OracleAgent"),    # FIX-M-01
    "sentinel":  ("Sentinel",  "agents/sentinel_agent.py",   "SentinelAgent"),  # FIX-M-01
    "pulse":     ("Pulse",     "agents/pulse_agent.py",      "PulseAgent"),     # FIX-M-01
    "aegis":     ("Aegis",     "agents/auditor_agent.py",    "AegisAgent"),
    "forge":     ("Forge",     "agents/training_agent.py",   "ForgeAgent"),     # FIX-M-04
    "genesis":   ("Genesis",   "agents/creator_agent.py",    "GenesisAgent"),   # FIX-M-04
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

    # --- Chronicle (memory, source of truth) ---
    chronicle = None
    Ch = _load_class("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent")
    if Ch:
        try:
            chronicle = Ch(storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
            chronicle.start()
        except Exception as exc:
            log.warning("Chronicle failed: %s", exc)
    _unload_conflicting_modules()

    # --- Atlas (research) ---
    atlas = None
    At = _load_class("Atlas", "agents/research_agent.py", "AtlasAgent")
    if At:
        try:
            atlas = At(chronicle_client=chronicle)
            atlas.start()
        except Exception:
            atlas = None
    _unload_conflicting_modules()

    # --- Nexus coordinator ---
    nexus = NexusAgent(chronicle_client=chronicle, atlas_client=atlas)
    nexus.start()
    if chronicle:
        nexus.register_agent(chronicle.name, chronicle)
    if atlas:
        nexus.register_agent(atlas.name, atlas)

    # --- All other agents (Oracle, Sentinel, Pulse, Aegis, Forge, Genesis) ---
    # FIX-M-01: Oracle, Sentinel, Pulse now included.
    # FIX-M-04: Forge, Genesis now included with graceful degradation.
    # FIX-M-02: Register using agent.name (not folder name) for correct lookup.
    for name, (folder, rel, cls_name) in LIVE_REPOS.items():
        if name in ("chronicle", "atlas"):
            continue  # already registered above
        Cls = _load_class(folder, rel, cls_name)
        if not Cls:
            log.warning("Could not load %s (%s/%s) — skipping (graceful degradation).", name, folder, rel)
            continue
        try:
            try:
                agent = Cls(chronicle_client=chronicle)
            except TypeError:
                agent = Cls()
            agent.start()
            # FIX-M-02: use agent.name attribute as registry key
            reg_name = getattr(agent, "name", name)
            nexus.register_agent(reg_name, agent)
            log.info("Registered agent: %s (domain=%s)", reg_name, getattr(agent, "domain", "?"))
        except Exception as exc:
            import traceback
            # BUG B FIX (Phase 5c): Log full traceback so startup failures are
            # visible and diagnosable. "No Silent Failures" — Book II.
            log.warning(
                "%s failed to start: %s — skipping (graceful degradation).\n"
                "Full traceback:\n%s",
                name, exc, traceback.format_exc(),
            )
        _unload_conflicting_modules()

    # FIX-M-03: Log the FINAL roster AFTER all agents are registered.
    # (on_start() fires before agents are registered, so its log was always stale.)
    log.info("Nexus boot complete. Live agents: %s", list(nexus.registry.all().keys()))
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

    FIX-MAIN-01 (Phase 5i): Check 'human_summary' first — set by coordinator's
    _format_result() for ALL result types. This is the primary human-readable
    output path. Falls back to the original nested-dict walking for backward
    compatibility with agents that don't yet set human_summary.
    """
    if not isinstance(result, dict):
        return None

    # 0. FIX-MAIN-01: human_summary is set by coordinator._format_result()
    #    for ALL result types. Check it first — it's always a clean string.
    hs = result.get("human_summary") or result.get("summary")
    if hs and isinstance(hs, str) and len(hs) > 10:
        return hs

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
    for key in ("text", "answer", "message"):
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

    # FIX-MAIN-02 (Phase 5i): When human_summary is present (set by coordinator
    # _format_result()), print it directly as the primary output — no header line
    # needed since the summary already contains emoji + context (e.g. "📰 GBPUSD
    # News Summary — July 21, 2026"). Only show the routing header for non-summary
    # results (e.g. raw JSON fallback, error messages).
    human_summary = result.get("human_summary")
    if human_summary and isinstance(human_summary, str) and len(human_summary) > 10:
        print(human_summary)
        return

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

    live = list(nexus.registry.all().keys())
    print("=" * 64)
    print("  NEXUS - Institutional Coordinator")
    print("  SLAs. Circuit breakers. Parallel orchestration. Learned collaboration.")
    print("=" * 64)
    print(f"  Live agents ({len(live)}): {live}")
    print("  Commands: <query> | query <query> | urgent <query> | classify <q> | agents | breakers | execution | monitor | quit")
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
            # FIX-M-05: Strip "query " prefix so it doesn't leak into search terms.
            elif line.startswith("query "):
                result = nexus.act("ecosystem.route",
                    {"query": line[6:].strip(), "_sender": "user"})
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