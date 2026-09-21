from pathlib import Path

from Oracle.intelligence import trade_learning


def test_record_close_once_skips_duplicate_close(tmp_path, monkeypatch):
    ledger = tmp_path / "learned_closed_trades.json"
    monkeypatch.setattr(trade_learning, "_CLOSED_TRADE_KEYS_PATH", Path(ledger))

    engine = trade_learning.TradeLearningEngine.__new__(trade_learning.TradeLearningEngine)
    calls = []

    def record_close(*args, **kwargs):
        calls.append((args, kwargs))
        return "recorded"

    engine.record_close = record_close
    position = type("Position", (), {"symbol": "EURUSD"})()

    first = engine.record_close_once(
        "EURUSD:123:broker-close", object(), position, 1.0, 0.5, "ranging", "closed")
    second = engine.record_close_once(
        "EURUSD:123:broker-close", object(), position, 1.0, 0.5, "ranging", "closed")

    assert first == "recorded"
    assert second is None
    assert len(calls) == 1