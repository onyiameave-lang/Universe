import json
import threading

from shared.learning import LearningLog


def test_slow_reflection_does_not_block_advice_or_other_records(tmp_path):
    reflection_started = threading.Event()
    finish_reflection = threading.Event()

    class SlowLLM:
        has_any = True

        def complete_json(self, **kwargs):
            reflection_started.set()
            if not finish_reflection.wait(timeout=3):
                raise TimeoutError("test reflection was not released")
            return {"lesson": "LLM lesson", "root_cause": "test",
                    "adjustment": "test"}, None

    learning = LearningLog("test", llm=SlowLLM(), storage_dir=str(tmp_path))
    first_record = threading.Thread(
        target=learning.record,
        args=("trade.propose", "error", False, {}, "failed"),
    )
    first_record.start()
    try:
        assert reflection_started.wait(timeout=1)
        assert learning.advice_for("trade.propose") == []

        second = learning.record(
            "trade.propose", "error", False, {}, "failed")
        assert second.lesson["source"] == "heuristic"
        assert learning._failure_signatures["trade.propose::failed"] == 2
    finally:
        finish_reflection.set()
        first_record.join(timeout=1)

    assert not first_record.is_alive()
    saved = json.loads((tmp_path / "test_learning.json").read_text())
    assert len(saved["lessons"]) == 2
    assert saved["failure_signatures"]["trade.propose::failed"] == 2


def test_pending_lesson_sync_recovers_after_restart(tmp_path):
    local = LearningLog("test", storage_dir=str(tmp_path))
    episode = local.record("trade.propose", "error", False, {}, "failed")

    saved = json.loads((tmp_path / "test_learning.json").read_text())
    assert len(saved["pending_chronicle"]) == 1
    assert saved["pending_chronicle"][0]["lesson"] == episode.lesson

    synced = threading.Event()
    calls = []

    class Chronicle:
        def store_memory(self, **kwargs):
            calls.append(kwargs)
            synced.set()
            return {"status": "complete", "memory_id": kwargs["memory_id"]}

    restarted = LearningLog(
        "test", chronicle=Chronicle(), storage_dir=str(tmp_path))
    restarted.start_sync_worker()
    try:
        assert synced.wait(timeout=2)
        for _ in range(100):
            state = json.loads((tmp_path / "test_learning.json").read_text())
            if not state["pending_chronicle"]:
                break
            threading.Event().wait(0.01)
        assert state["pending_chronicle"] == []
    finally:
        restarted.stop_sync_worker()

    assert calls[0]["memory_id"] == saved["pending_chronicle"][0]["memory_id"]
    assert calls[0]["domain"] == "learning"
    assert calls[0]["autolink"] is False


def test_failed_chronicle_write_retries_with_same_id(tmp_path, monkeypatch):
    monkeypatch.setattr("shared.learning._SYNC_RETRY_BASE_SEC", 0.01)
    attempts = []
    recovered = threading.Event()

    class Chronicle:
        def store_memory(self, **kwargs):
            attempts.append(kwargs["memory_id"])
            if len(attempts) == 1:
                return {"status": "error", "message": "temporarily unavailable"}
            recovered.set()
            return {"status": "complete", "memory_id": kwargs["memory_id"]}

    learning = LearningLog(
        "test", chronicle=Chronicle(), storage_dir=str(tmp_path))
    learning.record("trade.propose", "error", False, {}, "failed")
    try:
        assert recovered.wait(timeout=2)
        for _ in range(100):
            state = json.loads((tmp_path / "test_learning.json").read_text())
            if not state["pending_chronicle"]:
                break
            threading.Event().wait(0.01)
        assert state["pending_chronicle"] == []
    finally:
        learning.stop_sync_worker()

    assert len(attempts) >= 2
    assert len(set(attempts)) == 1
