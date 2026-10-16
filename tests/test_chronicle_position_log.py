import json

from Oracle.execution import chronicle_position_log
from Oracle.execution.chronicle_position_log import ChroniclePositionLog


class _FailingChronicle:
    def act(self, task, context):
        raise ConnectionError("Chronicle unavailable")


class _SuccessfulChronicle:
    def act(self, task, context):
        return {"status": "complete"}


def test_failed_chronicle_write_is_appended_to_local_fallback(tmp_path, monkeypatch, caplog):
    fallback = tmp_path / "state" / "position-events.jsonl"

    def fail_rest(*args, **kwargs):
        raise OSError("Chronicle REST unavailable")

    monkeypatch.setattr(chronicle_position_log.urllib.request, "urlopen", fail_rest)
    writer = ChroniclePositionLog(
        chronicle_agent=_FailingChronicle(),
        trader_id="ctrader_demo",
        local_fallback_path=fallback,
    )

    writer.log_modified("EURUSD", "EURUSD", 244400714, sl=1.13429, tp=1.14000)

    [line] = fallback.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    assert event["event"] == "position_modified"
    assert event["symbol"] == "EURUSD"
    assert event["ticket"] == 244400714
    assert event["sl"] == 1.13429
    assert event["tp"] == 1.14000
    assert event["trader_id"] == "ctrader_demo"
    assert isinstance(event["ts"], float)
    assert "saved position event to local fallback" in caplog.text


def test_successful_chronicle_write_does_not_append_local_fallback(
        tmp_path, monkeypatch):
    fallback = tmp_path / "position-events.jsonl"

    def fail_rest(*args, **kwargs):
        raise AssertionError("REST should not run after successful agent write")

    monkeypatch.setattr(chronicle_position_log.urllib.request, "urlopen", fail_rest)
    writer = ChroniclePositionLog(
        chronicle_agent=_SuccessfulChronicle(),
        local_fallback_path=fallback,
    )

    writer.log_modified("EURUSD", "EURUSD", 244400714, sl=1.13429)

    assert not fallback.exists()


def test_explicit_chronicle_error_status_uses_local_fallback(
        tmp_path, monkeypatch):
    class _ErrorChronicle:
        def act(self, task, context):
            return {"status": "error", "message": "store rejected"}

    def fail_rest(*args, **kwargs):
        raise OSError("REST unavailable")

    monkeypatch.setattr(chronicle_position_log.urllib.request, "urlopen", fail_rest)
    fallback = tmp_path / "position-events.jsonl"
    writer = ChroniclePositionLog(
        chronicle_agent=_ErrorChronicle(),
        local_fallback_path=fallback,
    )

    writer.log_modified("EURUSD", "EURUSD", 244400714, sl=1.13429)

    assert len(fallback.read_text(encoding="utf-8").splitlines()) == 1
