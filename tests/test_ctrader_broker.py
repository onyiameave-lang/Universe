from types import SimpleNamespace
import threading

from Oracle.execution.ctrader_broker import (
    _execution_error_reason,
    _decode_relative_ticks,
    _normalize_order_direction,
    _position_amendment_error,
    _round_price_to_digits,
    _volume_cents_within_constraints,
)


def test_order_direction_must_be_known_before_execution():
    assert _normalize_order_direction(" BUY ") == "buy"
    assert _normalize_order_direction("short") == "short"
    assert _normalize_order_direction("") is None
    assert _normalize_order_direction(None) is None


def test_execution_error_reason_includes_server_description():
    payload = SimpleNamespace(
        errorCode="INVALID_REQUEST",
        description="Stop loss is outside allowed range",
    )

    assert _execution_error_reason(payload) == (
        "INVALID_REQUEST: Stop loss is outside allowed range"
    )


def test_position_amendment_requires_confirmed_position_and_requested_stops():
    confirmed = SimpleNamespace(
        position=SimpleNamespace(positionId=42, stopLoss=1.13429, takeProfit=1.14000),
    )

    assert _position_amendment_error(confirmed, 42, 1.13429, None) is None
    assert _position_amendment_error(confirmed, 42, None, 1.14000) is None


def test_position_amendment_rejects_execution_errors_and_unconfirmed_levels():
    rejected = SimpleNamespace(
        executionType=7,
        errorCode="TRADING_BAD_STOPS",
        description="Invalid stop loss",
    )
    stale = SimpleNamespace(
        position=SimpleNamespace(positionId=42, stopLoss=1.146, takeProfit=1.14000),
    )
    wrong_position = SimpleNamespace(
        position=SimpleNamespace(positionId=43, stopLoss=1.13429, takeProfit=1.14000),
    )
    missing_position = SimpleNamespace()

    assert "TRADING_BAD_STOPS" in _position_amendment_error(
        rejected, 42, 1.13429, None)
    assert "received 1.146" in _position_amendment_error(
        stale, 42, 1.13429, None)
    assert "did not confirm the requested position" in _position_amendment_error(
        wrong_position, 42, 1.13429, None)
    assert "did not confirm the requested position" in _position_amendment_error(
        missing_position, 42, 1.13429, None)


def test_usdjpy_price_is_rounded_to_broker_digits():
    assert _round_price_to_digits(153.2705, 3) == 153.271


def test_historical_tick_price_and_time_deltas_are_reconstructed_before_sorting():
    # cTrader returns newest first: element zero is absolute, later entries
    # are signed deltas from the previous item for both time and price.
    encoded = [
        SimpleNamespace(timestamp=2_000, tick=112_335),
        SimpleNamespace(timestamp=-1_000, tick=1),
        SimpleNamespace(timestamp=-500, tick=-2),
    ]

    assert _decode_relative_ticks(encoded, 0, 2_000, 5) == [
        {"timestamp_ms": 500, "price": 1.12334, "raw_tick": 112334},
        {"timestamp_ms": 1_000, "price": 1.12336, "raw_tick": 112336},
        {"timestamp_ms": 2_000, "price": 1.12335, "raw_tick": 112335},
    ]


def test_order_volume_below_broker_minimum_is_not_upsized():
    constraints = {
        "minVolume": 100_000,
        "maxVolume": 1_000_000_000,
        "stepVolume": 100_000,
    }

    assert _volume_cents_within_constraints(1_905, constraints) == 0
    assert _volume_cents_within_constraints(250_000, constraints) == 200_000
    assert _volume_cents_within_constraints(50_000_000, constraints) == 50_000_000


def test_late_response_for_timed_out_request_is_not_delivered_to_a_caller():
    from Oracle.execution.ctrader_broker import CTraderBroker

    broker = CTraderBroker.__new__(CTraderBroker)
    broker._pending = {}
    broker._pending_lock = threading.Lock()
    broker._Protobuf = SimpleNamespace(extract=lambda message: message.payload)
    seen_payloads = []
    broker._update_cache_from_payload = seen_payloads.append

    broker._dispatch_message(SimpleNamespace(
        clientMsgId="already-timed-out", payload={"status": "filled"}))

    assert seen_payloads == [{"status": "filled"}]


def test_send_and_wait_removes_a_request_after_timeout(monkeypatch):
    import Oracle.execution.ctrader_broker as broker_module

    class ImmediateTimeoutEvent:
        def wait(self, timeout=None):
            return False

    class Deferred:
        def addErrback(self, callback):
            return self

    broker = broker_module.CTraderBroker.__new__(broker_module.CTraderBroker)
    broker._pending = {}
    broker._pending_lock = threading.Lock()
    broker._client = SimpleNamespace(send=lambda request, **kwargs: Deferred())
    broker._reactor = SimpleNamespace(callFromThread=lambda callback: callback())
    monkeypatch.setattr(broker_module, "threading", SimpleNamespace(Event=ImmediateTimeoutEvent))

    assert broker._send_and_wait(object(), timeout=0.01) is None
    assert broker._pending == {}


def test_historical_bar_requests_use_shared_history_throttle(monkeypatch):
    import sys
    from types import ModuleType
    from Oracle.execution.ctrader_broker import CTraderBroker

    api_package = ModuleType("ctrader_open_api")
    api_package.__path__ = []
    messages_package = ModuleType("ctrader_open_api.messages")
    messages_package.__path__ = []
    open_api_messages = ModuleType("ctrader_open_api.messages.OpenApiMessages_pb2")
    model_messages = ModuleType("ctrader_open_api.messages.OpenApiModelMessages_pb2")

    class Request:
        pass

    class Period:
        @staticmethod
        def Value(name):
            return name

    open_api_messages.ProtoOAGetTrendbarsReq = Request
    model_messages.ProtoOATrendbarPeriod = Period
    monkeypatch.setitem(sys.modules, "ctrader_open_api", api_package)
    monkeypatch.setitem(sys.modules, "ctrader_open_api.messages", messages_package)
    monkeypatch.setitem(sys.modules, "ctrader_open_api.messages.OpenApiMessages_pb2", open_api_messages)
    monkeypatch.setitem(sys.modules, "ctrader_open_api.messages.OpenApiModelMessages_pb2", model_messages)

    broker = CTraderBroker.__new__(CTraderBroker)
    broker.status = SimpleNamespace(connected=True)
    broker._client = object()
    broker._symbol_id_by_name = {"EURUSD": 7}
    broker._account_id = 42
    throttled = []
    broker._throttle_historical_request = lambda: throttled.append(True)
    broker._get_symbol_constraints = lambda *_args: {"digits": 5}
    broker._send_and_wait = lambda *_args, **_kwargs: SimpleNamespace(
        errorCode=None,
        trendbar=[SimpleNamespace(
            utcTimestampInMinutes=29_000_000, low=112_000,
            deltaOpen=5, deltaHigh=20, deltaClose=10, volume=12,
        )],
    )

    result = broker.historical_bars("EURUSD", 1, 2, period="M15")

    assert throttled == [True]
    assert result["status"] == "complete"


def test_trendbar_request_timeouts_are_eligible_for_bounded_bisection():
    from Forge.core.oracle_research_data import _history_error_can_be_bisected

    assert _history_error_can_be_bisected("cTrader historical-bar request timed out or failed")
