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
