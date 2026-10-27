import json

from Forge.intelligence.hypothesis_queue import HypothesisQueue


class _Chronicle:
    def __init__(self):
        self.records = {}
        self.fail_writes = False

    def act(self, task, context):
        if task == "state.list":
            prefix = context.get("prefix", "")
            return {"status": "complete", "keys": sorted(
                key for key in self.records if key.startswith(prefix))}
        key = context["key"]
        if task == "state.get":
            current = self.records.get(key)
            return {"status": "complete", "found": current is not None,
                    "value": json.loads(json.dumps(current["value"])) if current else None,
                    "revision": current["revision"] if current else 0}
        if task == "state.set":
            if self.fail_writes:
                return {"status": "error"}
            current = self.records.get(key)
            expected = context["expected_revision"]
            if (current is None and expected != 0) or (
                    current is not None and current["revision"] != expected):
                return {"status": "conflict"}
            revision = 1 if current is None else current["revision"] + 1
            self.records[key] = {
                "value": json.loads(json.dumps(context["value"])), "revision": revision}
            return {"status": "complete", "revision": revision}
        raise AssertionError(task)


def test_chronicle_is_durable_hypothesis_authority_across_queue_restart(tmp_path, monkeypatch):
    import Forge.intelligence.hypothesis_queue as module

    legacy_path = tmp_path / "hypothesis_queue.json"
    monkeypatch.setattr(module, "DEFAULT_STORE_PATH", legacy_path)
    chronicle = _Chronicle()
    queue = HypothesisQueue(chronicle=chronicle)

    hypothesis = queue.add("Test whether a setup threshold suppresses entries.",
                           "oracle_no_trade_diagnosis", "HYP-chronicle-1")
    queue.mark_testing(hypothesis.id)
    queue.link_experiment(hypothesis.id, "experiment-1")
    updated = queue.record_evidence(
        hypothesis.id, "entry_ablation",
        {"supported": True, "evidence_role": "development_screen"}, sample_size=12)

    restored = HypothesisQueue(chronicle=chronicle).get(hypothesis.id)

    assert updated.status == "inconclusive"
    assert restored is not None
    assert restored.status == "inconclusive"
    assert restored.linked_experiment_ids == ["experiment-1"]
    assert len(restored.evidence) == 1
    assert not legacy_path.exists()
    assert len(chronicle.records) == 2  # hypothesis record plus one-time migration marker


def test_existing_local_hypotheses_are_imported_to_chronicle_once(tmp_path):
    legacy_path = tmp_path / "hypothesis_queue.json"
    legacy = {
        "HYP-existing": {
            "id": "HYP-existing", "statement": "Imported question.",
            "proposed_by": "legacy", "status": "untested",
            "created_at": 1.0, "evidence": [], "linked_experiment_ids": [],
        }
    }
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    chronicle = _Chronicle()

    queue = HypothesisQueue(store_path=legacy_path, chronicle=chronicle)
    restored = HypothesisQueue(chronicle=chronicle)

    assert queue.get("HYP-existing").statement == "Imported question."
    assert restored.get("HYP-existing").proposed_by == "legacy"
    assert len(chronicle.records) == 2
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy


def test_failed_chronicle_update_does_not_leave_unpersisted_evidence_in_ram(tmp_path, monkeypatch):
    import Forge.intelligence.hypothesis_queue as module

    monkeypatch.setattr(module, "DEFAULT_STORE_PATH", tmp_path / "empty-legacy-queue.json")
    chronicle = _Chronicle()
    queue = HypothesisQueue(chronicle=chronicle)
    hypothesis = queue.add("Preserve evidence on Chronicle failure.", "test", "HYP-fail-closed")
    chronicle.fail_writes = True

    try:
        queue.record_evidence(hypothesis.id, "test", {"supported": True}, sample_size=50)
    except RuntimeError as exc:
        assert "Chronicle" in str(exc)
    else:
        raise AssertionError("failed Chronicle write was reported as successful")

    assert queue.get(hypothesis.id).evidence == []
    assert HypothesisQueue(chronicle=chronicle).get(hypothesis.id).evidence == []


def test_production_queue_can_require_chronicle():
    try:
        HypothesisQueue(require_chronicle=True)
    except RuntimeError as exc:
        assert "requires Chronicle" in str(exc)
    else:
        raise AssertionError("production queue accepted missing Chronicle storage")


def test_generic_supported_flag_cannot_confirm_hypothesis_without_lockbox_provenance(tmp_path):
    queue = HypothesisQueue(store_path=tmp_path / "generic.json")
    hypothesis = queue.add("A generic result should not settle this.", "test", "HYP-generic")
    updated = queue.record_evidence(
        hypothesis.id, "atlas_research", {
            "supported": True, "evidence_role": "atlas_research_context",
            "research_status": "complete", "sources": [{"url": "https://example.test"}],
        }, sample_size=500)
    assert updated.status == "inconclusive"


def test_only_complete_claimed_lockbox_evidence_can_confirm_and_is_idempotent(tmp_path):
    chronicle = _Chronicle()
    queue = HypothesisQueue(store_path=tmp_path / "lockbox.json", chronicle=chronicle)
    hypothesis = queue.add("The registered challenger improves the metric.",
                           "test", "HYP-lockbox")
    queue.link_experiment(hypothesis.id, "experiment-lockbox-1")
    chronicle.records["forge.oracle.experiment.experiment-lockbox-1"] = {
        "revision": 1, "value": {
            "status": "complete", "experiment_id": "experiment-lockbox-1",
            "symbol": "EURUSD", "regime": "trending_up",
            "evaluation_mode": "lockbox_confirmation",
            "holdout_claim_status": "claimed_once",
            "dataset_fingerprint": "a" * 64, "holdout_fingerprint": "b" * 64,
            "evaluation_design": {"evidence_role": "single_lockbox_confirmation"},
            "candidate_generation": {"ablation_targets": ["oracle.entry"]},
            "evaluation": {"statistical_evidence_complete": True},
            "trade_sample": {"minimum_required": 100, "baseline_trades": 100,
                              "candidate_trades": 100, "stress_baseline_trades": 100,
                              "stress_candidate_trades": 100},
            "guardrails": {"quality": True},
        }}
    chronicle.records["forge.oracle.holdout." + "b" * 64] = {
        "revision": 1, "value": {"experiment_id": "experiment-lockbox-1",
                                  "symbol": "EURUSD", "regime": "trending_up"}}
    evidence = {
        "evidence_id": "experiment-lockbox-1", "experiment_id": "experiment-lockbox-1",
        "supported": True, "evidence_role": "single_lockbox_confirmation",
        "evaluation_status": "complete", "holdout_claim_status": "claimed_once",
        "dataset_fingerprint": "a" * 64, "holdout_fingerprint": "b" * 64,
        "minimum_sample_met": True, "ablation_target": "oracle.entry",
        "guardrails": {"quality": True},
    }
    updated = queue.record_evidence(hypothesis.id, "oracle_entry_lockbox", evidence, 100)
    duplicate = queue.record_evidence(hypothesis.id, "oracle_entry_lockbox", evidence, 100)
    assert updated.status == "confirmed"
    assert duplicate.status == "confirmed"
    assert len(duplicate.evidence) == 1


def test_conflicting_independent_lockboxes_return_to_inconclusive(tmp_path):
    chronicle = _Chronicle()
    queue = HypothesisQueue(store_path=tmp_path / "conflict.json", chronicle=chronicle)
    hypothesis = queue.add("The evidence must agree.", "test", "HYP-conflict")
    for number, supported in ((1, True), (2, False)):
        experiment_id = f"experiment-{number}"
        holdout_fingerprint = str(number + 2) * 64
        dataset_fingerprint = str(number) * 64
        guardrails = {"quality": supported}
        queue.link_experiment(hypothesis.id, experiment_id)
        chronicle.records[f"forge.oracle.experiment.{experiment_id}"] = {
            "revision": 1, "value": {
                "status": "complete", "experiment_id": experiment_id,
                "symbol": "EURUSD", "regime": "trending_up",
                "evaluation_mode": "lockbox_confirmation",
                "holdout_claim_status": "claimed_once",
                "dataset_fingerprint": dataset_fingerprint,
                "holdout_fingerprint": holdout_fingerprint,
                "evaluation_design": {"evidence_role": "single_lockbox_confirmation"},
                "candidate_generation": {"ablation_targets": ["oracle.entry"]},
                "evaluation": {"statistical_evidence_complete": True},
                "trade_sample": {"minimum_required": 100, "baseline_trades": 100,
                                  "candidate_trades": 100, "stress_baseline_trades": 100,
                                  "stress_candidate_trades": 100},
                "guardrails": guardrails,
            }}
        chronicle.records[f"forge.oracle.holdout.{holdout_fingerprint}"] = {
            "revision": 1, "value": {"experiment_id": experiment_id,
                                      "symbol": "EURUSD", "regime": "trending_up"}}
        queue.record_evidence(hypothesis.id, "oracle_lockbox", {
            "evidence_id": experiment_id, "experiment_id": experiment_id,
            "supported": supported, "evidence_role": "single_lockbox_confirmation",
            "evaluation_status": "complete", "holdout_claim_status": "claimed_once",
            "dataset_fingerprint": dataset_fingerprint,
            "holdout_fingerprint": holdout_fingerprint,
            "minimum_sample_met": True, "ablation_target": "oracle.entry",
            "guardrails": guardrails,
        }, 100)
    assert queue.get(hypothesis.id).status == "inconclusive"


def test_hypothesis_evidence_survives_reopening_real_chronicle_state_store(tmp_path, monkeypatch):
    import Forge.intelligence.hypothesis_queue as module
    from Chronicle.core.state_store import StateStore

    legacy_path = tmp_path / "hypothesis_queue.json"
    monkeypatch.setattr(module, "DEFAULT_STORE_PATH", legacy_path)

    class ChronicleStateAPI:
        """Exercise HypothesisQueue through Chronicle's real file-backed store."""
        def __init__(self, storage_dir):
            self.state = StateStore(str(storage_dir))

        def act(self, task, context):
            if task == "state.list":
                return {"status": "complete", "keys": self.state.list_keys(
                    context.get("prefix", ""))}
            key = context["key"]
            if task == "state.get":
                return {"status": "complete", "key": key,
                        **self.state.get_with_revision(key)}
            if task == "state.set":
                return self.state.set(
                    key, context["value"], owner=context.get("_sender", ""),
                    expected_revision=context["expected_revision"])
            raise AssertionError(task)

    first_chronicle = ChronicleStateAPI(tmp_path / "chronicle")
    queue = HypothesisQueue(chronicle=first_chronicle)
    hypothesis = queue.add("Check whether entry filtering causes inactivity.",
                           "oracle_watchdog", "HYP-real-state-store")
    queue.mark_testing(hypothesis.id)
    queue.link_experiment(hypothesis.id, "exp-real-1")
    queue.record_evidence(
        hypothesis.id, "entry_ablation",
        {"supported": True, "evidence_role": "development_screen"}, sample_size=18)

    # A new StateStore instance forces a disk reload, modeling Chronicle/API restart.
    restarted_chronicle = ChronicleStateAPI(tmp_path / "chronicle")
    restored = HypothesisQueue(chronicle=restarted_chronicle).get(hypothesis.id)

    assert restored is not None
    assert restored.status == "inconclusive"
    assert restored.linked_experiment_ids == ["exp-real-1"]
    assert restored.evidence[0]["result"]["evidence_role"] == "development_screen"
    assert not legacy_path.exists()
    assert (tmp_path / "chronicle" / "state_store.json").exists()
