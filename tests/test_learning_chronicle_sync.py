from Chronicle.agents.chronicle_agent import ChronicleAgent
from Chronicle.core.memory_record import MemoryRecord
from Chronicle.core.vector_store import VectorStore


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
