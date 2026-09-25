"""Per-symbol market state from broker schedules plus DST-aware FX sessions."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SECONDS_PER_DAY = 24 * 60 * 60
_SECONDS_PER_WEEK = 7 * _SECONDS_PER_DAY
_EPOCH_DATE = date(1970, 1, 1)
_FX_CODES = {
    "AUD", "CAD", "CHF", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF",
    "JPY", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "TRY", "USD", "ZAR",
}

_FX_SESSIONS = (
    ("Sydney", "Australia/Sydney", 8, 17),
    ("Tokyo", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
)


def _value(item: Any, key: str, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


class MarketClock:
    """Evaluates cTrader's weekly intervals and holidays for a single symbol."""

    @staticmethod
    def is_forex_pair(symbol: str) -> bool:
        normalized = symbol.upper().replace("/", "")
        return (len(normalized) == 6 and normalized[:3] in _FX_CODES
                and normalized[3:] in _FX_CODES)

    @staticmethod
    def active_fx_sessions(now: Optional[datetime] = None) -> list:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        active = []
        for name, zone_name, open_hour, close_hour in _FX_SESSIONS:
            local = now.astimezone(ZoneInfo(zone_name))
            if local.weekday() < 5 and open_hour <= local.hour < close_hour:
                active.append(name)
        if "London" in active and "New York" in active:
            active.append("London+New York")
        if "Tokyo" in active and "London" in active:
            active.append("Tokyo+London")
        if "Sydney" in active and "Tokyo" in active:
            active.append("Sydney+Tokyo")
        return active

    @staticmethod
    def _holiday_active(holiday: Any, now_utc: datetime, default_zone: ZoneInfo) -> bool:
        holiday_zone_name = _value(holiday, "scheduleTimeZone") or str(default_zone)
        try:
            holiday_zone = ZoneInfo(holiday_zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return False
        local = now_utc.astimezone(holiday_zone)
        raw_day = _value(holiday, "holidayDate")
        if raw_day is None:
            return False
        holiday_day = _EPOCH_DATE + timedelta(days=int(raw_day))
        recurring = bool(_value(holiday, "isRecurring", False))
        if (local.date().month, local.date().day) != (holiday_day.month, holiday_day.day):
            if not recurring or local.date() != holiday_day:
                return False

        start = _value(holiday, "startSecond")
        end = _value(holiday, "endSecond")
        if start is None and end is None:
            return True
        start = int(start or 0)
        end = int(end or _SECONDS_PER_DAY)
        second = local.hour * 3600 + local.minute * 60 + local.second
        if start == end:
            return True
        if start < end:
            return start <= second < end
        return second >= start or second < end

    @staticmethod
    def _next_weekly_boundary(now_utc: datetime, zone: ZoneInfo,
                              schedule: Iterable[Any], opening: bool) -> Optional[datetime]:
        local = now_utc.astimezone(zone)
        days_since_sunday = (local.weekday() + 1) % 7
        sunday = local.date() - timedelta(days=days_since_sunday)
        candidates = []
        for week in (0, 1):
            week_date = sunday + timedelta(days=7 * week)
            week_start = datetime.combine(week_date, time.min, tzinfo=zone)
            for interval in schedule:
                second = int(_value(interval, "startSecond" if opening else "endSecond", 0))
                candidate = (week_start + timedelta(seconds=second)).astimezone(timezone.utc)
                if candidate > now_utc:
                    candidates.append(candidate)
        return min(candidates) if candidates else None

    @classmethod
    def evaluate(cls, symbol: str, schedule: Iterable[Any], schedule_timezone: str,
                 trading_mode: int = 0, holidays: Iterable[Any] = (),
                 now: Optional[datetime] = None, pre_open_minutes: int = 30) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        try:
            zone = ZoneInfo(schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return cls._unknown(symbol, "broker schedule timezone is missing or invalid", now)

        intervals = list(schedule or [])
        if not intervals:
            return cls._unknown(symbol, "broker returned no trading intervals", now)

        local = now.astimezone(zone)
        sunday = local.date() - timedelta(days=(local.weekday() + 1) % 7)
        week_second = ((local.date() - sunday).days * _SECONDS_PER_DAY
                       + local.hour * 3600 + local.minute * 60 + local.second)
        scheduled_open = any(
            int(_value(interval, "startSecond", 0)) <= week_second
            < int(_value(interval, "endSecond", 0))
            for interval in intervals
        )

        is_holiday = any(cls._holiday_active(item, now, zone) for item in holidays or [])
        mode = int(trading_mode or 0)
        next_open = cls._next_weekly_boundary(now, zone, intervals, opening=True)
        next_close = cls._next_weekly_boundary(now, zone, intervals, opening=False)
        sessions = cls.active_fx_sessions(now) if cls.is_forex_pair(symbol) else []

        if mode in (1, 2):
            state, reason, can_open = "CLOSED", "broker trading mode is disabled", False
        elif is_holiday:
            state, reason, can_open = "HOLIDAY", "broker schedule holiday", False
        elif mode == 3:
            state, reason, can_open = "CLOSE_ONLY", "broker allows closing only", False
        elif scheduled_open:
            state, reason, can_open = "OPEN", "inside broker trading interval", True
        else:
            state = "CLOSED" if local.weekday() >= 5 else "DAILY_BREAK"
            reason = "outside broker trading intervals"
            can_open = False

        if (state in ("CLOSED", "DAILY_BREAK") and next_open is not None
                and next_open - now <= timedelta(minutes=pre_open_minutes)):
            state, reason = "PRE_OPEN", "broker trading interval opens soon"

        overlap = next((name for name in sessions if "+" in name), None)
        next_open_value = next_open.isoformat() if next_open else None
        next_close_value = next_close.isoformat() if next_close else None

        return {
            "symbol": symbol,
            "state": state,
            "is_open": scheduled_open and not is_holiday,
            "is_weekend": local.weekday() >= 5,
            "is_daily_break": state == "DAILY_BREAK",
            "can_open": can_open,
            "is_holiday": is_holiday,
            "sessions": sessions,
            "session": overlap or (sessions[0] if sessions else None),
            "overlap": overlap,
            "next_open": next_open_value,
            "next_close": next_close_value,
            "next_open_utc": next_open_value,
            "next_close_utc": next_close_value,
            "schedule_timezone": schedule_timezone,
            "reason": reason,
            "updated_at_utc": now.isoformat(),
        }

    @staticmethod
    def _unknown(symbol: str, reason: str, now: datetime) -> Dict[str, Any]:
        return {
            "symbol": symbol, "state": "UNKNOWN", "is_open": False,
            "is_weekend": now.weekday() >= 5, "is_daily_break": False,
            "can_open": False, "is_holiday": False, "sessions": [],
            "session": None, "overlap": None,
            "next_open": None, "next_close": None,
            "next_open_utc": None, "next_close_utc": None,
            "schedule_timezone": None, "reason": reason,
            "updated_at_utc": now.isoformat(),
        }