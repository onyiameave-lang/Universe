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

import importlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
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


def _aegis_verifier(audience: str):
    """Build a verifier only from explicitly configured issuer public keys."""
    raw_keys = os.environ.get("FORGE_AEGIS_TRUSTED_ISSUERS_JSON")
    if not raw_keys:
        return None
    try:
        keys = json.loads(raw_keys)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FORGE_AEGIS_TRUSTED_ISSUERS_JSON is not valid JSON") from exc
    if not isinstance(keys, dict) or not keys:
        raise RuntimeError("FORGE_AEGIS_TRUSTED_ISSUERS_JSON must be a non-empty issuer-to-public-key object")
    if any(
        not isinstance(issuer, str) or not issuer.strip()
        or not isinstance(public_key, str) or not public_key.strip()
        for issuer, public_key in keys.items()
    ):
        raise RuntimeError("Aegis issuer IDs and public keys must be non-empty strings")
    try:
        from Forge.core.authorization import Ed25519AuthorizationVerifier
    except ImportError as exc:
        raise RuntimeError(
            "Aegis keys were configured, but the cryptography package is unavailable"
        ) from exc
    return Ed25519AuthorizationVerifier(
        trusted_issuers=keys,
        audience=audience,
    )


def _deployment_health_gate():
    """Load an operator-owned runtime observer from an explicit factory path.

    The observer must be installed in the deployment environment and expose
    ``operational_status()`` and ``assess(deployment, target_phase)``. Forge
    deliberately has no synthetic default because a local model check cannot
    establish live serving health.
    """
    configured = os.environ.get("FORGE_DEPLOYMENT_HEALTH_GATE")
    if not configured:
        return None
    if not isinstance(configured, str) or not re.fullmatch(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", configured
    ):
        raise RuntimeError(
            "FORGE_DEPLOYMENT_HEALTH_GATE must be a trusted module path in the form package.module:factory"
        )
    module_path, factory_name = configured.split(":", 1)
    try:
        factory = getattr(importlib.import_module(module_path), factory_name)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Configured deployment health gate module or factory could not be loaded"
        ) from exc
    if not callable(factory):
        raise RuntimeError("Configured deployment health gate factory is not callable")
    gate = factory()
    if (
        gate is None
        or not callable(getattr(gate, "assess", None))
        or not callable(getattr(gate, "operational_status", None))
    ):
        raise RuntimeError(
            "Deployment health gate factory must return an object with callable "
            "operational_status() and assess(deployment, target_phase) methods"
        )
    return gate


def _reuse_compatibility_policy():
    """Load operator-owned license and runtime compatibility checks."""
    configured = os.environ.get("FORGE_REUSE_COMPATIBILITY_POLICY")
    if not configured:
        return None
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", configured):
        raise RuntimeError(
            "FORGE_REUSE_COMPATIBILITY_POLICY must be a trusted module path in the form package.module:factory"
        )
    module_path, factory_name = configured.split(":", 1)
    try:
        factory = getattr(importlib.import_module(module_path), factory_name)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Configured reuse policy module or factory could not be loaded") from exc
    if not callable(factory):
        raise RuntimeError("Configured reuse policy factory is not callable")
    policy = factory()
    if policy is None or any(
        not callable(getattr(policy, name, None))
        for name in ("operational_status", "version_compatible", "licensing_clear")
    ):
        raise RuntimeError(
            "Reuse policy factory must return operational_status(), "
            "version_compatible(artifact, target_domain), and "
            "licensing_clear(artifact, requesting_agent, target_domain)"
        )
    return policy


def _domain_owners():
    """Read the operator's owner map for custom Forge evolution domains."""
    raw = os.environ.get("FORGE_DOMAIN_OWNERS_JSON")
    if not raw:
        return {}
    try:
        owners = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FORGE_DOMAIN_OWNERS_JSON is not valid JSON") from exc
    if not isinstance(owners, dict) or any(
        not isinstance(domain, str) or not domain.strip()
        or not isinstance(owner, str) or not owner.strip()
        for domain, owner in owners.items()
    ):
        raise RuntimeError(
            "FORGE_DOMAIN_OWNERS_JSON must be an object mapping non-empty domain names to agent names"
        )
    from Forge.core.challenger import normalize_domain_owners
    normalize_domain_owners(owners)
    return owners


def _bounded_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer from {minimum} through {maximum}") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _close_health_gate(gate) -> None:
    close = getattr(gate, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logging.getLogger("forge.main").exception(
                "deployment health gate cleanup failed"
            )


def _close_reuse_policy(policy) -> None:
    close = getattr(policy, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logging.getLogger("forge.main").exception("reuse policy cleanup failed")


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

    health_gate = None
    reuse_policy = None
    try:
        health_gate = _deployment_health_gate()
        reuse_policy = _reuse_compatibility_policy()
        agent = ForgeAgent(
            chronicle_client=chronicle,
            atlas_client=atlas,
            promotion_verifier=_aegis_verifier("forge.promotion"),
            deployment_verifier=_aegis_verifier("forge.deployment"),
            training_request_verifier=_aegis_verifier("forge.training"),
            reuse_authorization_verifier=_aegis_verifier("forge.reuse"),
            reuse_compatibility_policy=reuse_policy,
            domain_owners=_domain_owners(),
            deployment_health_gate=health_gate,
            experiment_max_concurrent_runs=_bounded_int_setting(
                "FORGE_MAX_CONCURRENT_RUNS_LOCAL", 2, 1, 64
            ),
            experiment_max_total_concurrent_runs=_bounded_int_setting(
                "FORGE_MAX_CONCURRENT_RUNS_TOTAL", 2, 1, 256
            ),
            experiment_reconciliation_verifier=_aegis_verifier("forge.operations"),
        )
        agent.start()
    except Exception:
        for peer in (atlas, chronicle):
            if peer:
                try:
                    peer.stop()
                except Exception:
                    logging.getLogger("forge.main").exception(
                        "peer cleanup failed during Forge startup"
                    )
        _close_health_gate(health_gate)
        _close_reuse_policy(reuse_policy)
        raise

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

    try:
        agent.stop()
    finally:
        for peer in (atlas, chronicle):
            if peer:
                try:
                    peer.stop()
                except Exception:
                    logging.getLogger("forge.main").exception(
                        "peer shutdown failed: %s", getattr(peer, "name", "unknown")
                    )
        _close_health_gate(health_gate)
        _close_reuse_policy(reuse_policy)
    print("Forge shutdown complete.")


def _status_probe() -> int:
    """Emit machine-readable readiness state for supervisors and monitoring."""
    gate = None
    reuse_policy = None
    try:
        from agents.training_agent import ForgeAgent  # type: ignore

        gate = _deployment_health_gate()
        reuse_policy = _reuse_compatibility_policy()
        agent = ForgeAgent(
            promotion_verifier=_aegis_verifier("forge.promotion"),
            deployment_verifier=_aegis_verifier("forge.deployment"),
            training_request_verifier=_aegis_verifier("forge.training"),
            reuse_authorization_verifier=_aegis_verifier("forge.reuse"),
            reuse_compatibility_policy=reuse_policy,
            domain_owners=_domain_owners(),
            deployment_health_gate=gate,
            experiment_max_concurrent_runs=_bounded_int_setting(
                "FORGE_MAX_CONCURRENT_RUNS_LOCAL", 2, 1, 64
            ),
            experiment_max_total_concurrent_runs=_bounded_int_setting(
                "FORGE_MAX_CONCURRENT_RUNS_TOTAL", 2, 1, 256
            ),
            experiment_reconciliation_verifier=_aegis_verifier("forge.operations"),
        )
        snapshot = agent.get_status()
        readiness = snapshot.get("evolution", {}).get("operational_readiness", {})
        ready = readiness.get("platform_ready") is True
        response = {
            "schema_version": 1,
            "probe_type": "readiness_snapshot",
            "status": "ready" if ready else "not_ready",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "readiness": readiness,
            "snapshot": snapshot,
        }
        print(json.dumps(response, indent=2, default=str))
        return 0 if ready else 1
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "probe_type": "readiness_snapshot",
            "status": "error",
            "error_type": type(exc).__name__,
        }, indent=2))
        return 2
    finally:
        _close_health_gate(gate)
        _close_reuse_policy(reuse_policy)


if __name__ == "__main__":
    if sys.argv[1:] == ["--status-json"]:
        raise SystemExit(_status_probe())
    main()
