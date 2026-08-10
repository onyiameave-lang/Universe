"""
Forge Hypothesis Queue
========================
"The queue becomes the backlog of scientific questions." Every hypothesis
has a bounded lifecycle:

    untested -> testing -> confirmed | inconclusive | rejected

Design notes (learned directly from a real bug found this session --
Atlas's select_family() mutated a shared global dict in place, silently
corrupting strategy selection forever):
  - A hypothesis's EVIDENCE is append-only (a list of experiment results
    over time), never overwritten. The current `status` is a derived
    summary, not the source of truth -- you can always reconstruct why a
    hypothesis reached its current status by reading its evidence list.
  - Nothing here auto-applies a conclusion anywhere else in the ecosystem.
    This is a record, not a trigger.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("forge.hypothesis_queue")

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[1] / "memory" / "hypothesis_queue.json"

VALID_STATUSES = ("untested", "testing", "confirmed", "inconclusive", "rejected")

# A conclusion needs at least this many trades/observations behind it before
# it can move to "confirmed" or "rejected" -- otherwise it stays
# "inconclusive" regardless of which way the evidence currently leans.
# Mirrors the same discipline used elsewhere this session (Champion
# Retirement's MIN_TRADES_FOR_STATUS) -- small samples don't get to look
# like settled science.
MIN_SAMPLE_SIZE_FOR_VERDICT = 30


@dataclass
class Hypothesis:
    id: str
    statement: str
    proposed_by: str          # which agent/process proposed this (e.g. "chronicle_research_director")
    status: str = "untested"
    created_at: float = field(default_factory=time.time)
    evidence: List[Dict[str, Any]] = field(default_factory=list)   # append-only

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HypothesisQueue:
    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = store_path or DEFAULT_STORE_PATH
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.store_path.exists():
            try:
                return json.loads(self.store_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("hypothesis_queue.json unreadable (%s); starting fresh", exc)
        return {}

    def _save(self) -> None:
        try:
            self.store_path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError as exc:
            log.warning("could not persist hypothesis_queue.json: %s", exc)

    def add(self, statement: str, proposed_by: str, hypothesis_id: Optional[str] = None) -> Hypothesis:
        """Adds a new hypothesis at 'untested'. If hypothesis_id is omitted,
        one is generated. Returns the existing hypothesis unchanged if an
        identical statement is already queued (no duplicate questions)."""
        for existing in self._data.values():
            if existing["statement"] == statement and existing["status"] in ("untested", "testing"):
                return Hypothesis(**existing)
        hid = hypothesis_id or f"HYP-{int(time.time())}-{len(self._data) + 1}"
        h = Hypothesis(id=hid, statement=statement, proposed_by=proposed_by)
        self._data[hid] = h.to_dict()
        self._save()
        log.info("[%s] new hypothesis queued: %s", hid, statement)
        return h

    def get(self, hypothesis_id: str) -> Optional[Hypothesis]:
        rec = self._data.get(hypothesis_id)
        return Hypothesis(**rec) if rec else None

    def get_pending(self) -> List[Hypothesis]:
        """Hypotheses at 'untested' -- ready for Forge to pick up and test."""
        return [Hypothesis(**rec) for rec in self._data.values() if rec["status"] == "untested"]

    def mark_testing(self, hypothesis_id: str) -> None:
        if hypothesis_id in self._data:
            self._data[hypothesis_id]["status"] = "testing"
            self._save()

    def record_evidence(self, hypothesis_id: str, experiment_template: str,
                         result: Dict[str, Any], sample_size: int) -> Hypothesis:
        """Appends one experiment's result to the hypothesis's evidence
        trail, then derives the new status from ALL evidence so far (never
        just the latest result) -- and never below MIN_SAMPLE_SIZE_FOR_VERDICT,
        regardless of how conclusive a small sample looks."""
        rec = self._data.get(hypothesis_id)
        if rec is None:
            raise KeyError(f"unknown hypothesis: {hypothesis_id}")

        rec["evidence"].append({
            "template": experiment_template, "result": result,
            "sample_size": sample_size, "recorded_at": time.time(),
        })

        total_sample = sum(e["sample_size"] for e in rec["evidence"])
        if total_sample < MIN_SAMPLE_SIZE_FOR_VERDICT:
            rec["status"] = "inconclusive"
        else:
            # A simple, honest rule: the LATEST experiment's own verdict
            # field (if the template provides one) decides confirmed vs
            # rejected once sample size clears the bar. Templates are
            # responsible for their own "supported"/"not supported" logic;
            # the queue just enforces the sample-size discipline.
            supported = result.get("supported")
            if supported is True:
                rec["status"] = "confirmed"
            elif supported is False:
                rec["status"] = "rejected"
            else:
                rec["status"] = "inconclusive"

        self._data[hypothesis_id] = rec
        self._save()
        return Hypothesis(**rec)

    def all(self) -> List[Hypothesis]:
        return [Hypothesis(**rec) for rec in self._data.values()]