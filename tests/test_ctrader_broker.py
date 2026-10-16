from types import SimpleNamespace

from Oracle.execution.ctrader_broker import (
    _execution_error_reason,
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


def test_order_volume_below_broker_minimum_is_not_upsized():
    constraints = {
        "minVolume": 100_000,
        "maxVolume": 1_000_000_000,
        "stepVolume": 100_000,
    }

    assert _volume_cents_within_constraints(1_905, constraints) == 0
    assert _volume_cents_within_constraints(250_000, constraints) == 200_000
    assert _volume_cents_within_constraints(50_000_000, constraints) == 50_000_000
