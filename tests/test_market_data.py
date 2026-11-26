import logging
import sys
from types import SimpleNamespace

import pandas as pd

from Oracle.core import market_data
from Oracle.core.market_data import Bar, Series


def test_yfinance_rejects_incomplete_history_with_invalid_bars(monkeypatch, caplog):
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

    with caplog.at_level(logging.ERROR, logger="oracle.market_data"):
        series = market_data.YFinanceSource().fetch("EURUSD")

    assert series is None
    assert "rejecting incomplete yfinance history" in caplog.text
    assert "1/2 invalid bars" in caplog.text


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


def test_execution_data_request_rejects_stale_in_memory_disk_fallback(tmp_path, monkeypatch):
    series = Series(
        symbol="GBPUSD",
        bars=[Bar("2026-11-20", 1.30, 1.31, 1.29, 1.305, 0.0)],
        source="cache:yfinance",
    )
    monkeypatch.setitem(
        market_data._market_data_mem_cache,
        "GBPUSD|6mo|1d",
        {
            "result": {
                "status": "complete",
                "series": series,
                "source": "cache:yfinance",
                "bars": 1,
                "cache_age_hours": 66.9,
            },
            "ts": market_data.time.time(),
        },
    )
    data = market_data.MarketData(cache_dir=tmp_path)
    data.sources = []

    result = data.get("GBPUSD", allow_cache_fallback=False)

    assert result["status"] == "error"
    assert result["series"] is None
