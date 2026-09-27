from Chronicle.agents.chronicle_agent import ChronicleAgent
from Chronicle.core.vector_store import VectorStore


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
