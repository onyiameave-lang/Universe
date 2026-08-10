"""
Chronicle Research Director
=============================
The REAL nightly research loop, correctly directed: CHRONICLE initiates,
not Oracle. (The earlier Oracle/tools/nightly_research.py had Oracle
asking about itself -- backwards, per the architecture discussion this
replaces it.)

The loop, concretely, for V1's first hypothesis ("which signal stream
actually predicts trade success?"):

    Chronicle
        |
        v
    Collect the day's data (Oracle's structured trade log)
        |
        v
    Check sample size -- skip anything not ready yet
        |
        v
    Register/continue each stream's hypothesis in Forge's Hypothesis Queue
        |
        v
    Forge runs Sensitivity Analysis (Experiment Template)
        |
        v
    Aegis validates -- V1: a human reviews the printed report (Aegis's
    real capabilities haven't been built/verified yet; a human standing in
    for a thin/aspirational step is safer than a silent no-op nobody
    notices)
        |
        v
    Chronicle stores the conclusion (domain="research_conclusions")
        |
        v
    Oracle can later read this as a SUGGESTION -- never auto-applied

Atlas's role is deliberately thin for THIS hypothesis: correlating
already-collected numeric data doesn't need Atlas's research capability
(no external knowledge required). It's called anyway, best-effort, to add
qualitative context -- but the loop doesn't depend on it succeeding.

Usage:
    python tools/research_director.py
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]    # Chronicle/
_ECO_ROOT = _REPO_ROOT.parent                         # ecosystem root
for p in (_REPO_ROOT, _ECO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.startup import load_dotenv_early, unload_conflicting_modules  # noqa: E402

log = logging.getLogger("chronicle.research_director")

# Streams every trade's entry_streams dict may contain -- matches Oracle's
# signal fusion (technical/news/social/memory). New streams added there
# don't need a code change here; only ones actually present in the data
# get a hypothesis registered.
KNOWN_STREAMS = ("technical", "news", "social", "memory")

# Same discipline as Forge's HypothesisQueue itself: don't even bother
# registering/testing a stream's hypothesis until there's a reasonable
# amount of data -- avoids a wall of "inconclusive" noise on day one.
MIN_TRADES_TO_ATTEMPT = 10


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


def _boot_agents():
    chronicle = _load("Chronicle", "agents/chronicle_agent.py", "ChronicleAgent",
                       storage_dir=str(_ECO_ROOT / "Chronicle" / "memory" / "store"))
    unload_conflicting_modules()

    forge = _load("Forge", "agents/training_agent.py", "ForgeAgent")
    unload_conflicting_modules()

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent")
    unload_conflicting_modules()

    return chronicle, forge, atlas


def _collect_trade_data():
    """Chronicle 'collecting the day's work' -- pulling Oracle's structured
    trade log. This is Chronicle looking at what Oracle did, NOT Oracle
    asking about itself, even though the data physically lives in Oracle's
    memory folder (matches the "operational state stays local, but nothing
    is hidden from ecosystem-wide research" split from the architecture
    discussion)."""
    try:
        oracle_root = _ECO_ROOT / "Oracle"
        if str(oracle_root) not in sys.path:
            sys.path.insert(0, str(oracle_root))
        from intelligence.trade_learning import load_experiment_log  # type: ignore
        return load_experiment_log(path=oracle_root / "memory" / "trade_experiment_log.jsonl")
    except Exception as exc:
        log.warning("could not load Oracle's trade experiment log: %s", exc)
        return []


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    load_dotenv_early()

    print("=" * 64)
    print(" CHRONICLE RESEARCH DIRECTOR")
    print(" Chronicle collects -> Forge tests -> Chronicle stores")
    print("=" * 64)

    chronicle, forge, atlas = _boot_agents()
    if forge is None:
        print("Forge did not load -- cannot run experiments. Aborting.")
        return

    trades = _collect_trade_data()
    print(f"\nCollected {len(trades)} trade record(s) from Oracle's structured log.")

    if len(trades) < MIN_TRADES_TO_ATTEMPT:
        print(f"Fewer than {MIN_TRADES_TO_ATTEMPT} trades so far -- not enough to test "
              f"anything meaningfully yet. Nothing to do tonight.")
        return

    present_streams = sorted({s for t in trades for s in (t.get("entry_streams") or {}).keys()}
                              & set(KNOWN_STREAMS))
    print(f"Streams present in the data: {present_streams or 'none'}")

    report_lines = [
        f"CHRONICLE RESEARCH DIRECTOR REPORT — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        f"Trades analyzed: {len(trades)}",
        "",
    ]
    conclusions = []

    for stream in present_streams:
        statement = f"The {stream} signal stream predicts trade win/loss outcomes"
        hyp_result = forge.act("hypothesis.add", {
            "statement": statement, "proposed_by": "chronicle_research_director"})
        hypothesis_id = hyp_result["hypothesis"]["id"]

        observations = [
            (t["entry_streams"][stream], t["won"])
            for t in trades if stream in (t.get("entry_streams") or {})
        ]
        exp_result = forge.act("experiment.run", {
            "template": "sensitivity_analysis", "hypothesis_id": hypothesis_id,
            "template_kwargs": {"observations": observations, "label": f"{stream} stream"},
        })
        result = exp_result["result"]
        status = exp_result.get("hypothesis", {}).get("status", "unknown")

        # Aegis stand-in (V1): flag anything borderline for a human to
        # actually look at, rather than silently trusting a thin/unbuilt
        # validation step.
        needs_human_review = status == "inconclusive" or result.get("sample_size", 0) < 20

        conclusions.append({
            "stream": stream, "hypothesis_id": hypothesis_id, "status": status,
            "result": result, "needs_human_review": needs_human_review,
        })

        print(f"\n[{stream}] {statement}")
        print(f"  {result.get('reason', 'no reason available')}")
        print(f"  Status: {status}" + ("  ⚠ REVIEW NEEDED (small sample or inconclusive)"
                                        if needs_human_review else ""))

        report_lines.append(f"[{stream}] {statement}")
        report_lines.append(f"  {result.get('reason', 'no reason available')}")
        report_lines.append(f"  Status: {status}"
                             + (" (NEEDS HUMAN REVIEW)" if needs_human_review else ""))
        report_lines.append("")

    # Best-effort Atlas context -- deliberately optional; this hypothesis
    # doesn't need external research, so a failure here shouldn't block
    # anything above.
    if atlas is not None:
        try:
            confirmed = [c for c in conclusions if c["status"] == "confirmed"]
            if confirmed:
                query = ("Given that " + "; ".join(
                    f"the {c['stream']} stream shows {c['result']['reason']}" for c in confirmed) +
                    " -- is this consistent with how these signal types are generally understood "
                    "to relate to short-term price movement?")
                atlas_result = atlas.act("research.investigate", {
                    "query": query, "domain": "trading", "deep_research": True,
                    "_sender": "chronicle_research_director"})
                atlas_summary = ((atlas_result or {}).get("report") or {}).get("summary")
                if atlas_summary:
                    print(f"\nAtlas context: {atlas_summary}")
                    report_lines.append(f"Atlas context: {atlas_summary}")
        except Exception as exc:
            log.warning("Atlas context request failed (non-blocking): %s", exc)

    report_text = "\n".join(report_lines)
    if chronicle is not None:
        try:
            chronicle.act("memory.store", {
                "content": report_text, "pillar": "episodic", "domain": "research_conclusions",
                "summary": f"Research Director: {len(present_streams)} stream(s) tested, "
                           f"{len(trades)} trades analyzed",
                "tags": ["research_conclusions", time.strftime("%Y-%m-%d")],
                "_sender": "chronicle_research_director",
            })
            print("\nSaved to Chronicle (domain=research_conclusions). "
                  "Oracle can consume this as a suggestion — never auto-applied.")
        except Exception as exc:
            log.warning("could not save report to Chronicle: %s", exc)

    for peer in (forge, atlas, chronicle):
        if peer:
            try:
                peer.stop()
            except Exception:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chronicle Research Director")
    ap.parse_args()
    run()