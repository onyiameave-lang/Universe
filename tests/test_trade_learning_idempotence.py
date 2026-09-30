from pathlib import Path
from types import SimpleNamespace
import time

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


def test_record_close_uses_price_direction_for_fusion_learning(monkeypatch):
    cases = (
        ("buy", 100.0, 110.0, 1),
        ("buy", 100.0, 90.0, -1),
        ("sell", 100.0, 90.0, -1),
        ("sell", 100.0, 110.0, 1),
    )

    for direction, entry, exit_price, expected_direction in cases:
        engine = trade_learning.TradeLearningEngine.__new__(trade_learning.TradeLearningEngine)
        engine.confidence = SimpleNamespace(
            record_outcome=lambda *args: {
                "live_confidence": 0.5, "demo_trades": 1, "wins": 1, "losses": 0,
            },
            maybe_trigger_reevolution=lambda *args: None,
        )
        engine.benchmark = SimpleNamespace(record_trade=lambda outcome: None)
        captured = []
        oracle = SimpleNamespace(
            act=lambda task, context: captured.append((task, context)),
            sentinel=None,
            chronicle=None,
        )
        pos = SimpleNamespace(
            symbol="EURUSD",
            direction=SimpleNamespace(value=direction),
            entry_price=entry,
            initial_stop=entry - 5 if direction == "buy" else entry + 5,
            entry_time=time.time() - 60,
            entry_streams={},
            entry_term_evidence={},
            entry_regime="trending_up",
            entry_confidence=0.7,
            journal_id=None,
        )
        monkeypatch.setattr(trade_learning, "_append_experiment_log", lambda *a, **k: None)

        engine.record_close(oracle, pos, exit_price, 0.7, "trending_up", "test")

        assert captured[0][0] == "fusion.learn"
        assert captured[0][1]["realized_direction"] == expected_direction