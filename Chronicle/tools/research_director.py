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

The nightly report also snapshots Oracle's persisted failure counts and
lessons. Atlas may assess newly recurring operational failures; Forge remains
limited to structured trade-stream experiments. No result is auto-applied.

Usage:
    python tools/research_director.py
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
_STATE_PATH = _REPO_ROOT / "memory" / "research_director_state.json"
_FAILURE_REPORT_LIMIT = 15


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

    atlas = _load("Atlas", "agents/research_agent.py", "AtlasAgent",
                  chronicle_client=chronicle)
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


def _load_state(path: Path = _STATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not load Research Director checkpoint %s: %s", path, exc)
        return {}


def _save_state(state: Dict[str, Any], path: Path = _STATE_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.warning("could not save Research Director checkpoint %s: %s", path, exc)
        return False


def _collect_learning_failures() -> Dict[str, Any]:
    """Read Oracle's persisted failure summary; it contains lifetime counts, not dated events."""
    path = _ECO_ROOT / "Oracle" / "memory" / "oracle_learning.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("Oracle learning log not found at %s", path)
        return {"available": False, "failure_signatures": {}, "lessons": []}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read Oracle learning log %s: %s", path, exc)
        return {"available": False, "failure_signatures": {}, "lessons": []}

    if not isinstance(data, dict):
        log.warning("Oracle learning log has an unexpected format: %s", path)
        return {"available": False, "failure_signatures": {}, "lessons": []}
    signatures = data.get("failure_signatures", {})
    lessons = data.get("lessons", [])
    if not isinstance(signatures, dict):
        log.warning("Oracle learning log has invalid failure_signatures: %s", path)
        return {"available": False, "failure_signatures": {}, "lessons": []}
    if not isinstance(lessons, list):
        log.warning("Oracle learning log has invalid lessons: %s", path)
        lessons = []
    counts = {
        str(signature): count
        for signature, count in signatures.items()
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }
    return {"available": True, "failure_signatures": counts, "lessons": lessons}


def _filter_trades_since(
    trades: List[Dict[str, Any]], last_run: Optional[float], run_at: float
) -> List[Dict[str, Any]]:
    """Select trade records since the previous successful report checkpoint."""
    selected = []
    for trade in trades:
        timestamp = trade.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        if timestamp <= run_at and (last_run is None or timestamp > last_run):
            selected.append(trade)
    return selected


def _get_or_add_hypothesis(forge, statement: str) -> str:
    """Continue unresolved hypotheses so daily samples can accumulate in Forge."""
    listed = forge.act("hypothesis.list", {})
    hypotheses = (listed or {}).get("hypotheses", [])
    candidates = [
        hypothesis for hypothesis in hypotheses
        if isinstance(hypothesis, dict)
        and hypothesis.get("statement") == statement
        and hypothesis.get("status") in ("untested", "testing", "inconclusive")
        and hypothesis.get("id")
    ]
    if candidates:
        candidates.sort(key=lambda hypothesis: hypothesis.get("created_at", 0))
        return candidates[-1]["id"]

    added = forge.act("hypothesis.add", {
        "statement": statement, "proposed_by": "chronicle_research_director"})
    hypothesis_id = ((added or {}).get("hypothesis") or {}).get("id")
    if not hypothesis_id:
        raise RuntimeError((added or {}).get("message", "Forge did not return a hypothesis id"))
    return hypothesis_id


def _atlas_assessment(atlas, query: str, memory_only: bool) -> Optional[str]:
    """Use Atlas's local memory/LLM fallback for operational logs, not web search."""
    if memory_only:
        report = atlas._best_effort_report(query, "trading")
    else:
        result = atlas.act("research.investigate", {
            "query": query, "domain": "trading", "depth": "standard",
            "_sender": "chronicle_research_director"})
        report = (result or {}).get("report") or {}
    summary = report.get("summary")
    return summary if isinstance(summary, str) and summary.strip() else None


def _redact_failure_text(value: Any) -> str:
    text = str(value or "").replace("\n", " ").strip()
    text = re.sub(r"(?i)\b(bearer)\s+\S+", r"\1 [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)"
        r"(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]", text,
    )
    text = re.sub(r"(?i)([?&](?:key|token|secret|password)=)[^&\s]+",
                  r"\1[REDACTED]", text)
    return text[:240]


def _summarize_failures(
    learning: Dict[str, Any], previous_counts: Optional[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, int], bool]:
    current = learning.get("failure_signatures", {})
    previous = previous_counts if isinstance(previous_counts, dict) else None
    has_baseline = previous is not None
    report_items = []
    normalized_counts: Dict[str, int] = {}

    for signature, count in current.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            continue
        normalized_counts[str(signature)] = count
        prior = previous.get(signature, 0) if previous is not None else 0
        prior = prior if isinstance(prior, int) and not isinstance(prior, bool) else 0
        delta = count - prior if count >= prior else count
        task, separator, error = str(signature).partition("::")
        normalized_error = error.strip().lower()
        report_items.append({
            "task": _redact_failure_text(task),
            "error": _redact_failure_text(error if separator else ""),
            "count": count,
            "new_since_last_report": delta if has_baseline else None,
            "recurring": count >= 3,
            "counter_reset": has_baseline and count < prior,
            "routine_outcome": normalized_error.startswith(
                ("risk gate rejected", "signal is hold")),
        })

    report_items.sort(
        key=lambda item: (
            not item.get("routine_outcome", False),
            item["new_since_last_report"] or 0,
            item["count"],
        ),
        reverse=True,
    )
    report_items = report_items[:_FAILURE_REPORT_LIMIT]

    lesson_counts: Counter[Tuple[str, str, str, bool]] = Counter()
    for lesson in learning.get("lessons", []):
        if not isinstance(lesson, dict):
            continue
        task = _redact_failure_text(lesson.get("task"))
        summary = _redact_failure_text(
            lesson.get("root_cause") or lesson.get("lesson") or lesson.get("adjustment"))
        if task and summary:
            routine_outcome = summary.strip().lower().startswith(
                ("risk gate rejected", "signal is hold"))
            lesson_counts[
                (task, summary, _redact_failure_text(lesson.get("adjustment")),
                 routine_outcome)
            ] += 1

    ranked_lessons = sorted(
        lesson_counts.items(),
        key=lambda item: (item[0][3], -item[1]),
    )
    for (task, summary, adjustment, routine_outcome), count in ranked_lessons[:5]:
        report_items.append({
            "task": task, "lesson": summary, "suggested_adjustment": adjustment,
            "count": count, "routine_outcome": routine_outcome,
        })
    return report_items, normalized_counts, has_baseline


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    load_dotenv_early()

    print("=" * 64)
    print(" CHRONICLE RESEARCH DIRECTOR")
    print(" Chronicle collects -> Forge tests -> Chronicle stores")
    print("=" * 64)

    chronicle, forge, atlas = _boot_agents()
    state = _load_state()
    run_at = time.time()
    last_run = state.get("last_successful_report_at")
    if not isinstance(last_run, (int, float)):
        last_run = None

    try:
        all_trades = _collect_trade_data()
        trades = _filter_trades_since(all_trades, last_run, run_at)
        print(f"\nCollected {len(trades)} new trade record(s) since the last successful report.")

        learning = _collect_learning_failures()
        previous_failure_counts = state.get("failure_signatures")
        failures, failure_counts, has_failure_baseline = _summarize_failures(
            learning, previous_failure_counts)
        failure_rows = [item for item in failures if "error" in item]

        report_lines = [
            f"CHRONICLE RESEARCH DIRECTOR REPORT — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(run_at))}",
            (f"Trade records since previous successful report: {len(trades)}"
             if last_run is not None else
             f"Trade records in initial baseline: {len(trades)}"),
            "",
            "DAILY TRADE-STREAM RESEARCH",
        ]
        conclusions = []
        present_streams = sorted({
            stream for trade in trades
            for stream in (trade.get("entry_streams") or {})
            if stream in KNOWN_STREAMS
        })

        if len(trades) < MIN_TRADES_TO_ATTEMPT:
            msg = (f"Only {len(trades)} new trade(s); Forge requires at least "
                   f"{MIN_TRADES_TO_ATTEMPT} trades before attempting stream research.")
            print(msg)
            report_lines.extend([msg, ""])
        elif forge is None:
            msg = "Forge did not load; trade-stream analysis was not run."
            log.warning(msg)
            report_lines.extend([msg, ""])
        elif not present_streams:
            msg = "No recognized signal streams were present in the new trade records."
            print(msg)
            report_lines.extend([msg, ""])
        else:
            for stream in present_streams:
                statement = f"The {stream} signal stream predicts trade win/loss outcomes"
                try:
                    hypothesis_id = _get_or_add_hypothesis(forge, statement)

                    observations = [
                        (trade["entry_streams"][stream], trade["won"])
                        for trade in trades
                        if stream in (trade.get("entry_streams") or {})
                        and isinstance(trade["entry_streams"][stream], (int, float))
                        and isinstance(trade.get("won"), bool)
                    ]
                    exp_result = forge.act("experiment.run", {
                        "template": "sensitivity_analysis", "hypothesis_id": hypothesis_id,
                        "template_kwargs": {
                            "observations": observations, "label": f"{stream} stream"},
                    })
                    if (exp_result or {}).get("status") != "complete":
                        raise RuntimeError((exp_result or {}).get(
                            "message", "Forge experiment did not complete"))
                    result = (exp_result or {}).get("result") or {}
                    status = ((exp_result or {}).get("hypothesis") or {}).get("status", "unknown")
                    needs_human_review = (
                        status == "inconclusive" or result.get("sample_size", 0) < 20)
                    conclusions.append({
                        "stream": stream, "hypothesis_id": hypothesis_id, "status": status,
                        "result": result, "needs_human_review": needs_human_review,
                    })
                    review_note = " (NEEDS HUMAN REVIEW)" if needs_human_review else ""
                    report_lines.extend([
                        f"[{stream}] {statement}",
                        f"  {result.get('reason', 'no reason available')}",
                        f"  Status: {status}{review_note}",
                        "",
                    ])
                except Exception as exc:
                    log.warning("Forge stream analysis failed for %s: %s", stream, exc)
                    report_lines.extend([
                        f"[{stream}] analysis failed: {_redact_failure_text(exc)}", ""])

        report_lines.extend(["RECURRING ORACLE FAILURES", ""])
        if not learning.get("available"):
            report_lines.append(
                "Oracle's persisted learning log could not be read; failure analysis was skipped.")
        elif not failure_rows:
            report_lines.append("No persisted Oracle failure signatures were found.")
        else:
            report_lines.append(
                "Failure counts are cumulative totals from Oracle's LearningLog; "
                "daily deltas are available after the first successful report.")
            if not has_failure_baseline:
                report_lines.append("This is the initial failure-count baseline; no daily deltas yet.")
            for item in failure_rows:
                delta = item["new_since_last_report"]
                delta_text = (f", +{delta} since prior report" if delta is not None
                              else ", baseline count")
                category = "routine control outcome" if item.get("routine_outcome") \
                    else "operational failure"
                report_lines.append(
                    f"- [{category}] {item['task']}: {item['error'] or '(no error detail)'} "
                    f"(total {item['count']}{delta_text})")
            for item in failures:
                if "lesson" in item:
                    lesson_type = "routine outcome lesson" if item.get("routine_outcome") \
                        else "operational lesson"
                    report_lines.append(
                        f"- Existing {lesson_type} for {item['task']}: {item['lesson']}; "
                        f"suggested adjustment: {item['suggested_adjustment'] or '(none recorded)'} "
                        f"(stored {item['count']} time(s))")

        atlas_queries: List[Tuple[str, bool]] = []
        operational_failures = [
            item for item in failure_rows if not item.get("routine_outcome")
        ]
        if operational_failures and (not has_failure_baseline or any(
                item["new_since_last_report"] for item in operational_failures)):
            new_failures = [
                item for item in operational_failures
                if item["new_since_last_report"] is None or item["new_since_last_report"] > 0
            ][:5]
            failure_context = "\n".join(
                f"- {item['task']}: {item['error'] or '(no detail)'} "
                f"(new={item['new_since_last_report']}, total={item['count']})"
                for item in new_failures
            )
            recorded_lessons = [
                item for item in failures
                if "lesson" in item and not item.get("routine_outcome")
            ][:3]
            lesson_context = "\n".join(
                f"- {item['task']}: {item['lesson']}; "
                f"adjustment={item['suggested_adjustment']}"
                for item in recorded_lessons
            )
            prompt = (
                "Explain these recurring Oracle operational errors using available "
                "Chronicle memory and model knowledge. Identify cautious, testable "
                "diagnostic questions for a human. Do not claim to have verified the "
                "runtime, propose automatic code changes, or generate trading strategies.\n"
                f"Errors:\n{failure_context}"
            )
            if lesson_context:
                prompt += f"\nRecorded lessons:\n{lesson_context}"
            atlas_queries.append((prompt, True))
        confirmed = [item for item in conclusions if item["status"] == "confirmed"]
        if confirmed:
            atlas_queries.append((
                "Given that " + "; ".join(
                    f"the {item['stream']} stream shows {item['result'].get('reason', '')}"
                    for item in confirmed) +
                " -- is this consistent with how these signal types are generally understood "
                "to relate to short-term price movement?", False))

        if atlas is not None:
            for query, use_best_effort in atlas_queries:
                try:
                    atlas_summary = _atlas_assessment(atlas, query, use_best_effort)
                    if atlas_summary:
                        assessment_type = (
                            "Atlas advisory assessment (Chronicle/LLM only; no web search)"
                            if use_best_effort else "Atlas assessment"
                        )
                        report_lines.extend(["", f"{assessment_type}: {atlas_summary}"])
                except Exception as exc:
                    log.warning("Atlas context request failed (non-blocking): %s", exc)
        elif atlas_queries:
            report_lines.extend(["", "Atlas was unavailable; no qualitative assessment was added."])

        report_text = "\n".join(report_lines)
        print("\n" + report_text)

        saved = False
        if chronicle is not None:
            try:
                response = chronicle.act("memory.store", {
                    "content": report_text, "pillar": "episodic", "domain": "research_conclusions",
                    "summary": (
                        f"Research Director: {len(conclusions)} stream(s) tested, "
                        f"{len(trades)} new trade(s), {len(failure_rows)} Oracle failure(s) reviewed"),
                    "tags": ["research_conclusions", "oracle_failures",
                             time.strftime("%Y-%m-%d", time.gmtime(run_at))],
                    "_sender": "chronicle_research_director",
                })
                saved = isinstance(response, dict) and response.get("status") == "complete"
                if saved:
                    print("\nSaved to Chronicle (domain=research_conclusions). "
                          "Findings are advisory and never auto-applied.")
                else:
                    log.warning("Chronicle did not confirm Research Director report storage: %r",
                                response)
            except Exception as exc:
                log.warning("could not save report to Chronicle: %s", exc)
        else:
            log.warning("Chronicle did not load; report was not persisted and checkpoint was not advanced")

        if saved:
            _save_state({
                "last_successful_report_at": run_at,
                "failure_signatures": (
                    failure_counts if learning.get("available")
                    else previous_failure_counts or {}),
            })
    finally:
        for peer in (forge, atlas, chronicle):
            if peer:
                try:
                    peer.stop()
                except Exception as exc:
                    log.warning("could not stop nightly research agent %s: %s",
                                getattr(peer, "name", type(peer).__name__), exc)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chronicle Research Director")
    ap.parse_args()
    run()