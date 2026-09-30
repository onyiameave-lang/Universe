import json
import threading
import time
from unittest.mock import patch

from Chronicle.agents.chronicle_agent import ChronicleAgent


def test_chronicle_resumes_its_own_pending_learning_outbox(tmp_path):
    learning_path = tmp_path / "chronicle_learning.json"
    learning_path.write_text(json.dumps({
        "lessons": [],
        "failure_signatures": {},
        "pending_chronicle": [{
            "memory_id": "learning-chronicle-recovery",
            "lesson": {
                "lesson": "Persist Chronicle's own lesson",
                "root_cause": "test",
                "adjustment": "test",
            },
            "created_at": time.time(),
            "attempts": 0,
            "next_attempt_at": 0,
            "last_error": "",
        }],
    }), encoding="utf-8")

    sync_called = threading.Event()
    original_store_memory = ChronicleAgent.store_memory

    def track_store_memory(agent, *args, **kwargs):
        result = original_store_memory(agent, *args, **kwargs)
        sync_called.set()
        return result

    with patch.object(ChronicleAgent, "store_memory", track_store_memory):
        chronicle = ChronicleAgent(storage_dir=str(tmp_path))
        try:
            assert sync_called.wait(timeout=3)
        finally:
            if chronicle.learning is not None:
                chronicle.learning.stop_sync_worker()

    try:
        if chronicle.learning is not None:
            chronicle.learning.stop_sync_worker()
        state = json.loads(learning_path.read_text(encoding="utf-8"))
        assert state["pending_chronicle"] == []
        assert chronicle.store.get("learning-chronicle-recovery") is not None
    finally:
        if chronicle.learning is not None:
            chronicle.learning.stop_sync_worker()
