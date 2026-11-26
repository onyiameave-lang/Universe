from datetime import datetime, timedelta, timezone
import threading
import time
from types import SimpleNamespace

from Oracle.agents import oracle_agent as oracle_module
from Oracle.agents.oracle_agent import OracleAgent
from Oracle.execution.mt5_broker import BrokerStatus, MT5Broker


def _valid_m15_payload(count=80):
    now = datetime.now(timezone.utc)
    current_open = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    latest_completed_open = current_open - timedelta(minutes=15)
    bars = []
    for index in range(count - 1, -1, -1):
        stamp = latest_completed_open - timedelta(minutes=15 * index)
        bars.append({
            "ts": stamp.isoformat().replace("+00:00", "Z"),
            "open": 1.10,
            "high": 1.12,
            "low": 1.08,
            "close": 1.11,
            "volume": 100.0,
        })
    return {
        "broker_symbol": "EURUSD.a",
        "live_bid": 1.1099,
        "live_ask": 1.1101,
        "quote_observed_at": now.isoformat().replace("+00:00", "Z"),
        "raw_series": {"M15": bars},
    }


def _agent_with_mt5_payload(payload, clock_offset=0):
    broker = SimpleNamespace(
        status=SimpleNamespace(connected=True),
        fetch_execution_rates=lambda _symbol: payload,
    )
    agent = OracleAgent.__new__(OracleAgent)
    agent._mt5_execution_broker = broker
    agent._mt5_signal_cache = {}
    agent._mt5_quote_clock_offset_sec = clock_offset
    agent._mt5_quote_clock_observations = {}
    agent._mt5_quote_clock_lock = threading.Lock()
    agent.data = SimpleNamespace(
        _series_validation_error=oracle_module.MarketData._series_validation_error,
    )
    return agent


def test_mt5_execution_series_uses_fresh_terminal_bars_and_quote():
    agent = _agent_with_mt5_payload(_valid_m15_payload())

    result = agent._get_mt5_execution_series("EURUSD")

    assert result["status"] == "complete", result
    assert result["source"] == "mt5_terminal_m15+live_quote"
    assert result["bars"] == 80
    assert result["broker_symbol"] == "EURUSD.a"
    assert result["last"] == 1.11
    assert result["live_bid"] == 1.1099
    assert result["live_ask"] == 1.1101


def test_mt5_execution_series_rejects_invalid_ohlc():
    payload = _valid_m15_payload()
    payload["raw_series"]["M15"][-1]["high"] = 1.09
    agent = _agent_with_mt5_payload(payload)

    result = agent._get_mt5_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "invalid MT5 execution data" in result["message"]


def test_mt5_execution_series_rejects_stale_bars():
    payload = _valid_m15_payload()
    payload["raw_series"]["M15"] = [
        {**bar, "ts": (
            datetime.fromisoformat(bar["ts"].replace("Z", "+00:00"))
            - timedelta(hours=2)
        ).isoformat()}
        for bar in payload["raw_series"]["M15"]
    ]
    agent = _agent_with_mt5_payload(payload)

    result = agent._get_mt5_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "latest MT5 M15 bar is stale" in result["message"]


def test_mt5_execution_series_rejects_stale_quote():
    payload = _valid_m15_payload()
    payload["quote_observed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    agent = _agent_with_mt5_payload(payload)

    result = agent._get_mt5_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "MT5 quote is stale or future-dated" in result["message"]


def test_mt5_execution_series_fails_closed_if_shared_broker_is_disconnected():
    agent = _agent_with_mt5_payload(_valid_m15_payload())
    agent._mt5_execution_broker.status.connected = False

    result = agent._get_mt5_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "shared broker is disconnected" in result["message"]


def test_mt5_quote_clock_offset_requires_two_symbol_corroboration():
    payload = _valid_m15_payload()
    now_epoch = datetime.now(timezone.utc).timestamp()
    payload["quote_time_unix"] = now_epoch + 7200
    payload["quote_observed_at"] = datetime.fromtimestamp(
        now_epoch + 7200, timezone.utc
    ).isoformat()
    agent = _agent_with_mt5_payload(payload, clock_offset=None)

    first = agent._get_mt5_execution_series("EURGBP")
    second = agent._get_mt5_execution_series("EURJPY")

    assert first["status"] == "error"
    assert "not yet corroborated" in first["message"]
    assert second["status"] == "complete", second
    assert second["quote_time_offset_sec"] == 7200


def test_mt5_quote_clock_correction_does_not_accept_stale_quote():
    payload = _valid_m15_payload()
    payload["quote_time_unix"] = datetime.now(timezone.utc).timestamp() + 7200 - 60
    payload["quote_observed_at"] = datetime.fromtimestamp(
        payload["quote_time_unix"], timezone.utc
    ).isoformat()
    agent = _agent_with_mt5_payload(payload, clock_offset=7200)

    result = agent._get_mt5_execution_series("EURUSD")

    assert result["status"] == "error"
    assert "after 7200s clock correction (60.0s old)" in result["message"]


def test_mt5_runner_signal_routes_to_mt5_source_without_public_data_fallback():
    agent = OracleAgent.__new__(OracleAgent)
    calls = []
    agent.signal = lambda symbol, **kwargs: calls.append((symbol, kwargs)) or {
        "status": "error", "message": "expected test result",
    }

    agent.execute("trade.signal", {"symbol": "EURUSD", "_sender": "demo_trader"})

    assert calls[0][1]["require_mt5"] is True
    assert calls[0][1]["allow_cache_fallback"] is False
    assert calls[0][1]["require_ctrader"] is False


def test_mt5_runner_proposal_routes_to_mt5_source_without_public_data_fallback():
    agent = OracleAgent.__new__(OracleAgent)
    calls = []
    agent.signal = lambda symbol, **kwargs: calls.append((symbol, kwargs)) or {
        "status": "error", "message": "expected test result",
    }

    agent.execute("trade.propose", {"symbol": "EURUSD", "_sender": "demo_trader"})

    assert calls[0][1]["require_mt5"] is True
    assert calls[0][1]["allow_cache_fallback"] is False
    assert calls[0][1]["require_ctrader"] is False


def test_mt5_broker_execution_fetch_returns_terminal_quote_timestamp():
    now = int(time.time())
    current_open = now - (now % 900)
    rates = [SimpleNamespace(
        time=current_open - 900 * offset,
        open=1.10, high=1.12, low=1.08, close=1.11, tick_volume=100,
    ) for offset in range(80, 0, -1)]

    class FakeMT5:
        TIMEFRAME_M15 = 15

        @staticmethod
        def symbol_select(_symbol, _enable):
            return True

        @staticmethod
        def copy_rates_from_pos(_symbol, _timeframe, _start, _count):
            return rates

        @staticmethod
        def symbol_info_tick(_symbol):
            return SimpleNamespace(bid=1.1099, ask=1.1101, time=now, time_msc=now * 1000)

    broker = MT5Broker.__new__(MT5Broker)
    broker._mt5 = FakeMT5()
    broker.status = BrokerStatus(connected=True)
    broker.symbol_map = {"EURUSD": "EURUSD.a"}

    result = broker.fetch_execution_rates("EURUSD")

    assert result is not None
    assert result["broker_symbol"] == "EURUSD.a"
    assert len(result["raw_series"]["M15"]) == 80
    assert result["live_bid"] == 1.1099
    assert result["live_ask"] == 1.1101
    assert result["quote_observed_at"]
