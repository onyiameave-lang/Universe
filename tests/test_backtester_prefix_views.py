from Oracle.core.backtester import Backtester, _PrefixView
from Oracle.core.market_data import Bar, Series


def test_marked_decision_callback_receives_zero_copy_history_prefixes():
    bars = [
        Bar(ts=str(index), open=1.0 + index * 0.001, high=1.01 + index * 0.001,
            low=0.99 + index * 0.001, close=1.0 + index * 0.001)
        for index in range(80)
    ]
    seen_lengths = []

    def decide(closes, highs, lows, **_kwargs):
        assert isinstance(closes, _PrefixView)
        assert isinstance(highs, _PrefixView)
        assert isinstance(lows, _PrefixView)
        assert closes[-1] == bars[len(closes) - 1].close
        assert closes[-5:] == [bar.close for bar in bars[len(closes) - 5:len(closes)]]
        seen_lengths.append(len(closes))
        return {"call": "hold"}

    decide.supports_prefix_views = True
    result = Backtester().run(
        Series(symbol="EURUSD", bars=bars), decide, warmup=40,
        execution_mode="next_open", price_basis="bid", strategy_genome=object(),
    )

    assert result["status"] == "complete"
    assert seen_lengths == list(range(41, 80))


def test_prefix_view_supports_negative_indices_and_slices():
    view = _PrefixView([10, 20, 30, 40], 3)

    assert len(view) == 3
    assert view[-1] == 30
    assert view[-2:] == [20, 30]
    assert list(view) == [10, 20, 30]
