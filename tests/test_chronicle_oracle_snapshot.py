from __future__ import annotations

import copy
import hashlib
import json

from Chronicle.core.state_store import StateStore
from Chronicle.tools.forge_oracle_state_snapshot import main as snapshot_main


def test_oracle_namespace_snapshot_roundtrip_preserves_history_and_excludes_other_agents(tmp_path):
    source = StateStore(str(tmp_path / "source"))
    first = source.set("forge.oracle.case.1", {"status": "queued"}, owner="forge")
    assert first["status"] == "complete"
    second = source.set("forge.oracle.case.1", {"status": "complete"},
                        owner="forge", expected_revision=first["revision"])
    assert second["status"] == "complete"
    assert source.set("atlas.research.1", {"private": "out of scope"},
                      owner="atlas")["status"] == "complete"

    snapshot = source.export_snapshot("forge.oracle.")
    assert snapshot["record_count"] == 1
    assert set(snapshot["records"]) == {"forge.oracle.case.1"}
    assert source.export_snapshot("forge.oracle.") == snapshot

    destination = StateStore(str(tmp_path / "destination"))
    assert destination.set("atlas.local.1", {"keep": True}, owner="atlas")["status"] == "complete"
    restored = destination.restore_snapshot(snapshot, "forge.oracle.")
    assert restored == {"status": "complete", "restored": 1,
                        "skipped_newer": 0, "record_count": 1}
    assert destination.get("forge.oracle.case.1") == {"status": "complete"}
    assert destination.get_revision("forge.oracle.case.1") == second["revision"]
    assert [item["value"] for item in destination.get_history("forge.oracle.case.1")] == [
        {"status": "queued"}, {"status": "complete"},
    ]
    assert destination.get("atlas.local.1") == {"keep": True}
    assert destination.get("atlas.research.1") is None


def test_oracle_namespace_restore_rejects_foreign_keys_and_preserves_newer_local_revision(tmp_path):
    snapshot_source = StateStore(str(tmp_path / "source"))
    snapshot_source.set("forge.oracle.lesson.1", {"score": 1}, owner="forge")
    snapshot = snapshot_source.export_snapshot("forge.oracle.")
    tampered = copy.deepcopy(snapshot)
    tampered["records"]["forge.oracle.lesson.1"]["value"]["score"] = 9
    destination = StateStore(str(tmp_path / "destination"))
    invalid = destination.restore_snapshot(tampered, "forge.oracle.")
    assert invalid["status"] == "error"
    assert "fingerprint" in invalid["message"]

    foreign_namespace = copy.deepcopy(snapshot)
    foreign_namespace["records"]["atlas.lesson.1"] = {
        "value": {"score": 99}, "owner": "atlas", "updated_at": 1,
        "history": [], "revision": 1,
    }
    foreign_namespace["record_count"] = len(foreign_namespace["records"])
    encoded_records = json.dumps(foreign_namespace["records"], sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
    foreign_namespace["records_sha256"] = hashlib.sha256(
        encoded_records.encode("utf-8")).hexdigest()
    assert destination.restore_snapshot(foreign_namespace, "forge.oracle.")["status"] == "error"
    assert destination.list_keys() == []

    clean_snapshot = snapshot_source.export_snapshot("forge.oracle.")
    destination.set("forge.oracle.lesson.1", {"score": 2}, owner="forge")
    destination.set("forge.oracle.lesson.1", {"score": 3}, owner="forge",
                    expected_revision=1)
    restored = destination.restore_snapshot(clean_snapshot, "forge.oracle.")
    assert restored["status"] == "complete"
    assert restored["skipped_newer"] == 1
    assert destination.get("forge.oracle.lesson.1") == {"score": 3}


def test_snapshot_cli_exports_and_restores_state_between_clean_stores(tmp_path, capsys):
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    snapshot_path = tmp_path / "oracle-state.json"
    source = StateStore(str(source_dir))
    source.set("forge.oracle.job.1", {"status": "queued"}, owner="forge")

    assert snapshot_main(["export", "--storage-dir", str(source_dir),
                          "--snapshot", str(snapshot_path)]) == 0
    assert "Exported 1" in capsys.readouterr().out
    assert snapshot_main(["restore", "--storage-dir", str(destination_dir),
                          "--snapshot", str(snapshot_path)]) == 0
    assert "Restored Forge Oracle Chronicle state: 1 records" in capsys.readouterr().out
    assert StateStore(str(destination_dir)).get("forge.oracle.job.1") == {"status": "queued"}
