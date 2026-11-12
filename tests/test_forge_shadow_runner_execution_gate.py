from types import SimpleNamespace

from Forge.core.shadow_runner import MT5ShadowExecutionBridge


class FakeBroker:
    def __init__(self, account_type="demo"):
        self.status = SimpleNamespace(connected=True, account_type=account_type)
        self.paper = False
        self.orders = []

    def place_order(self, plan, human_confirm=False):
        self.orders.append(plan)
        return {"status": "filled", "order": 123}


def _bridge(tmp_path, account_type="demo"):
    bridge = MT5ShadowExecutionBridge.__new__(MT5ShadowExecutionBridge)
    bridge.execute_mt5_demo_orders = True
    bridge.broker = FakeBroker(account_type)
    bridge.mt5_logger = None
    bridge.sym_mapper = None
    bridge.order_ledger_path = tmp_path / "signal_orders.sqlite3"
    return bridge


def _execute(bridge, *, current_signal, latest_signal, signal_id="GBPUSD:123"):
    return bridge.execute_or_log_mt5_demo_trade(
        cycle=1,
        symbol="GBPUSD",
        direction="LONG",
        confidence=0.68,
        regime="TRENDING_BULLISH",
        sizing_info={"mt5_broker_lots": 0.01, "position_units": 1000},
        entry_price=1.25,
        stop_price=1.24,
        target_price=1.27,
        reward_risk=2.0,
        shadow_status="TAKE_PROFIT",
        realized_r=1.0,
        penalty_audit={},
        current_signal=current_signal,
        latest_signal=latest_signal,
        signal_id=signal_id,
    )


def test_historical_replay_does_not_submit_demo_order(tmp_path):
    bridge = _bridge(tmp_path)

    result = _execute(bridge, current_signal=False, latest_signal=False)

    assert result["execution_status"] == "historical_replay_not_executed"
    assert bridge.broker.orders == []


def test_stale_latest_signal_does_not_submit_demo_order(tmp_path):
    bridge = _bridge(tmp_path)

    result = _execute(bridge, current_signal=False, latest_signal=True)

    assert result["execution_status"] == "stale_signal_not_executed"
    assert bridge.broker.orders == []


def test_current_demo_signal_is_submitted_at_most_once(tmp_path):
    bridge = _bridge(tmp_path)

    first = _execute(bridge, current_signal=True, latest_signal=True)
    second = _execute(bridge, current_signal=True, latest_signal=True)

    assert first["execution_status"] == "filled"
    assert second["execution_status"] == "duplicate_signal_skipped"
    assert len(bridge.broker.orders) == 1


def test_current_signal_is_blocked_on_non_demo_account(tmp_path):
    bridge = _bridge(tmp_path, account_type="real")

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "blocked_non_demo_account"
    assert bridge.broker.orders == []
