from types import SimpleNamespace

from Oracle.execution.mt5_broker import MT5Broker


def _broker_with_quote(ask, bid):
    sent_requests = []
    broker = MT5Broker.__new__(MT5Broker)
    broker.status = SimpleNamespace(connected=True, account_type="demo")
    broker.paper = False
    broker.allow_live = False
    broker._live_allowed = lambda: None
    broker._filling_mode = lambda symbol: 0
    broker._clamp_volume = lambda symbol, units: 0.1
    broker._mt5 = SimpleNamespace(
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0,
        ORDER_FILLING_FOK=0,
        TRADE_RETCODE_DONE=10009,
        symbol_select=lambda symbol, enabled: True,
        symbol_info_tick=lambda symbol: SimpleNamespace(ask=ask, bid=bid),
        order_send=lambda request: (
            sent_requests.append(request)
            or SimpleNamespace(
                retcode=10009,
                comment="",
                price=request["price"],
                order=123,
            )
        ),
    )
    return broker, sent_requests


def _plan():
    return {
        "approved": True,
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD",
        "direction": "long",
        "size": 1000,
        "entry": 1.1000,
        "stop": 1.0950,
        "target": 1.1100,
        "rebase_stops_to_market": True,
        "max_entry_deviation": 0.0025,
    }


def test_current_quote_preserves_signal_stop_and_target_distances():
    broker, requests = _broker_with_quote(ask=1.1002, bid=1.1000)

    result = broker.place_order(_plan())

    assert result["status"] == "filled"
    assert requests[0]["sl"] == 1.0952
    assert requests[0]["tp"] == 1.1102


def test_order_is_rejected_if_quote_moved_beyond_signal_tolerance():
    broker, requests = _broker_with_quote(ask=1.1030, bid=1.1028)

    result = broker.place_order(_plan())

    assert result["status"] == "rejected"
    assert "tolerance" in result["reason"]
    assert requests == []
