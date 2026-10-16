import json
from concurrent.futures import ThreadPoolExecutor

from Chronicle.agents.chronicle_agent import ChronicleAgent
from Chronicle.core.memory_record import MemoryRecord
from Chronicle.core.vector_store import VectorStore


def test_invalid_store_is_quarantined_without_overwriting_existing_backup(tmp_path):
    path = tmp_path / "records.json"
    backup = tmp_path / "records.corrupt.json"
    path.write_text("{invalid current json", encoding="utf-8")
    backup.write_text("{invalid previous backup", encoding="utf-8")

    store = VectorStore(str(tmp_path))

    assert path.is_file() is False
    assert backup.read_text(encoding="utf-8") == "{invalid previous backup"
    quarantined = list(tmp_path.glob("records.corrupt.*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{invalid current json"

    store.add(MemoryRecord(
        memory_id="chronicle-recovered-startup",
        content="Chronicle can start after quarantining invalid records",
        embedding=[1.0, 0.0],
    ))
    assert json.loads(path.read_text(encoding="utf-8"))[0]["memory_id"] == (
        "chronicle-recovered-startup"
    )


def test_chronicle_agent_starts_and_stores_when_corrupt_backup_already_exists(tmp_path):
    path = tmp_path / "records.json"
    backup = tmp_path / "records.corrupt.json"
    path.write_text("{invalid current json", encoding="utf-8")
    backup.write_text("{invalid previous backup", encoding="utf-8")

    chronicle = ChronicleAgent(storage_dir=str(tmp_path))
    result = chronicle.store_memory(
        content="Chronicle started and accepted this local test record",
        domain="test",
        autolink=False,
        memory_id="chronicle-startup-recovery",
    )

    assert result["status"] == "complete"
    assert chronicle.store.get("chronicle-startup-recovery") is not None
    assert backup.read_text(encoding="utf-8") == "{invalid previous backup"


def test_separate_store_instances_preserve_concurrent_writes(tmp_path):
    stores = [VectorStore(str(tmp_path)) for _ in range(8)]

    def add_record(index):
        stores[index].add(MemoryRecord(
            memory_id=f"concurrent-{index}",
            content=f"concurrent record {index}",
            embedding=[float(index + 1), 1.0],
        ))

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        list(pool.map(add_record, range(len(stores))))

    reloaded = VectorStore(str(tmp_path))
    assert {record.memory_id for record in reloaded.all()} == {
        f"concurrent-{index}" for index in range(len(stores))
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_vector_store_logs_and_recovers_from_invalid_existing_json(tmp_path):
    store = VectorStore(str(tmp_path))
    store._path.write_text("{invalid json", encoding="utf-8")

    record = MemoryRecord(
        memory_id="learning-oracle-recovery",
        content="A durable learning record",
        summary="A durable learning record",
        embedding=[1.0, 0.0],
    )
    store.add(record)

    assert store.get("learning-oracle-recovery") is record
    assert store._path.read_text(encoding="utf-8").startswith("[")


def test_chronicle_store_memory_upserts_by_outbox_memory_id(tmp_path):
    class Embedder:
        def encode(self, text):
            return [1.0, 0.0]

    chronicle = ChronicleAgent.__new__(ChronicleAgent)
    chronicle.embedder = Embedder()
    chronicle.store = VectorStore(str(tmp_path))

    first = chronicle.store_memory(
        content="first version", summary="first", domain="learning",
        memory_id="learning-oracle-stable", autolink=False,
    )
    second = chronicle.store_memory(
        content="updated version", summary="updated", domain="learning",
        memory_id="learning-oracle-stable", autolink=False,
    )

    assert first["status"] == "complete"
    assert second["status"] == "complete"
    assert chronicle.store.stats()["total_records"] == 1
    assert chronicle.store.get("learning-oracle-stable").content == "updated version"
