"""
Forge - Training Intelligence  (institutional, main entry point)
===============================================================
Constitutional Name: Forge  (formerly Training Engine)
Mission: Train, validate, tune, benchmark, and monitor models with rigor.
(Book III Part II Ch VIII; Book I Article IX.)

An institutional ML platform. Every job: validate data (quality + LEAKAGE
guard) -> select a backend (reasoning + Atlas research) -> optimize
hyperparameters by CROSS-VALIDATED search -> train final model -> evaluate on
a held-out set -> register + version with a data baseline -> gate promotion on
a passed benchmark -> monitor DRIFT over time. Real math throughout; sklearn /
PyTorch / PPO / from-scratch backends; nothing fabricated.

Run:
    python main.py

Commands:
    demo                              validate + tune + train the bundled 3-class set
    csv <path> <target> [type]        full pipeline on a real CSV
    validate                          (demo) show data validation + leakage check
    backends                          installed training backends
    leaderboard <domain>              ranked registered models
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
        logging.getLogger("forge.main").warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.pop(0)


def _demo():
    import random
    rng = random.Random(7); X, y = [], []
    for label, (cx, cy) in {"A": (2, 2), "B": (7, 7), "C": (2, 7)}.items():
        for _ in range(80):
            X.append([cx + rng.gauss(0, 0.8), cy + rng.gauss(0, 0.8),
                     rng.gauss(0, 1), rng.gauss(0, 1)])
            y.append(label)
    return X, y


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                      storage_dir=str(_REPO_ROOT.parent / "Chronicle" / "memory" / "store"))
    _unload_conflicting_modules()

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent", chronicle_client=chronicle)
    _unload_conflicting_modules()

    from agents.training_agent import ForgeAgent  # type: ignore

    agent = ForgeAgent(chronicle_client=chronicle, atlas_client=atlas)
    agent.start()

    print("=" * 64)
    print("  FORGE - Institutional ML Platform")
    print("  Validate -> select -> CV-tune -> train -> evaluate -> register -> gate -> drift.")
    print("=" * 64)
    from core.backends import available_backends
    print(f"  Backends: {[b.name for b in available_backends()]} | Atlas: {atlas is not None}")
    print("  Commands: demo | csv <path> <target> [type] | validate | backends | leaderboard <d> | status | quit")

    while True:
        try:
            line = input("Forge> ").strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            parts = line.split()
            cmd = parts[0]

            if cmd == "demo":
                X, y = _demo()
                out = agent.act("training.run", {"X": X, "y": y, "task_type": "classification",
                    "dataset_id": "demo_3class", "register_as": "demo_model", "_sender": "user"})
                print("BACKEND:", out.get("chosen_backend"), "| DATA QUALITY:", out.get("data_quality"))
                print("CV SEARCH:", json.dumps(out.get("cv_optimization", {}).get("best_hyperparameters"), indent=2))
                print("METRICS:", json.dumps(out.get("metrics"), indent=2))
            elif cmd == "validate":
                X, y = _demo()
                out = agent.act("data.validate", {"X": X, "y": y, "task_type": "classification", "_sender": "user"})
                print(json.dumps(out, indent=2))
            elif cmd == "csv" and len(parts) >= 3:
                tt = parts[3] if len(parts) > 3 else "classification"
                out = agent.act("training.from_csv", {"path": parts[1], "target_column": parts[2],
                    "task_type": tt, "register_as": Path(parts[1]).stem, "_sender": "user"})
                print(json.dumps(out, indent=2))
            elif cmd == "backends":
                print(json.dumps(agent.act("backends.catalog", {"_sender": "user"}), indent=2))
            elif cmd == "leaderboard" and len(parts) >= 2:
                print(json.dumps(agent.act("model.leaderboard", {"domain": parts[1], "_sender": "user"}), indent=2))
            elif cmd == "status":
                print(json.dumps(agent.get_status(), indent=2))
            else:
                print("Unknown command. Try: demo")
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
    print("Forge shutdown complete.")


if __name__ == "__main__":
    main()
