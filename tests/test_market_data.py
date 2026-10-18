import sys
from types import SimpleNamespace

from Oracle.core import market_data


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
