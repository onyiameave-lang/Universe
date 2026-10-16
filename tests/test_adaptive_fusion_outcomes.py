import threading
from types import SimpleNamespace

from Oracle.intelligence.adaptive_fusion import AdaptiveFusion, DEFAULT_WEIGHTS


def _fusion(state=None):
    fusion = AdaptiveFusion.__new__(AdaptiveFusion)
    fusion._lock = threading.RLock()
    persisted_state = state if state is not None else {}
    fusion._store = SimpleNamespace(
        data=lambda: persisted_state,
        save=lambda: None,
    )
    return fusion


def test_profitable_short_is_counted_as_win_and_threshold_is_smoothed():
    fusion = _fusion()

    result = fusion.learn_from_outcome(
        "EURUSD", {}, realized_direction=-1, trade_won=True)

    assert result["outcome_trades"] == 1
    assert result["outcome_wins"] == 1
    assert result["entry_threshold"] == 0.125


def test_losing_short_is_not_counted_as_win_when_price_rises():
    fusion = _fusion()

    result = fusion.learn_from_outcome(
        "EURUSD", {}, realized_direction=1, trade_won=False)

    assert result["outcome_trades"] == 1
    assert result["outcome_wins"] == 0
    assert result["entry_threshold"] == 0.175


def test_flat_trade_does_not_penalize_directional_streams():
    fusion = _fusion()
    streams = {"technical": {"direction": 1.0, "confidence": 0.9}}

    fusion.learn_from_outcome(
        "EURUSD", streams, realized_direction=0, trade_won=False)

    state = fusion._sym("EURUSD")
    assert state["stream_hits"]["technical"] == {"correct": 0, "total": 0}


def test_legacy_direction_based_win_counts_are_reset_but_weights_preserved():
    legacy_weights = {"technical": 0.5, "news": 0.2, "social": 0.2, "memory": 0.1}
    fusion = _fusion({
        "EURUSD": {
            "weights": dict(legacy_weights),
            "entry_threshold": 0.08,
            "stream_hits": {},
            "trades": 20,
            "wins": 18,
        }
    })

    state = fusion._sym("EURUSD")

    assert state["entry_threshold"] == 0.15
    assert state["outcome_trades"] == 0
    assert state["outcome_wins"] == 0
    assert state["trades"] == state["wins"] == 0
    assert state["weights"] == legacy_weights
