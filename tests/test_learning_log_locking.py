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
