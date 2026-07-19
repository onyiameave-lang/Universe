"""
Aegis - Governance & Auditing  (institutional, main entry point)
===============================================================
Constitutional Name: Aegis  (formerly Auditor)
Mission: Continuously govern, score risk, detect anomalies, and self-heal.
(Book VI Human Constitution; Book III Ch XIII; Book I Article XII.)

An institutional control function: policy-as-data rulebook, a time-decayed risk
register with trend detection, statistical anomaly detection, continuous
background monitoring, risk-weighted (not count-based) threat response with
LEARNED thresholds, and the self-healing remediation ladder (fixes never
auto-applied; human confirms). Hash-chained tamper-evident audit log.

Run:
    python main.py

Commands:
    selfaudit                     compliance + security sweep of the ecosystem
    audit <repo> <action>         record an audited action (risk-weighted response)
    threat <agent> <n>            simulate n violations -> watch exposure escalate
    anomaly <agent> <lat> <ok>    feed a behavioral observation (repeat to baseline)
    risk                          the risk register (exposure + trends)
    policies                      the policy-as-data rulebook
    monitor <repos...>            start continuous monitoring of repos
    alerts                        recent monitor alerts
    verify | health | status | quit
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

# B-11/12 fix: import shared utilities instead of duplicating them here
from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

# Keep local aliases so the rest of this file's call-sites are unchanged
_load_dotenv_early = load_dotenv_early
_unload_conflicting_modules = unload_conflicting_modules

from agents.auditor_agent import AegisAgent  # type: ignore

REPO_NAMES = ["Chronicle", "Oracle", "Nexus", "Sentinel", "Pulse", "Atlas", "Forge", "Genesis", "Aegis"]



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
        logging.getLogger("aegis.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def self_audit(agent: AegisAgent) -> dict:
    eco_root = _REPO_ROOT.parent
    report = {"repositories": {}, "summary": {}}
    total, n = 0.0, 0
    for repo in REPO_NAMES:
        rp = eco_root / repo
        if not rp.exists():
            report["repositories"][repo] = {"status": "missing"}
            continue
        present = [d.name for d in rp.iterdir() if d.is_dir()]
        manifest = {}
        mp = rp / "repository.json"
        if mp.exists():
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
        comp = agent.compliance.check_repository(repo, manifest, present)
        sec = agent.security.scan_directory(str(rp))
        report["repositories"][repo] = {"compliance": comp["status"], "score": comp["score"],
                                       "security_risk": sec.get("risk_level"),
                                       "findings": sec.get("total_findings", 0)}
        total += comp["score"]; n += 1
    report["summary"] = {"repositories_audited": n,
                        "avg_compliance": round(total / n, 3) if n else 0.0,
                        "audit_chain": agent.audit_log.verify_integrity()}
    return report


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    # The `from agents.auditor_agent import AegisAgent` import above already
    # registered Aegis's own "core" package in sys.modules. Clear that out
    # before loading any peer repo, or their "from core.X import Y" imports
    # will silently resolve against Aegis's core/ instead of their own.
    _unload_conflicting_modules()

    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                      storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"))
    _unload_conflicting_modules()

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    agent = AegisAgent(chronicle_client=chronicle, atlas_client=atlas)
    agent.start()

    print("=" * 64)
    print("  AEGIS - Institutional Control Function")
    print("  Policy-as-data. Risk register. Anomaly detection. Continuous monitor.")
    print("=" * 64)
    print(f"  Policies: {agent.policies.stats()['total_policies']} | Chain intact:"
          f" {agent.audit_log.verify_integrity().get('intact')} | Atlas: {atlas is not None}")
    print("  Commands: selfaudit | audit <r> <a> | threat <agent> <n> | anomaly <agent> <lat> <ok> |")
    print("            risk | policies | monitor <repos> | alerts | verify | health | quit")

    while True:
        try:
            line = input("Aegis> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "selfaudit":
                print(json.dumps(self_audit(agent), indent=2))
            elif cmd == "audit" and len(parts) >= 3:
                print(json.dumps(agent.act("audit.action",
                    {"repository": parts[1], "agent": parts[1], "action": parts[2],
                     "action_context": {"silent": True} if parts[2] == "silent_failure" else {},
                     "_sender": "user"}), indent=2))
            elif cmd == "threat" and len(parts) >= 3:
                name, nn = parts[1], int(parts[2])
                out = {}
                for _ in range(nn):
                    out = agent.act("audit.action", {"repository": name, "agent": name,
                        "action": "silent_failure", "action_context": {"silent": True}, "_sender": "user"})
                if out.get("status") == "error":
                    print(f"ERROR on final call: {out.get('message')}")
                    if out.get("trace"):
                        print(out["trace"])
                else:
                    print(f"After {nn} violations -> exposure={out.get('risk_exposure')}")
                    print(json.dumps(out.get("response", {}), indent=2))
            elif cmd == "anomaly" and len(parts) >= 4:
                print(json.dumps(agent.act("anomaly.observe",
                    {"agent": parts[1], "latency_ms": float(parts[2]),
                     "success": parts[3].lower() in ("ok", "true", "1"), "_sender": "user"}), indent=2))
            elif cmd == "risk":
                print(json.dumps(agent.act("risk.register", {"_sender": "user"}), indent=2))
            elif cmd == "policies":
                print(json.dumps(agent.act("policy.list", {"_sender": "user"}), indent=2))
            elif cmd == "monitor":
                repos = parts[1:] or ["Chronicle", "Atlas", "Aegis"]
                print(json.dumps(agent.act("monitor.start", {"repos": repos, "_sender": "user"}), indent=2))
            elif cmd == "alerts":
                print(json.dumps(agent.act("monitor.alerts", {"_sender": "user"}), indent=2))
            elif cmd == "verify":
                print(json.dumps(agent.act("audit.verify", {"_sender": "user"}), indent=2))
            elif cmd == "health":
                print(json.dumps(agent.get_status()["health"], indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: threat rogue 6")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    for peer in (atlas, chronicle):
        if peer:
            try:
                peer.stop()
            except Exception:
                pass
    print("Aegis shutdown complete. Audit log persisted.")


if __name__ == "__main__":
    main()