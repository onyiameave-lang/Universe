import logging
import sys
from types import SimpleNamespace

import pandas as pd

from Oracle.core import market_data


def test_yfinance_discards_invalid_bars_and_keeps_valid_history(monkeypatch, caplog):
    history = pd.DataFrame(
        {
            "Open": [1.10, 1.20],
            "High": [1.15, 1.25],
            "Low": [1.05, 1.15],
            "Close": [1.12, 1.22],
            "Volume": [0.0, 0.0],
        },
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )
    history.loc[history.index[0], "High"] = 1.11
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(Ticker=lambda _symbol: SimpleNamespace(
            history=lambda **_kwargs: history)),
    )

    with caplog.at_level(logging.WARNING, logger="oracle.market_data"):
        series = market_data.YFinanceSource().fetch("EURUSD")

    assert series is not None
    assert [bar.ts for bar in series.bars] == ["2026-01-02"]
    assert "discarding 1/2 invalid yfinance bars" in caplog.text


def test_yfinance_returns_no_series_when_all_bars_are_invalid(monkeypatch):
    history = pd.DataFrame(
        {
            "Open": [1.10],
            "High": [1.11],
            "Low": [1.05],
            "Close": [1.12],
            "Volume": [0.0],
        },
        index=pd.to_datetime(["2026-01-01"]),
    )
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(Ticker=lambda _symbol: SimpleNamespace(
            history=lambda **_kwargs: history)),
    )

    assert market_data.YFinanceSource().fetch("EURUSD") is None


def test_yfinance_history_uses_configured_request_timeout(monkeypatch):
    seen = {}

    def history(**kwargs):
        seen.update(kwargs)
        raise TimeoutError("simulated Yahoo request timeout")

    monkeypatch.setattr(market_data, "_YFINANCE_TIMEOUT_SEC", 4.0)
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(history=history)),
    )

    result = market_data.YFinanceSource().fetch("EURUSD")

    assert result is None
    assert seen["timeout"] == 4.0
    assert seen["period"] == "6mo"
    assert seen["interval"] == "1d"


def test_broker_market_data_source_prioritized_over_yfinance():
    bars = [
        {
            "timestamp": f"2026-01-{idx:02d}",
            "timestamp_ms": 1767225600000 + idx * 86400000,
            "open": 1.1000 + idx * 0.0005,
            "high": 1.1020 + idx * 0.0005,
            "low": 1.0980 + idx * 0.0005,
            "close": 1.1010 + idx * 0.0005,
            "volume": 1000.0 + idx,
        }
        for idx in range(1, 45)
    ]

    class FakeBroker:
        status = SimpleNamespace(connected=True)

        def historical_bars(self, symbol, **kwargs):
            return {
                "status": "complete",
                "symbol": symbol,
                "source": f"mt5_broker_live:{symbol}",
                "bars": bars,
            }

        def live_quote(self, symbol):
            return {
                "status": "complete",
                "symbol": symbol,
                "bid": bars[-1]["close"],
                "ask": bars[-1]["close"] + 0.0001,
            }

    class FakeMapper:
        def translate(self, sym):
            return f"{sym}.pro"

    md = market_data.MarketData()
    md.attach_broker(FakeBroker(), FakeMapper())
    res = md.get("EURUSD")
    assert res["status"] == "complete"
    assert res["source"] == "mt5_broker_live:EURUSD.pro"
    assert len(res["series"].bars) == 44


def test_market_data_rejects_corrupted_latest_bar_and_anchors_fallback_to_broker_quote():
    valid_bars = [
        market_data.Bar(
            ts=f"2026-01-{idx:02d}",
            open=1.10,
            high=1.12,
            low=1.09,
            close=1.11,
            volume=100.0,
        )
        for idx in range(1, 35)
    ]
    corrupted_latest = market_data.Series(
        symbol="EURUSD",
        bars=valid_bars,
        source="yfinance:EURUSD=X",
        discarded_bars=1,
        total_raw_bars=35,
        latest_bar_discarded=True,
    )
    assert market_data.MarketData._series_completeness_error(corrupted_latest) is not None

    high_corruption = market_data.Series(
        symbol="EURUSD",
        bars=valid_bars,
        source="yfinance:EURUSD=X",
        discarded_bars=6,
        total_raw_bars=36,
        latest_bar_discarded=False,
    )
    assert market_data.MarketData._series_completeness_error(high_corruption) is not None

    class QuoteOnlyBroker:
        status = SimpleNamespace(connected=True)

        def historical_bars(self, symbol, **kwargs):
            return {"status": "error", "reason": "no bars", "bars": []}

        def live_quote(self, symbol):
            return {"status": "complete", "symbol": symbol, "bid": 1.1125, "ask": 1.1127}

    md = market_data.MarketData()
    md.attach_broker(QuoteOnlyBroker())
    clean_series = market_data.Series(
        symbol="EURUSD",
        bars=[
            market_data.Bar(
                ts=f"2026-01-{idx:02d}",
                open=1.10,
                high=1.12,
                low=1.09,
                close=1.1100,
                volume=100.0,
            )
            for idx in range(1, 35)
        ],
        source="yfinance:EURUSD=X",
    )
    err = md._verify_and_anchor_with_broker_quote("EURUSD", clean_series)
    assert err is None
    assert abs(clean_series.last - 1.1125) < 1e-6
    assert "broker_quote_anchored" in clean_series.source

    divergent_series = market_data.Series(
        symbol="EURUSD",
        bars=[
            market_data.Bar(
                ts=f"2026-01-{idx:02d}",
                open=1.20,
                high=1.22,
                low=1.19,
                close=1.2100,
                volume=100.0,
            )
            for idx in range(1, 35)
        ],
        source="yfinance:EURUSD=X",
    )
    div_err = md._verify_and_anchor_with_broker_quote("EURUSD", divergent_series)
    assert div_err is not None
    assert "diverges from broker live quote" in div_err
