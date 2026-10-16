from types import SimpleNamespace

from Oracle.execution.mt5_broker import MT5Broker


def test_order_rejection_includes_broker_retcode_and_comment():
    broker = MT5Broker.__new__(MT5Broker)
    broker.status = SimpleNamespace(account_type="demo")
    broker._mt5 = SimpleNamespace(
        TRADE_ACTION_DEAL=1,
        ORDER_TYPE_SELL=2,
        ORDER_TYPE_BUY=0,
        ORDER_TIME_GTC=0,
        TRADE_RETCODE_DONE=10009,
        symbol_select=lambda symbol, enabled: True,
        symbol_info_tick=lambda symbol: SimpleNamespace(ask=1.1, bid=1.0999),
        order_send=lambda request: SimpleNamespace(
            retcode=10016,
            comment="Invalid stops",
        ),
    )
    broker._live_allowed = lambda: None
    broker._filling_mode = lambda symbol: 0
    broker._clamp_volume = lambda symbol, units: 0.02

    result = broker.place_order({
        "approved": True,
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD",
        "direction": "short",
        "size": 2696.3999,
        "stop": 1.12,
        "target": 1.06,
    })

    assert result["status"] == "rejected"
    assert result["retcode"] == 10016
    assert result["comment"] == "Invalid stops"
    assert "retcode=10016" in result["reason"]
    assert "Invalid stops" in result["reason"]
