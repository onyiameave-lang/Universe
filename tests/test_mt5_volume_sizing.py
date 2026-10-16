from types import SimpleNamespace

from Oracle.execution.mt5_broker import MT5Broker


def _broker_with_symbol_info(**values):
    broker = MT5Broker.__new__(MT5Broker)
    broker._mt5 = SimpleNamespace(
        symbol_info=lambda symbol: SimpleNamespace(**values),
    )
    return broker


def test_units_are_converted_to_lots_and_rounded_down():
    broker = _broker_with_symbol_info(
        trade_contract_size=100_000,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )

    assert broker._clamp_volume("EURUSD", 2696.3999) == 0.02


def test_fractional_volume_step_is_preserved():
    broker = _broker_with_symbol_info(
        trade_contract_size=100,
        volume_min=0.25,
        volume_max=100.0,
        volume_step=0.25,
    )

    assert broker._clamp_volume("CFD", 37.5) == 0.25


def test_below_minimum_risk_size_is_rejected_instead_of_rounded_up():
    broker = _broker_with_symbol_info(
        trade_contract_size=100_000,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )

    assert broker._clamp_volume("EURUSD", 500) is None


def test_missing_contract_size_is_rejected():
    broker = _broker_with_symbol_info(
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
    )

    assert broker._clamp_volume("EURUSD", 2696.3999) is None
