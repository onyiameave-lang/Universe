"""
Migrate existing local tracker data into Chronicle
=====================================================
Run this ONCE, after deploying the Chronicle-backed storage changes, so
your existing history (champion confidence, evolved strategies, etc.) is
immediately visible in Chronicle -- rather than sitting in the local
cache file until the next natural write happens to trigger a push.

Without this, ChronicleBackedStore still works correctly (it falls back
to the local cache and loses nothing), but Chronicle itself would stay
empty for each tracker until its next real update. This closes that gap
directly, once, rather than waiting.

One unified migration list, run through one loop -- adding a newly
Chronicle-wired tracker later means adding one entry to MIGRATIONS below,
not writing a new hand-copied block.

Usage:
    python tools/migrate_to_chronicle.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_ECO_ROOT = Path(__file__).resolve().parents[2]    # Universal_AI/
_ORACLE_ROOT = _ECO_ROOT / "Oracle"
_REPO_ROOT = _ORACLE_ROOT
for p in (_ORACLE_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

log = logging.getLogger("oracle.migrate_to_chronicle")


def _load(folder, rel, cls, **kw):
    import importlib.util
    path_added = False
    try:
        root = _ECO_ROOT / folder
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            path_added = True
        path = root / rel
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"{folder}_{cls}", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # type: ignore
        inst = getattr(m, cls)(**kw)
        inst.start()
        return inst
    except Exception as exc:
        log.warning("load %s failed: %s", folder, exc)
        return None
    finally:
        if path_added:
            sys.path.remove(str(_ECO_ROOT / folder))


def _migrate_simple_tracker(chronicle, construct: Callable[[], Any],
                             data_attr: str, save_method: str,
                             chronicle_key: str) -> Dict[str, Any]:
    """Shared logic for every ChronicleBackedStore-wired tracker: construct
    with chronicle_client set (pulls from Chronicle first, finds nothing
    yet, falls back to the existing local cache -- your real history),
    then force one explicit save (the part that doesn't happen
    automatically -- pushes everything to Chronicle right now instead of
    waiting for the next real update)."""
    instance = construct()
    data = getattr(instance, data_attr)
    count = len(data)
    if count > 0:
        getattr(instance, save_method)()
    return {"count": count, "key": chronicle_key}


def _migrate_scientific_journal(chronicle) -> Dict[str, Any]:
    """Different shape from the others: an append-only LOG (one entry per
    experiment), not bounded state -- so this pushes each existing entry as
    its own episodic memory via the same method new entries already use
    going forward (ScientificResearchLab._store_to_chronicle, confirmed
    working by direct test), rather than one big state blob."""
    from intelligence.scientific_lab import ScientificResearchLab  # type: ignore
    lab = ScientificResearchLab(chronicle=chronicle, storage_dir=str(_REPO_ROOT / "memory"))
    journal = lab._journal
    pushed = 0
    for experiment in journal:
        try:
            lab._store_to_chronicle(experiment, champion_record=None)
            pushed += 1
        except Exception as exc:
            log.warning("could not push experiment %s: %s", experiment.get("experiment_id"), exc)
    return {"count": len(journal), "pushed": pushed, "key": "trading (episodic, one record per experiment)"}


def _migrate_champion_library_and_history(chronicle) -> Dict[str, Any]:
    """One ScientificResearchLab instance bundles both champion_library.json
    (current champion per symbol/regime) and champion_history.json (capped
    rolling history) -- both genuinely bounded state, both pushed here in
    one pass rather than constructing the lab twice."""
    from intelligence.scientific_lab import ScientificResearchLab  # type: ignore
    lab = ScientificResearchLab(chronicle=chronicle, storage_dir=str(_REPO_ROOT / "memory"))
    lib_count = len(lab._champions)
    hist_count = len(lab._champion_history)
    if lib_count > 0:
        lab._champions_store.save()
    if hist_count > 0:
        lab._history_store.save()
    return {"count": lib_count + hist_count, "pushed": lib_count + hist_count,
            "key": f"champion_library ({lib_count}) + champion_history ({hist_count})"}


def _migrate_trade_experiment_log(chronicle) -> Dict[str, Any]:
    """Same shape as scientific_journal.json: an unbounded, ever-growing
    log -- pushes each existing line as its own episodic memory, matching
    what new entries already do going forward (via _append_experiment_log's
    chronicle_client param, confirmed working by direct test)."""
    from intelligence.trade_learning import load_experiment_log  # type: ignore
    records = load_experiment_log(path=_REPO_ROOT / "memory" / "trade_experiment_log.jsonl")
    pushed = 0
    for r in records:
        try:
            result = "won" if r.get("won") else "lost"
            chronicle.act("memory.store", {
                "content": f"Trade experiment: {r.get('symbol')} {result} ({r.get('pnl_r', 0):+.2f}R). "
                           f"Entry streams: {r.get('entry_streams', {})}",
                "pillar": "episodic", "domain": "trading",
                "summary": f"{r.get('symbol')} {result} {r.get('pnl_r', 0):+.2f}R",
                "tags": ["trade_experiment_log", r.get("symbol", ""), result], "_sender": "oracle"})
            pushed += 1
        except Exception as exc:
            log.warning("could not push experiment log entry: %s", exc)
    return {"count": len(records), "pushed": pushed, "key": "trading (episodic, one record per line)"}



# One unified list -- add a new entry here as each tracker gets wired to
# ChronicleBackedStore, instead of writing a new numbered block.
MIGRATIONS = [
    {
        "name": "ChampionConfidenceTracker (champion_confidence.json)",
        "run": lambda chronicle: _migrate_simple_tracker(
            chronicle,
            construct=lambda: __import__("intelligence.trade_learning", fromlist=["ChampionConfidenceTracker"])
                              .ChampionConfidenceTracker(chronicle_client=chronicle),
            data_attr="_data", save_method="_save", chronicle_key="champion_confidence"),
    },
    {
        "name": "scientific_journal.json (existing entries)",
        "run": _migrate_scientific_journal,
    },
    {
        "name": "AdaptiveFusion (fusion_weights.json)",
        "run": lambda chronicle: _migrate_simple_tracker(
            chronicle,
            construct=lambda: __import__("intelligence.adaptive_fusion", fromlist=["AdaptiveFusion"])
                              .AdaptiveFusion(storage_dir=str(_REPO_ROOT / "memory"), chronicle_client=chronicle),
            data_attr="_state", save_method="_persist", chronicle_key="fusion_weights"),
    },
    {
        "name": "EvolutionLab (evolved_strategies.json)",
        "run": lambda chronicle: _migrate_simple_tracker(
            chronicle,
            construct=lambda: __import__("intelligence.evolution", fromlist=["EvolutionLab"])
                              .EvolutionLab(chronicle=chronicle, storage_dir=str(_REPO_ROOT / "memory")),
            data_attr="_champions", save_method="_persist", chronicle_key="evolved_strategies"),
    },
    {
        "name": "champion_library.json + champion_history.json",
        "run": _migrate_champion_library_and_history,
    },
    {
        "name": "trade_experiment_log.jsonl",
        "run": _migrate_trade_experiment_log,
    },
    # oracle_learning.json is deliberately excluded -- confirmed orphaned
    # (nothing in the current codebase reads or writes it). Migrating dead
    # data doesn't make it live; this needs a decision (archive vs. leave
    # it), not a migration step. See docs/ECOSYSTEM_CAPABILITIES_AND_ROADMAP.md.
]


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    load_dotenv_early()

    print("=" * 60)
    print(" Migrating existing local tracker data into Chronicle")
    print("=" * 60)

    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                       storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
    unload_conflicting_modules()
    if chronicle is None:
        print("Could not load Chronicle -- aborting. Nothing was migrated.")
        return

    total = len(MIGRATIONS)
    for i, job in enumerate(MIGRATIONS, start=1):
        print(f"\n[{i}/{total}] {job['name']}...")
        try:
            result = job["run"](chronicle)
        except Exception as exc:
            log.warning("migration failed for %s: %s", job["name"], exc)
            print(f"  FAILED: {exc}")
            continue
        count = result.get("count", 0)
        if count == 0:
            print("  Nothing to migrate.")
        else:
            pushed = result.get("pushed", count)
            print(f"  Pushed {pushed}/{count} record(s) to Chronicle (key: {result.get('key')}).")

    chronicle.stop()
    print(f"\nDone. {total} migration(s) attempted. "
          "Verify with Chronicle's state.get / memory.search tasks.")


if __name__ == "__main__":
    run()