from datetime import datetime, timedelta, timezone

from Forge.core.oracle_research_data import _aggregate_m15_bars_to_m45


def _bar(opened, open_, high, low, close, volume):
    return {
        "timestamp": opened.isoformat(),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_m45_aggregates_three_aligned_m15_bars():
    start = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
    source = [
        _bar(start, 1.1, 1.3, 1.0, 1.2, 10),
        _bar(start + timedelta(minutes=15), 1.2, 1.4, 1.1, 1.25, 20),
        _bar(start + timedelta(minutes=30), 1.25, 1.35, 1.15, 1.3, 30),
    ]

    assert _aggregate_m15_bars_to_m45(source) == [{
        "timestamp": start.isoformat(),
        "open": 1.1,
        "high": 1.4,
        "low": 1.0,
        "close": 1.3,
        "volume": 60.0,
    }]


def test_m45_drops_groups_with_a_missing_m15_bar():
    start = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
    source = [
        _bar(start, 1.1, 1.2, 1.0, 1.15, 10),
        _bar(start + timedelta(minutes=30), 1.2, 1.3, 1.1, 1.25, 20),
    ]

    assert _aggregate_m15_bars_to_m45(source) == []
