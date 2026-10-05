from pathlib import Path
from types import SimpleNamespace
import threading
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


def test_record_close_sends_price_direction_and_actual_trade_result(monkeypatch):
    cases = (
        ("buy", 100.0, 110.0, 1, True),
        ("buy", 100.0, 90.0, -1, False),
        ("sell", 100.0, 90.0, -1, True),
        ("sell", 100.0, 110.0, 1, False),
        ("sell", 100.0, 100.0, 0, False),
    )
    monkeypatch.setattr(trade_learning, "_append_experiment_log", lambda *args, **kwargs: None)

    for direction, entry, exit_price, expected_direction, expected_win in cases:
        engine = trade_learning.TradeLearningEngine.__new__(trade_learning.TradeLearningEngine)
        engine.confidence = SimpleNamespace(
            record_outcome=lambda *args: {
                "live_confidence": 0.5, "demo_trades": 1, "wins": 1, "losses": 0,
            },
            maybe_trigger_reevolution=lambda *args: None,
        )
        engine.benchmark = SimpleNamespace(record_trade=lambda outcome: None)
        calls = []
        oracle = SimpleNamespace(
            act=lambda task, context: calls.append((task, context)),
            sentinel=None,
            chronicle=None,
        )
        position = SimpleNamespace(
            symbol="EURUSD",
            direction=SimpleNamespace(value=direction),
            entry_price=entry,
            initial_stop=entry - 5 if direction == "buy" else entry + 5,
            entry_time=time.time() - 60,
            entry_streams={},
            entry_term_evidence={},
            entry_regime="ranging",
            entry_confidence=0.7,
            journal_id=None,
        )

        engine.record_close(oracle, position, exit_price, 0.7, "ranging", "test")

        assert calls[0][0] == "fusion.learn"
        assert calls[0][1]["realized_direction"] == expected_direction
        assert calls[0][1]["trade_won"] is expected_win


def test_legacy_fusion_outcome_state_is_reset_without_losing_weights():
    from Oracle.intelligence.adaptive_fusion import AdaptiveFusion

    legacy_weights = {"technical": 0.5, "news": 0.2, "social": 0.2, "memory": 0.1}
    state = {
        "EURUSD": {
            "weights": dict(legacy_weights),
            "entry_threshold": 0.08,
            "stream_hits": {},
            "trades": 20,
            "wins": 18,
        }
    }
    fusion = AdaptiveFusion.__new__(AdaptiveFusion)
    fusion._lock = threading.RLock()
    fusion._store = SimpleNamespace(data=lambda: state, save=lambda: None)

    updated = fusion._sym("EURUSD")

    assert updated["entry_threshold"] == 0.15
    assert updated["outcome_trades"] == 0
    assert updated["outcome_wins"] == 0
    assert updated["weights"] == legacy_weights