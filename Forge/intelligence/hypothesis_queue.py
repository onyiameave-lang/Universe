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
import hashlib
import threading
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
    proposed_by: str  # which agent/process proposed this (e.g. "chronicle_research_director")
    status: str = "untested"
    created_at: float = field(default_factory=time.time)
    evidence: List[Dict[str, Any]] = field(default_factory=list)  # append-only
    # --- V2 Phase 1 Linking Field (Optional) ---
    linked_experiment_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HypothesisQueue:
    def __init__(self, store_path: Optional[Path] = None, chronicle: Any = None,
                 require_chronicle: bool = False):
        if require_chronicle and chronicle is None:
            raise RuntimeError("the Forge hypothesis queue requires Chronicle durability")
        self.chronicle = chronicle
        self.store_path = (Path(store_path) if store_path is not None else
                           (None if chronicle is not None else DEFAULT_STORE_PATH))
        if self.store_path is not None:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}
        if chronicle is None:
            self._data = self._load(self.store_path)
        else:
            self._data = self._load_chronicle()
            # One-time import of the old local queue. Chronicle wins if an ID
            # already exists there; the old file is retained as a migration source.
            legacy_path = Path(store_path) if store_path is not None else DEFAULT_STORE_PATH
            self._migrate_legacy_local_queue(legacy_path)

    @staticmethod
    def _load(path: Optional[Path]) -> Dict[str, Any]:
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("hypothesis queue must be a JSON object")
                return {str(key): value for key, value in loaded.items()
                        if isinstance(value, dict) and value.get("id") == str(key)}
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("hypothesis_queue.json unreadable (%s); starting fresh", exc)
                return {}
        return {}

    @staticmethod
    def _chronicle_key(hypothesis_id: str) -> str:
        digest = hashlib.sha256(hypothesis_id.encode("utf-8")).hexdigest()
        return f"forge.hypothesis_queue.record.{digest}"

    def _load_chronicle(self) -> Dict[str, Dict[str, Any]]:
        listed = self.chronicle.act("state.list", {
            "prefix": "forge.hypothesis_queue.record.", "_sender": "forge"})
        if not isinstance(listed, dict) or listed.get("status") != "complete":
            raise RuntimeError("Chronicle could not list the Forge hypothesis queue")
        loaded: Dict[str, Dict[str, Any]] = {}
        for key in listed.get("keys", []):
            current = self.chronicle.act("state.get", {"key": key, "_sender": "forge"})
            record = current.get("value") if isinstance(current, dict) else None
            if (isinstance(current, dict) and current.get("status") == "complete"
                    and current.get("found") and isinstance(record, dict)
                    and isinstance(record.get("id"), str)
                    and self._chronicle_key(record["id"]) == key):
                loaded[record["id"]] = record
        return loaded

    def _migrate_legacy_local_queue(self, legacy_path: Path) -> None:
        marker_key = "forge.hypothesis_queue.migration.local_json_v1"
        marker = self.chronicle.act("state.get", {"key": marker_key, "_sender": "forge"})
        if not isinstance(marker, dict) or marker.get("status") != "complete":
            raise RuntimeError("Chronicle hypothesis migration marker lookup failed")
        if marker.get("found"):
            return
        imported = 0
        for hypothesis_id, record in self._load(legacy_path).items():
            if hypothesis_id in self._data:
                continue
            self._data[hypothesis_id] = record
            self._save(hypothesis_id)
            imported += 1
        saved = self.chronicle.act("state.set", {
            "key": marker_key,
            "value": {"schema": "forge.hypothesis_queue_migration.v1",
                      "legacy_source": "local_json", "imported_records": imported,
                      "completed_at_unix": time.time()},
            "expected_revision": marker.get("revision", 0), "_sender": "forge",
        })
        if not isinstance(saved, dict) or saved.get("status") != "complete":
            raise RuntimeError("Chronicle could not record hypothesis queue migration completion")

    def _save(self, hypothesis_id: Optional[str] = None) -> None:
        if self.chronicle is not None:
            ids = [hypothesis_id] if hypothesis_id is not None else list(self._data)
            for current_id in ids:
                record = self._data.get(current_id)
                if not isinstance(record, dict):
                    continue
                key = self._chronicle_key(current_id)
                current = self.chronicle.act("state.get", {"key": key, "_sender": "forge"})
                if not isinstance(current, dict) or current.get("status") != "complete":
                    raise RuntimeError("Chronicle hypothesis record lookup failed")
                saved = self.chronicle.act("state.set", {
                    "key": key, "value": record,
                    "expected_revision": current.get("revision", 0), "_sender": "forge",
                })
                if not isinstance(saved, dict) or saved.get("status") != "complete":
                    raise RuntimeError("Chronicle could not persist the Forge hypothesis record")
            return
        if self.store_path is None:
            return
        try:
            self.store_path.write_text(json.dumps(self._data, indent=2, sort_keys=True),
                                       encoding="utf-8")
        except OSError as exc:
            log.warning("could not persist hypothesis_queue.json: %s", exc)

    def add(self, statement: str, proposed_by: str, hypothesis_id: Optional[str] = None) -> Hypothesis:
        """Adds a new hypothesis at 'untested'. If hypothesis_id is omitted,
        one is generated. Returns the existing hypothesis unchanged if an
        identical statement is already queued (no duplicate questions)."""
        with self._lock:
            for existing in self._data.values():
                if existing["statement"] == statement and existing["status"] in ("untested", "testing"):
                    return Hypothesis(**existing)

            hid = hypothesis_id or f"HYP-{int(time.time())}-{len(self._data) + 1}"
            if hid in self._data:
                if self._data[hid].get("statement") != statement:
                    raise ValueError("hypothesis ID already exists with a different statement")
                return Hypothesis(**self._data[hid])
            h = Hypothesis(id=hid, statement=statement, proposed_by=proposed_by)
            self._data[hid] = h.to_dict()
            try:
                self._save(hid)
            except Exception:
                self._data.pop(hid, None)
                raise
            log.info("[%s] new hypothesis queued: %s", hid, statement)
            return h

    def get(self, hypothesis_id: str) -> Optional[Hypothesis]:
        with self._lock:
            rec = self._data.get(hypothesis_id)
            return Hypothesis(**rec) if rec else None

    def get_pending(self) -> List[Hypothesis]:
        """Hypotheses at 'untested' -- ready for Forge to pick up and test."""
        with self._lock:
            return [Hypothesis(**rec) for rec in self._data.values() if rec["status"] == "untested"]

    def mark_testing(self, hypothesis_id: str) -> None:
        with self._lock:
            if hypothesis_id in self._data:
                old_status = self._data[hypothesis_id]["status"]
                self._data[hypothesis_id]["status"] = "testing"
                try:
                    self._save(hypothesis_id)
                except Exception:
                    self._data[hypothesis_id]["status"] = old_status
                    raise

    def record_evidence(self, hypothesis_id: str, experiment_template: str,
                        result: Dict[str, Any], sample_size: int) -> Hypothesis:
        """Append one experiment result, deduplicating identified evidence.

        Only a complete, claimed-once lockbox with durable fingerprints and
        a sufficient sample can confirm or reject a hypothesis. Development
        screens and generic templates remain inconclusive.
        """
        with self._lock:
            rec = self._data.get(hypothesis_id)
            if rec is None:
                raise KeyError(f"unknown hypothesis: {hypothesis_id}")
            if (not isinstance(result, dict)
                    or not isinstance(sample_size, int)
                    or isinstance(sample_size, bool) or sample_size < 0):
                raise ValueError("hypothesis evidence requires a result object and non-negative integer sample size")
            evidence_id = result.get("evidence_id") or result.get("experiment_id") or result.get("run_id")
            if evidence_id is not None:
                evidence_id = str(evidence_id).strip()
                if not evidence_id:
                    raise ValueError("evidence_id cannot be blank")
                if any(item.get("evidence_id") == evidence_id for item in rec["evidence"]):
                    return Hypothesis(**rec)
            old = json.loads(json.dumps(rec))
            evidence_entry = {
                "template": experiment_template, "result": result,
                "sample_size": sample_size, "recorded_at": time.time(),
            }
            if evidence_id is not None:
                evidence_entry["evidence_id"] = evidence_id
            rec["evidence"].append(evidence_entry)

            # A numeric sample and a caller-supplied `supported` flag are not
            # enough to settle a hypothesis. Only a complete, claimed-once
            # lockbox evaluation with an immutable data fingerprint can do so.
            # Development screens and generic templates remain exploratory.
            lockbox_evidence = []
            seen_holdouts = set()
            for item in rec["evidence"]:
                evidence = item.get("result", {})
                fingerprint = evidence.get("holdout_fingerprint")
                dataset_fingerprint = evidence.get("dataset_fingerprint")
                valid = (
                    evidence.get("evidence_role") == "single_lockbox_confirmation"
                    and evidence.get("evaluation_status") == "complete"
                    and evidence.get("holdout_claim_status") == "claimed_once"
                    and isinstance(fingerprint, str) and len(fingerprint) == 64
                    and all(char in "0123456789abcdef" for char in fingerprint.lower())
                    and isinstance(dataset_fingerprint, str)
                    and len(dataset_fingerprint) == 64
                    and all(char in "0123456789abcdef" for char in dataset_fingerprint.lower())
                    and isinstance(evidence.get("experiment_id"), str)
                    and bool(evidence["experiment_id"].strip())
                    and evidence.get("minimum_sample_met") is True
                    and isinstance(evidence.get("supported"), bool)
                    and isinstance(item.get("sample_size"), int)
                    and not isinstance(item.get("sample_size"), bool)
                    and item["sample_size"] >= MIN_SAMPLE_SIZE_FOR_VERDICT
                    and self._verify_lockbox_against_chronicle(rec, item)
                )
                if valid and fingerprint not in seen_holdouts:
                    lockbox_evidence.append((fingerprint, evidence["supported"]))
                    seen_holdouts.add(fingerprint)
            verdicts = {supported for _, supported in lockbox_evidence}
            if lockbox_evidence and len(verdicts) == 1:
                rec["status"] = "confirmed" if True in verdicts else "rejected"
            else:
                rec["status"] = "inconclusive"

            self._data[hypothesis_id] = rec
            try:
                self._save(hypothesis_id)
            except Exception:
                self._data[hypothesis_id] = old
                raise
            return Hypothesis(**rec)

    def _verify_lockbox_against_chronicle(self, hypothesis: Dict[str, Any],
                                          evidence_entry: Dict[str, Any]) -> bool:
        """Prove verdict evidence matches the linked immutable Chronicle run."""
        if self.chronicle is None:
            return False
        evidence = evidence_entry.get("result", {})
        experiment_id = evidence.get("experiment_id")
        if (not isinstance(experiment_id, str)
                or experiment_id not in hypothesis.get("linked_experiment_ids", [])):
            return False
        try:
            stored = self.chronicle.act("state.get", {
                "key": f"forge.oracle.experiment.{experiment_id}", "_sender": "forge"})
            claim = self.chronicle.act("state.get", {
                "key": f"forge.oracle.holdout.{evidence.get('holdout_fingerprint')}",
                "_sender": "forge"})
        except Exception:
            return False
        record = stored.get("value") if isinstance(stored, dict) else None
        claim_value = claim.get("value") if isinstance(claim, dict) else None
        design = record.get("evaluation_design") if isinstance(record, dict) else None
        evaluation = record.get("evaluation") if isinstance(record, dict) else None
        candidate_generation = record.get("candidate_generation") if isinstance(record, dict) else None
        if (not isinstance(stored, dict) or stored.get("status") != "complete"
                or not stored.get("found") or not isinstance(record, dict)
                or not isinstance(claim, dict) or claim.get("status") != "complete"
                or not claim.get("found") or not isinstance(claim_value, dict)
                or claim_value.get("experiment_id") != experiment_id
                or claim_value.get("symbol") != record.get("symbol")
                or claim_value.get("regime") != record.get("regime")
                or record.get("status") != "complete"
                or record.get("experiment_id") != experiment_id
                or record.get("evaluation_mode") != "lockbox_confirmation"
                or record.get("holdout_claim_status") != "claimed_once"
                or not isinstance(design, dict)
                or not isinstance(evaluation, dict)
                or not isinstance(candidate_generation, dict)
                or record.get("dataset_fingerprint") != evidence.get("dataset_fingerprint")
                or record.get("holdout_fingerprint") != evidence.get("holdout_fingerprint")
                or design.get("evidence_role") != "single_lockbox_confirmation"
                or evaluation.get("statistical_evidence_complete") is not True
                or record.get("guardrails") != evidence.get("guardrails")
                or candidate_generation.get("ablation_targets") != [evidence.get("ablation_target")]):
            return False
        samples = record.get("trade_sample", {})
        minimum = samples.get("minimum_required")
        sample_counts = [samples.get(field) for field in (
            "baseline_trades", "candidate_trades", "stress_baseline_trades",
            "stress_candidate_trades")]
        return (
            isinstance(minimum, int) and not isinstance(minimum, bool) and minimum >= 100
            and all(isinstance(value, int) and not isinstance(value, bool)
                    and value >= minimum for value in sample_counts)
            and evidence.get("minimum_sample_met") is True
            and evidence_entry.get("sample_size") == min(sample_counts)
        )

    def link_experiment(self, hypothesis_id: str, experiment_id: str) -> Hypothesis:
        """Idempotently link a persisted experiment to its research question."""
        with self._lock:
            rec = self._data.get(hypothesis_id)
            if rec is None:
                raise KeyError(f"unknown hypothesis: {hypothesis_id}")
            normalized = str(experiment_id).strip()
            if not normalized:
                raise ValueError("experiment_id is required")
            linked = list(rec.get("linked_experiment_ids", []))
            if normalized not in linked:
                old_linked = linked.copy()
                linked.append(normalized)
                rec["linked_experiment_ids"] = linked
                self._data[hypothesis_id] = rec
                try:
                    self._save(hypothesis_id)
                except Exception:
                    rec["linked_experiment_ids"] = old_linked
                    self._data[hypothesis_id] = rec
                    raise
            return Hypothesis(**rec)

    def all(self) -> List[Hypothesis]:
        with self._lock:
            return [Hypothesis(**rec) for rec in self._data.values()]
