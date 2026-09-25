from datetime import date, datetime, timezone

from Oracle.execution.market_clock import MarketClock


def _utc(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_broker_interval_controls_open_and_weekend_preopen():
    schedule = [{"startSecond": 22 * 3600, "endSecond": 5 * 86400 + 22 * 3600}]

    open_state = MarketClock.evaluate(
        "EURUSD", schedule, "Etc/UTC", now=_utc("2026-09-23T12:00:00Z"))
    preopen_state = MarketClock.evaluate(
        "EURUSD", schedule, "Etc/UTC", now=_utc("2026-09-20T21:45:00Z"))
    closed_state = MarketClock.evaluate(
        "EURUSD", schedule, "Etc/UTC", now=_utc("2026-09-19T12:00:00Z"))

    assert open_state["state"] == "OPEN"
    assert open_state["can_open"] is True
    assert preopen_state["state"] == "PRE_OPEN"
    assert preopen_state["can_open"] is False
    assert closed_state["state"] == "CLOSED"
    assert closed_state["can_open"] is False


def test_daily_break_and_holiday_close_symbol():
    schedule = [
        {"startSecond": 22 * 3600, "endSecond": 24 * 3600 + 10 * 3600},
        {"startSecond": 24 * 3600 + 11 * 3600, "endSecond": 5 * 86400 + 22 * 3600},
    ]
    break_state = MarketClock.evaluate(
        "EURUSD", schedule, "Etc/UTC", now=_utc("2026-09-21T10:00:00Z"))
    holiday_day = (date(2026, 9, 21) - date(1970, 1, 1)).days
    holiday_state = MarketClock.evaluate(
        "EURUSD", schedule, "Etc/UTC",
        holidays=[{"holidayDate": holiday_day, "isRecurring": False}],
        now=_utc("2026-09-21T12:00:00Z"))

    assert break_state["state"] == "DAILY_BREAK"
    assert holiday_state["state"] == "HOLIDAY"
    assert holiday_state["can_open"] is False


def test_fx_session_overlap_uses_dst_aware_zoneinfo():
    state = MarketClock.evaluate(
        "EURUSD", [{"startSecond": 0, "endSecond": 7 * 86400}],
        "Etc/UTC", now=_utc("2026-07-01T13:00:00Z"))

    assert "London" in state["sessions"]
    assert "New York" in state["sessions"]
    assert "London+New York" in state["sessions"]


def test_missing_broker_schedule_fails_closed_for_new_entries():
    state = MarketClock.evaluate("EURUSD", [], "Etc/UTC", now=_utc("2026-09-23T12:00:00Z"))

    assert state["state"] == "UNKNOWN"
    assert state["can_open"] is False