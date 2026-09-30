from types import SimpleNamespace

from Oracle.execution.ctrader_broker import (
    _execution_error_reason,
    _normalize_order_direction,
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


def test_usdjpy_price_is_rounded_to_broker_digits():
    assert _round_price_to_digits(153.2705, 3) == 153.271


def test_order_volume_below_broker_minimum_is_not_upsized():
    constraints = {
        "minVolume": 100_000,
        "maxVolume": 1_000_000_000,
        "stepVolume": 100_000,
    }

    assert _volume_cents_within_constraints(1_905, constraints) == 0
    assert _volume_cents_within_constraints(250_000, constraints) == 200_000
    assert _volume_cents_within_constraints(50_000_000, constraints) == 50_000_000
