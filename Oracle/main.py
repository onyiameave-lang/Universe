"""
Oracle - Autonomous Quantitative Research Laboratory
====================================================
Constitutional Name: Oracle  (formerly MarketOracle)
Mission: Validate trading intelligence scientifically and preserve reusable evidence.
(Book I Article X, XIII; Book VI capital sovereignty.)

Oracle forms hypotheses, consults Chronicle, escalates research dead ends to
Atlas, validates strategy genomes by regime, and preserves every experiment.

Run:
    python main.py

Commands:
    signal <SYM>          adaptive-fused signal (uses evolved champion if any)
    evolve <SYM> [gens]   run a full scientific research cycle
    research <SYM> [gens] alias for evolve
    hypotheses <SYM>      generate regime-aware hypotheses
    champion <SYM>        show the regime-aware champion
    backtest <SYM>        walk-forward validation
    propose <SYM>         risk-gated trade plan
    learn <SYM> <+1/-1>   feed a realized outcome so fusion weights adapt
    portfolio | status | quit
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
        logging.getLogger("oracle.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                      storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"))
    _unload_conflicting_modules()

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
    _unload_conflicting_modules()

    sentinel = _load("Sentinel", "agents/sentinel_agent.py", "SentinelAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    pulse = _load("Pulse", "agents/pulse_agent.py", "PulseAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    from agents.oracle_agent import OracleAgent  # type: ignore

    agent = OracleAgent(chronicle_client=chronicle, sentinel_client=sentinel,
                       pulse_client=pulse, atlas_client=atlas)
    agent.start()

    print("=" * 64)
    print("  ORACLE - Autonomous Quantitative Research Laboratory")
    print("  Hypotheses. Research escalation. Regime champions. Preserved evidence.")
    print("=" * 64)
    print(f"  Paper trading: {agent.risk.paper} | Sentinel:{sentinel is not None} "
          f"Pulse:{pulse is not None} Chronicle:{chronicle is not None}")
    print("  Commands: signal <S> | evolve <S> [gens] | research <S> [gens] | hypotheses <S> |")
    print("            champion <S> | backtest <S> | propose <S> | learn <S> <+1/-1> | portfolio | status | quit")

    last_streams = {}
    while True:
        try:
            line = input("Oracle> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "signal" and len(parts) >= 2:
                print(json.dumps(agent.act("trade.signal", {"symbol": parts[1], "_sender": "user"}), indent=2))
            elif cmd in ("evolve", "research") and len(parts) >= 2:
                gens = int(parts[2]) if len(parts) > 2 else 5
                print(f"Running scientific research cycle for {parts[1]} over {gens} generations...")
                out = agent.act("strategy.evolve", {"symbol": parts[1], "generations": gens, "_sender": "user"})
                evo = out.get("evolution", {})
                experiment = out.get("experiment", {})

                in_sample_return = evo.get("in_sample_return")
                oos_results = evo.get("out_of_sample")

                print(f"\n  Experiment: {experiment.get('experiment_id')} | Regime: {out.get('context', {}).get('regime')}")
                print(f"  Verdict: {experiment.get('evidence', {}).get('verdict')} | Score: {experiment.get('evidence', {}).get('score')}")
                if out.get("stagnation", {}).get("stagnant"):
                    print(f"  Stagnation detected: {', '.join(out['stagnation'].get('reasons', []))}")
                    print(f"  Atlas research: {(out.get('research') or {}).get('status')}")

                if evo.get("promoted_new_champion"):
                    print("\n✅ SUCCESS: New champion strategy was promoted.")
                else:
                    print("\n❌ FAILED: New champion was NOT promoted. The candidate strategy did not pass certification.")

                print(f"\n  In-Sample Return: {in_sample_return or 0.0:.4f}")
                if oos_results:
                    print(f"  Out-of-Sample Return: {oos_results.get('total_return', 0.0):.4f} (Trades: {oos_results.get('trades')})")
                
                reflection = out.get("reflection", {})
                if reflection:
                    print("\n  Self Reflection:")
                    for insight in reflection.get("insights", []):
                        print(f"    - {insight}")
                    for directive in reflection.get("directives", []):
                        print(f"    - [DIRECTIVE] {directive}")

                print("\n  Hypotheses tested:")
                print(json.dumps(out.get("hypotheses", []), indent=2))
                print("\n  Best Genome DNA from this run:")
                print(json.dumps(evo.get("best_genome", {}).get("rules", []), indent=2))
            elif cmd == "hypotheses" and len(parts) >= 2:
                print(json.dumps(agent.act("hypothesis.generate", {"symbol": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "champion" and len(parts) >= 2:
                print(json.dumps(agent.act("strategy.champion", {"symbol": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "backtest" and len(parts) >= 2:
                print(json.dumps(agent.act("strategy.backtest", {"symbol": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "propose" and len(parts) >= 2:
                out = agent.act("trade.propose", {"symbol": parts[1], "_sender": "user"})
                last_streams = out.get("_streams", {})
                out.pop("_streams", None)
                print(json.dumps(out, indent=2))
            elif cmd == "learn" and len(parts) >= 3:
                rd = 1 if parts[2].strip() in ("+1", "1", "up") else -1
                print(json.dumps(agent.act("fusion.learn",
                    {"symbol": parts[1], "streams": last_streams, "realized_direction": rd,
                     "_sender": "user"}), indent=2))
            elif cmd == "portfolio":
                print(json.dumps(agent.act("portfolio.status", {"_sender": "user"}), indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: evolve EURUSD 6")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Error: {exc}")

    agent.stop()
    for peer in (pulse, sentinel, atlas, chronicle):
        if peer:
            try:
                peer.stop()
            except Exception:
                pass
    print("Oracle shutdown complete.")


if __name__ == "__main__":
    main()
