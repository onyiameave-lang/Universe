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
        ("buy", 100.0, 110.0, 1, True),
        ("buy", 100.0, 90.0, -1, False),
        ("sell", 100.0, 90.0, -1, True),
        ("sell", 100.0, 110.0, 1, False),
        ("sell", 100.0, 100.0, 0, False),
    )

    for direction, entry, exit_price, expected_direction, expected_win in cases:
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
        assert captured[0][1]["trade_won"] is expected_win


def test_demo_feedback_sends_normalized_entry_context_to_forge(monkeypatch):
    engine = trade_learning.TradeLearningEngine.__new__(trade_learning.TradeLearningEngine)
    engine.confidence = SimpleNamespace(
        record_outcome=lambda *args: {
            "live_confidence": 0.5, "demo_trades": 1, "wins": 0, "losses": 1,
        },
        maybe_trigger_reevolution=lambda *args: None,
    )
    engine.benchmark = SimpleNamespace(record_trade=lambda _outcome: None)
    forge_calls = []
    oracle = SimpleNamespace(
        act=lambda *_args, **_kwargs: None,
        sentinel=None,
        chronicle=None,
        forge=SimpleNamespace(act=lambda task, context: forge_calls.append(
            (task, context)) or {"status": "queued"}),
    )
    pos = SimpleNamespace(
        symbol="EURUSD", direction=SimpleNamespace(value="buy"),
        entry_price=1.1000, initial_stop=1.0950, initial_target=1.1100,
        entry_time=time.time() - 1800, entry_streams={
            "technical": {"direction": 0.6, "confidence": 0.8,
                          "source": "evolved_champion"}},
        entry_term_evidence={}, entry_regime="trending_up", entry_confidence=0.72,
        entry_atr=0.002, journal_id=None, strategy_id="genome-1",
        timeframe_policy_id="policy-1", execution_environment="demo",
    )
    monkeypatch.setattr(trade_learning, "_append_experiment_log", lambda *args, **kwargs: None)

    engine.record_close(
        oracle, pos, 1.0950, 0.4, "trending_down", "stop_loss",
        close_event_id="demo-close-context-1")

    task, request = forge_calls[0]
    context = request["outcome"]["trade_context"]
    assert task == "oracle.strategy.demo_outcome"
    assert request["outcome"]["pnl_basis"].startswith("price_return")
    assert context["stop_distance_atr"] == 2.5
    assert context["target_distance_atr"] == 5.0
    assert context["target_to_stop_ratio"] == 2.0
    assert context["technical_vote"] == 0.6
    assert context["technical_confidence"] == 0.8
