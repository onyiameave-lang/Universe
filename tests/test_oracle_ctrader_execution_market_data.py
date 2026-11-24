from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

from Oracle.agents import oracle_agent as oracle_module
from Oracle.agents.oracle_agent import OracleAgent


def _agent_with_broker(history):
    quote = {
        "status": "complete",
        "quote": {
            "bid": 1.1099,
            "ask": 1.1101,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    agent = OracleAgent.__new__(OracleAgent)
    agent._forge_ctrader_broker = SimpleNamespace(
        status=SimpleNamespace(connected=True),
        historical_bars=lambda **_kwargs: history,
        execution_cost_observation=lambda *_args, **_kwargs: quote,
    )
    agent._forge_ctrader_history_lock = threading.Lock()
    agent._ctrader_signal_cache = {}
    agent.data = SimpleNamespace(
        _series_validation_error=oracle_module.MarketData._series_validation_error,
    )
    return agent


def _valid_history(count=80):
    now = datetime.now(timezone.utc)
    bars = []
    for offset in range(count - 1, -1, -1):
        stamp = (now - timedelta(minutes=15 * (offset + 1))).isoformat()
        bars.append({
            "timestamp": stamp,
            "open": 1.10,
            "high": 1.12,
            "low": 1.08,
            "close": 1.11,
            "volume": 100.0,
        })
    return {"status": "complete", "bars": bars}


def test_ctrader_execution_series_accepts_valid_fresh_bars():
    agent = _agent_with_broker(_valid_history())

    result = agent._get_ctrader_execution_series("EURUSD")

    assert result["status"] == "complete", result
    assert result["source"] == "ctrader_open_api_m15+live_quote"
    assert result["bars"] == 80
    assert result["price_basis"] == "bid_bars_live_mid_quote"
    assert result["last"] == 1.11
    assert result["live_bid"] == 1.1099
    assert result["live_ask"] == 1.1101


def test_ctrader_execution_series_rejects_invalid_ohlc():
    history = _valid_history()
    history["bars"][-1]["high"] = 1.09
    agent = _agent_with_broker(history)

    result = agent._get_ctrader_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "invalid cTrader execution bars" in result["message"]


def test_ctrader_execution_series_blocks_when_shared_session_is_disconnected():
    agent = _agent_with_broker(_valid_history())
    agent._forge_ctrader_broker.status.connected = False

    result = agent._get_ctrader_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "shared broker is disconnected" in result["message"]


def test_ctrader_execution_series_rejects_stale_intraday_bars():
    history = _valid_history()
    history["bars"][-1]["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    agent = _agent_with_broker(history)

    result = agent._get_ctrader_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "latest cTrader M15 bar is stale" in result["message"]


def test_ctrader_execution_series_rejects_stale_live_quote():
    agent = _agent_with_broker(_valid_history())
    agent._forge_ctrader_broker.execution_cost_observation = lambda *_args, **_kwargs: {
        "status": "complete",
        "quote": {
            "bid": 1.1099,
            "ask": 1.1101,
            "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
    }

    result = agent._get_ctrader_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "quote is stale or future-dated" in result["message"]


def test_champion_stream_uses_the_already_validated_market_series():
    agent = OracleAgent.__new__(OracleAgent)
    market_series = object()
    seen = []

    class Champion:
        genome_id = "test-champion"

        def vote(self, series):
            seen.append(series)
            return 0.5

    def unexpected_market_data_fetch(*_args, **_kwargs):
        raise AssertionError("champion stream must not fetch a second market-data source")

    agent.data = SimpleNamespace(get=unexpected_market_data_fetch)
    agent._lightweight_champion_lookup = lambda *_args: (Champion(), "test")
    agent._signal_fusion = SimpleNamespace(
        _news_signal=lambda _symbol: {},
        _social_signal=lambda _symbol: {},
        _memory_signal=lambda *_args: {},
    )

    streams = agent._streams("EURUSD", {"regime": {"regime": "ranging"}}, series=market_series)

    assert seen == [market_series]
    assert streams["technical"]["source"] == "evolved_champion"
