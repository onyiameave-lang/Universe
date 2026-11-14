from types import SimpleNamespace

from Oracle.execution.mt5_broker import BrokerStatus, MT5Broker


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, trade_mode):
        self.trade_mode = trade_mode

    def account_info(self):
        return SimpleNamespace(
            trade_mode=self.trade_mode,
            login=12345,
            server="Demo-Server",
            balance=100000.0,
            equity=100001.0,
            currency="USD",
        )


def _broker(trade_mode):
    broker = MT5Broker.__new__(MT5Broker)
    broker._mt5 = FakeMT5(trade_mode)
    broker.status = BrokerStatus(connected=True, account_type="unknown")
    return broker


def test_account_refresh_updates_demo_account_classification():
    broker = _broker(trade_mode=0)

    status = broker.account()

    assert status["connected"] is True
    assert status["account_type"] == "demo"
    assert status["trade_mode"] == 0
    assert status["server"] == "Demo-Server"


def test_account_refresh_preserves_unknown_mode_and_does_not_assume_demo():
    broker = _broker(trade_mode=7)

    status = broker.account()

    assert status["account_type"] == "unknown"
    assert status["trade_mode"] == 7
    assert status["account_info_available"] is True


def test_mt5_broker_historical_bars_and_live_quote():
    rates = [
        {
            "time": 1767225600 + idx * 86400,
            "open": 1.1000 + idx * 0.0005,
            "high": 1.1030 + idx * 0.0005,
            "low": 1.0980 + idx * 0.0005,
            "close": 1.1010 + idx * 0.0005,
            "tick_volume": 1200 + idx,
            "real_volume": 0,
        }
        for idx in range(35)
    ]

    class FakeMT5Bars(FakeMT5):
        TIMEFRAME_D1 = 16408

        def symbol_select(self, symbol, enable):
            return True

        def symbol_info(self, symbol):
            return SimpleNamespace(name=symbol, digits=5, point=0.00001, trade_mode=4)

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=1.12123, ask=1.12135, time_msc=1770000000000)

        def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
            return rates

    broker = MT5Broker.__new__(MT5Broker)
    broker.symbol_map = {}
    broker._mt5 = FakeMT5Bars(trade_mode=0)
    broker.status = BrokerStatus(connected=True, account_type="demo")

    quote = broker.live_quote("EURUSD")
    assert quote["status"] == "complete"
    assert quote["bid"] == 1.12123
    assert quote["ask"] == 1.12135

    hist = broker.historical_bars("EURUSD", period="D1", count=35, include_live_tick=True)
    assert hist["status"] == "complete"
    assert hist["source"] == "mt5_broker_live:EURUSD"
    assert len(hist["bars"]) == 35
    assert hist["bars"][-1]["close"] == 1.12123
