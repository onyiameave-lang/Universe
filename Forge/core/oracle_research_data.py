"""Forge-owned, read-only cTrader history provider for Oracle research."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("forge.oracle_research_data")

TIMEFRAMES_MS = {
    "W1": 7 * 24 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "M15": 15 * 60 * 1000,
    # The Open API period enum omits M45; derive it after fetching M15 once.
    "M45": 45 * 60 * 1000,
}
_YEAR_MS = 365 * 24 * 60 * 60 * 1000


def _history_error_can_be_bisected(detail: str) -> bool:
    """Return whether a smaller read-only trendbar request may recover."""
    lower = str(detail or "").lower()
    return any(term in lower for term in (
        "range", "too large", "maximum", "max distance", "timestamp constraint",
        "period constraint", "historical request limit", "invalid request",
        "timeout", "timed out",
    ))


def _aggregate_m15_bars_to_m45(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate complete UTC-aligned M15 bars into M45 OHLCV bars.

    Incomplete groups (including gaps and partial first/last groups) are
    dropped so Forge never treats a partial interval as a 45-minute candle.
    """
    interval_ms = 15 * 60 * 1000
    bucket_ms = 45 * 60 * 1000
    groups: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for bar in bars:
        try:
            opened = datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            timestamp_ms = int(opened.timestamp() * 1000)
            if timestamp_ms % interval_ms:
                continue
            bucket_start = timestamp_ms // bucket_ms * bucket_ms
            offset = timestamp_ms - bucket_start
            if offset not in (0, interval_ms, 2 * interval_ms):
                continue
            groups.setdefault(bucket_start, {})[offset] = bar
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    aggregated = []
    for bucket_start, members in sorted(groups.items()):
        if set(members) != {0, interval_ms, 2 * interval_ms}:
            continue
        first, middle, last = (members[offset] for offset in (0, interval_ms, 2 * interval_ms))
        try:
            aggregated.append({
                "timestamp": datetime.fromtimestamp(bucket_start / 1000, tz=timezone.utc).isoformat(),
                "open": float(first["open"]),
                "high": max(float(first["high"]), float(middle["high"]), float(last["high"])),
                "low": min(float(first["low"]), float(middle["low"]), float(last["low"])),
                "close": float(last["close"]),
                "volume": float(first.get("volume", 0.0))
                + float(middle.get("volume", 0.0))
                + float(last.get("volume", 0.0)),
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return aggregated


class OracleResearchDataProvider:
    """Fetch completed, bid-based top-down OHLCV without loading Oracle agent."""

    def __init__(self) -> None:
        self._broker = None
        self._lock = threading.RLock()
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._aliases_path = Path(__file__).resolve().parents[2] / "Oracle" / "config" / "ctrader_research_symbol_aliases.json"

    def _connect(self) -> Dict[str, Any]:
        if self._broker is not None:
            status = getattr(self._broker, "status", None)
            if getattr(status, "connected", False):
                return {"status": "complete", "broker": self._broker}
            # cTrader can close an idle transport between scheduled requests.
            # Reuse the configured broker and its running reactor to establish
            # a fresh authenticated client instead of returning a dead handle.
            try:
                connection = self._broker.connect()
            except Exception as exc:
                return {"status": "blocked",
                        "message": f"Forge cTrader reconnect failed: {type(exc).__name__}"}
            if isinstance(connection, dict) and connection.get("connected"):
                return {"status": "complete", "broker": self._broker}
            reason = connection.get("reason", "cTrader authentication failed") if isinstance(connection, dict) else "cTrader authentication failed"
            return {"status": "blocked", "message": f"Forge cTrader reconnect failed: {reason}"}
        credential_env_path = os.getenv("ORACLE_FORGE_CTRADER_ENV_FILE", "").strip()
        if credential_env_path:
            env_path = Path(credential_env_path).expanduser()
            if not env_path.is_file():
                return {"status": "blocked",
                        "message": "configured Forge cTrader credential env file does not exist"}
            try:
                from dotenv import load_dotenv
            except ImportError:
                return {"status": "blocked",
                        "message": "python-dotenv is required to load the configured Forge cTrader credential env file"}
            # Keep runner/Actions environment variables authoritative; only
            # fill missing cTrader values from this optional local V1 .env.
            load_dotenv(dotenv_path=env_path, override=False)
        try:
            from Oracle.execution.ctrader_broker import CTraderBroker
        except ImportError as exc:
            return {"status": "blocked", "message": f"cTrader broker adapter unavailable: {type(exc).__name__}"}
        use_demo = os.getenv("CTRADER_USE_DEMO", "true").strip().lower() not in {"0", "false", "no"}
        broker = CTraderBroker(use_demo=use_demo)
        connection = broker.connect()
        if not isinstance(connection, dict) or not connection.get("connected"):
            reason = connection.get("reason", "cTrader authentication failed") if isinstance(connection, dict) else "cTrader authentication failed"
            return {"status": "blocked", "message": f"Forge cTrader research connection failed: {reason}"}
        self._broker = broker
        return {"status": "complete", "broker": broker}

    def _symbol_aliases(self) -> Dict[str, str]:
        try:
            document = json.loads(self._aliases_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        aliases = document.get("aliases") if isinstance(document, dict) and document.get("schema") == "oracle.ctrader_research_symbol_aliases.v1" else None
        if not isinstance(aliases, dict):
            return {}
        return {str(key).strip().upper(): str(value).strip().upper()
                for key, value in aliases.items() if str(key).strip() and str(value).strip()}

    @staticmethod
    def _bar_is_stale(last_open: datetime, interval_ms: int, now: datetime,
                      maximum_age: timedelta = timedelta(days=7)) -> bool:
        """Measure freshness from bar close, not its opening timestamp."""
        completed_at = last_open + timedelta(milliseconds=interval_ms)
        return completed_at < now - maximum_age

    def fetch_topdown_bundle(self, symbol: str) -> Dict[str, Any]:
        key = str(symbol).strip().upper()
        if not re.fullmatch(r"[A-Z0-9._-]{1,24}", key):
            return {"status": "blocked", "message": "invalid Oracle research symbol"}
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 20 * 60:
                return cached[1]
            connection = self._connect()
            if connection.get("status") != "complete":
                return connection
            broker = connection["broker"]
            broker_symbol = self._symbol_aliases().get(key, key)
            now = datetime.now(timezone.utc)
            end_ms = int(now.timestamp() * 1000)
            start_ms = int((now - timedelta(days=5 * 365 + 2)).timestamp() * 1000)
            frames: Dict[str, List[Dict[str, Any]]] = {}
            fingerprints: Dict[str, str] = {}
            coverage: Dict[str, Dict[str, Any]] = {}

            for period, interval_ms in TIMEFRAMES_MS.items():
                if period == "M45":
                    # The cTrader CLI advertises M45 but the Open API's
                    # ProtoOATrendbarPeriod enum in this client does not.
                    pieces = _aggregate_m15_bars_to_m45(frames.get("M15", []))
                    requests = 0
                else:
                    # Keep requests under broker response limits; bisect
                    # boundedly on truncation or a reported range rejection.
                    chunk_ms = min(interval_ms * 1800, _YEAR_MS)
                    windows = []
                    cursor = start_ms
                    while cursor < end_ms:
                        boundary = min(end_ms, cursor + chunk_ms)
                        windows.append((cursor, boundary, 0))
                        cursor = boundary
                    pieces: List[Dict[str, Any]] = []
                    requests = 0
                    timeout_failures = 0
                    while windows:
                        left, right, depth = windows.pop()
                        requests += 1
                        if requests > 1000:
                            return {"status": "blocked", "symbol": key,
                                    "message": f"{period} history exceeded Forge's bounded request budget"}
                        history = broker.historical_bars(
                            symbol=broker_symbol, from_timestamp_ms=left,
                            to_timestamp_ms=right, period=period, count=2000, timeout=60.0,
                        )
                        if (isinstance(history, dict)
                                and "account is not connected" in str(history.get("message", "")).lower()):
                            reconnected = self._connect()
                            if reconnected.get("status") == "complete":
                                broker = reconnected["broker"]
                                history = broker.historical_bars(
                                    symbol=broker_symbol, from_timestamp_ms=left,
                                    to_timestamp_ms=right, period=period, count=2000, timeout=60.0,
                                )
                        is_complete = isinstance(history, dict) and history.get("status") == "complete"
                        truncated = is_complete and bool(history.get("has_more"))
                        detail = str(history.get("message", "")) if isinstance(history, dict) else "invalid broker response"
                        lower = detail.lower()
                        if "timeout" in lower or "timed out" in lower:
                            timeout_failures += 1
                            if timeout_failures > 3:
                                return {"status": "blocked", "symbol": key,
                                        "message": f"{period} cTrader history timed out repeatedly; stopped after three retries"}
                        range_limited = _history_error_can_be_bisected(detail)
                        midpoint = (left + right) // 2
                        can_split = depth < 16 and right - left > interval_ms * 2 and left < midpoint < right
                        if can_split and (truncated or (not is_complete and range_limited)):
                            windows.extend(((left, midpoint, depth + 1), (midpoint, right, depth + 1)))
                            continue
                        if not is_complete:
                            return {"status": "blocked", "symbol": key,
                                    "message": f"{period} cTrader history unavailable: {detail or 'request failed'}"}
                        if truncated:
                            return {"status": "blocked", "symbol": key,
                                    "message": f"{period} cTrader history remains truncated at the minimum request window"}
                        pieces.extend(history.get("bars", []))

                by_timestamp = {str(item.get("timestamp")): item for item in pieces if isinstance(item, dict)}
                completed = []
                for stamp in sorted(by_timestamp):
                    item = by_timestamp[stamp]
                    try:
                        opened = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if opened.tzinfo is None:
                            opened = opened.replace(tzinfo=timezone.utc)
                        values = {name: float(item[name]) for name in ("open", "high", "low", "close")}
                        volume = float(item.get("volume", 0.0))
                    except (KeyError, TypeError, ValueError):
                        return {"status": "blocked", "symbol": key, "message": f"{period} has invalid OHLCV data"}
                    if (not all(math.isfinite(x) for x in (*values.values(), volume))
                            or not (values["low"] <= min(values["open"], values["close"])
                                    <= max(values["open"], values["close"]) <= values["high"])
                            or volume < 0):
                        return {"status": "blocked", "symbol": key, "message": f"{period} failed OHLCV bounds validation"}
                    if opened + timedelta(milliseconds=interval_ms) <= now:
                        completed.append({"timestamp": opened.isoformat(), **values, "volume": volume})
                if not completed:
                    return {"status": "blocked", "symbol": key, "message": f"{period} has no completed bars"}
                first = datetime.fromisoformat(completed[0]["timestamp"])
                last = datetime.fromisoformat(completed[-1]["timestamp"])
                if first > now - timedelta(days=5 * 365) + timedelta(milliseconds=interval_ms * 3):
                    return {"status": "blocked", "symbol": key, "message": f"{period} does not cover the five-year history target"}
                # `last` is the bar's opening timestamp. A newly completed W1
                # bar necessarily opens about seven days before its close, so
                # compare its completion time rather than treating that open
                # time as the age of the observation.
                if self._bar_is_stale(last, interval_ms, now):
                    return {"status": "blocked", "symbol": key, "message": f"{period} history is stale"}
                canonical = json.dumps(completed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                frames[period] = completed
                fingerprints[period] = hashlib.sha256(canonical).hexdigest()
                coverage[period] = {
                    "bars": len(completed), "first_completed_bar": completed[0]["timestamp"],
                    "last_completed_bar": completed[-1]["timestamp"], "request_count": requests,
                }
                if period == "M45":
                    coverage[period]["derived_from"] = "M15"
                    coverage[period]["aggregation"] = "three contiguous UTC-aligned M15 candles"

            combined = hashlib.sha256(json.dumps(
                {"symbol": key, "period_fingerprints": fingerprints, "price_basis": "bid",
                 "schema": "oracle.topdown_ohlcv.v1"},
                sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
            result = {
                "status": "complete", "symbol": key, "broker_symbol": broker_symbol,
                "source": "ctrader_open_api_bid", "price_basis": "bid", "history_target": "5y",
                "timeframes": frames, "timeframe_fingerprints": fingerprints,
                "timeframe_coverage": coverage, "data_fingerprint": combined,
                "as_of": now.isoformat(),
            }
            self._cache.clear()
            self._cache[key] = (time.monotonic(), result)
            return result


    def collect_cost_observations(self, symbols: List[str]) -> Dict[str, Any]:
        """Build research-only profiles from cTrader deal, metadata, tick, and execution-quote evidence."""
        import bisect
        import math

        connection = self._connect()
        if connection.get("status") != "complete":
            return {"status": "blocked", "message": connection.get("message"), "observations": []}
        broker = connection["broker"]
        aliases = self._symbol_aliases()
        broker_catalog = set(broker.symbol_catalog())
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        window_hours = max(1, min(24, int(os.getenv("ORACLE_FORGE_COST_WINDOW_HOURS", "6"))))
        window_start_ms = now_ms - window_hours * 60 * 60 * 1000
        deal_history_days = max(7, min(3650, int(os.getenv("ORACLE_FORGE_DEAL_HISTORY_DAYS", "1827"))))
        deals_result = broker.historical_deals(
            now_ms - deal_history_days * 24 * 60 * 60 * 1000,
            now_ms, max_rows=10000, timeout=60.0)
        observations = []
        profiles = {}
        minimum_deals = max(1, int(os.getenv("ORACLE_FORGE_MINIMUM_COST_DEALS", "20")))
        minimum_quotes = max(20, int(os.getenv("ORACLE_FORGE_MINIMUM_COST_QUOTES", "100")))
        deal_status_ok = deals_result.get("status") == "complete"
        if not deal_status_ok:
            log.warning("cTrader cost profile deal sample unavailable: %s",
                        deals_result.get("message", deals_result.get("status")))
        all_deals = deals_result.get("deals", []) if isinstance(deals_result, dict) else []
        account_currency = str(getattr(broker.status, "currency", "")).upper()
        conversion_graph: Dict[str, Dict[str, float]] = {"USD": {"USD": 1.0}}

        def add_conversion(base: str, quote: str, rate: float) -> None:
            base, quote = str(base or "").upper(), str(quote or "").upper()
            if not base or not quote or base == quote or not math.isfinite(rate) or rate <= 0:
                return
            conversion_graph.setdefault(base, {})[quote] = rate
            conversion_graph.setdefault(quote, {})[base] = 1.0 / rate

        def conversion_to_usd(asset: str) -> Optional[float]:
            asset = str(asset or "").upper()
            if asset == "USD":
                return 1.0
            if not asset:
                return None
            pending = [(asset, 1.0)]
            visited = set()
            while pending:
                current, rate = pending.pop(0)
                if current == "USD":
                    return rate
                if current in visited:
                    continue
                visited.add(current)
                for next_asset, edge_rate in conversion_graph.get(current, {}).items():
                    if next_asset not in visited:
                        pending.append((next_asset, rate * edge_rate))
            return None

        for raw_symbol in symbols:
            symbol = str(raw_symbol).strip().upper()
            broker_symbol = aliases.get(symbol, symbol)
            try:
                if broker_symbol not in broker_catalog:
                    observations.append({"status": "blocked", "symbol": symbol,
                                         "broker_symbol": broker_symbol,
                                         "message": f"broker does not list mapped symbol {broker_symbol}"})
                    continue
                metadata = broker.symbol_metadata(broker_symbol)
                symbol_deals = [
                    d for d in all_deals
                    if d.get("symbol") == broker_symbol
                    and d.get("deal_status") in {2, 3}
                    and float(d.get("filled_volume_cents") or 0) > 0
                ]
                fill_eligible = deal_status_ok and len(symbol_deals) >= minimum_deals
                live_quote_result = broker.execution_cost_observation(broker_symbol, timeout=3.0)
                live_quote = live_quote_result.get("quote", {}) if isinstance(live_quote_result, dict) else {}
                live_bid, live_ask = live_quote.get("bid"), live_quote.get("ask")
                live_mid = ((float(live_bid) + float(live_ask)) / 2.0
                            if isinstance(live_bid, (int, float))
                            and isinstance(live_ask, (int, float))
                            and 0 < live_bid <= live_ask else None)
                live_spread_bps = ((float(live_ask) - float(live_bid)) / live_mid * 10000.0
                                   if live_mid else None)
                # Commission metadata and market spread are useful for every
                # mapped symbol. Actual fill/slippage evidence is collected
                # when available and remains a separate promotion gate.
                reference_mid = live_mid
                if fill_eligible or reference_mid is None:
                    reference_start = now_ms - 7 * 24 * 60 * 60 * 1000
                    reference = broker.historical_bars(
                        broker_symbol, reference_start, now_ms, period="D1", count=20, timeout=30.0)
                    reference_bars = reference.get("bars", []) if isinstance(reference, dict) else []
                    reference_bars = [bar for bar in reference_bars if isinstance(bar, dict)]
                    if reference_bars:
                        reference_mid = float(reference_bars[-1]["close"])
                if (not isinstance(reference_mid, (int, float)) or not math.isfinite(reference_mid)
                        or reference_mid <= 0):
                    observations.append({
                        "symbol": symbol, "broker_symbol": broker_symbol,
                        "status": "blocked", "metadata": metadata,
                        "sample": {"filled_deals": len(symbol_deals), "matched_spreads": 0,
                                   "bid_tick_status": "not_requested", "ask_tick_status": "not_requested"},
                        "cost_profile": None,
                        "reasons": ["recent cTrader price reference unavailable for cost normalization"],
                    })
                    continue
                chunk_minutes = max(1, min(60, int(os.getenv("ORACLE_FORGE_TICK_CHUNK_MINUTES", "15"))))
                chunk_ms = chunk_minutes * 60 * 1000
                bid_ticks, ask_ticks = [], []
                bid_statuses, ask_statuses = [], []
                if fill_eligible:
                    cursor = window_start_ms
                    while cursor < now_ms:
                        chunk_end = min(now_ms, cursor + chunk_ms)
                        bid_chunk = broker.historical_ticks(broker_symbol, "BID", cursor, chunk_end, timeout=60.0)
                        ask_chunk = broker.historical_ticks(broker_symbol, "ASK", cursor, chunk_end, timeout=60.0)
                        bid_statuses.append(bid_chunk.get("status"))
                        ask_statuses.append(ask_chunk.get("status"))
                        bid_ticks.extend(bid_chunk.get("ticks", []))
                        ask_ticks.extend(ask_chunk.get("ticks", []))
                        cursor = chunk_end
                bid_ticks = sorted({item["timestamp_ms"]: item for item in bid_ticks}.values(), key=lambda item: item["timestamp_ms"])
                ask_ticks = sorted({item["timestamp_ms"]: item for item in ask_ticks}.values(), key=lambda item: item["timestamp_ms"])
                bid_complete = bool(bid_statuses) and all(value == "complete" for value in bid_statuses)
                ask_complete = bool(ask_statuses) and all(value == "complete" for value in ask_statuses)
                bid_raw_ticks = [int(item["raw_tick"]) for item in bid_ticks if isinstance(item.get("raw_tick"), int)]
                ask_raw_ticks = [int(item["raw_tick"]) for item in ask_ticks if isinstance(item.get("raw_tick"), int)]
                raw_tick_count = len(bid_ticks) + len(ask_ticks)
                bid_ticks = [item for item in bid_ticks
                             if 0.1 <= float(item.get("price", 0)) / reference_mid <= 10.0]
                ask_ticks = [item for item in ask_ticks
                             if 0.1 <= float(item.get("price", 0)) / reference_mid <= 10.0]
                rejected_scale_ticks = raw_tick_count - len(bid_ticks) - len(ask_ticks)
                ask_times = [item["timestamp_ms"] for item in ask_ticks]
                pip_position = metadata.get("pipPosition")
                paired_spreads = []
                paired_mids = []
                quote_tolerance_ms = max(100, int(os.getenv("ORACLE_FORGE_QUOTE_MATCH_TOLERANCE_MS", "1000")))
                for bid_tick in bid_ticks:
                    idx = bisect.bisect_left(ask_times, bid_tick["timestamp_ms"])
                    candidates = ask_ticks[max(0, idx - 1):min(len(ask_ticks), idx + 1)]
                    if not candidates:
                        continue
                    ask_tick = min(candidates, key=lambda item: abs(item["timestamp_ms"] - bid_tick["timestamp_ms"]))
                    if abs(ask_tick["timestamp_ms"] - bid_tick["timestamp_ms"]) > quote_tolerance_ms:
                        continue
                    mid = (float(bid_tick["price"]) + float(ask_tick["price"])) / 2.0
                    if mid > 0 and ask_tick["price"] >= bid_tick["price"]:
                        paired_spreads.append((ask_tick["price"] - bid_tick["price"]) / mid * 10000.0)
                        paired_mids.append(mid)

                base_asset = str(metadata.get("baseAssetName") or "").upper()
                quote_asset = str(metadata.get("quoteAssetName") or "").upper()
                current_mid = sorted(paired_mids)[len(paired_mids) // 2] if paired_mids else live_mid
                if current_mid is not None:
                    add_conversion(base_asset, quote_asset, current_mid)
                commission_price_reference = current_mid or reference_mid

                commission_bps_samples, slippage_bps_samples = [], []
                maximum_slippage_deals = max(
                    minimum_deals,
                    min(500, int(os.getenv("ORACLE_FORGE_MAXIMUM_SLIPPAGE_DEALS", "100"))),
                )
                attempted_slippage_deals = 0
                fill_quote_scale_mismatches = 0
                ordered_deals = sorted(
                    symbol_deals,
                    key=lambda item: int(item.get("execution_timestamp_ms") or 0),
                    reverse=True,
                )
                for deal in ordered_deals:
                    if attempted_slippage_deals >= maximum_slippage_deals:
                        break
                    execution_price = deal.get("execution_price")
                    units = float(deal.get("filled_volume_cents") or 0) / 100.0
                    conversion = deal.get("base_to_usd_conversion_rate")
                    if (isinstance(execution_price, (int, float)) and execution_price > 0
                            and units > 0 and isinstance(conversion, (int, float)) and conversion > 0):
                        notional_usd = units * float(conversion)
                        money_digits = deal.get("money_digits")
                        commission = (abs(float(deal.get("commission_raw") or 0)) / (10 ** int(money_digits))
                                      if isinstance(money_digits, int) and 1 <= money_digits <= 12 else 0.0)
                        if notional_usd > 0 and commission > 0 and account_currency == "USD":
                            commission_bps_samples.append(commission / notional_usd * 10000.0)
                        # Compare the fill against the broker quote at execution
                        # time, not the deal's submission time or the unrelated
                        # recent quote window collected for spread measurement.
                        reference_timestamp = int(
                            deal.get("execution_timestamp_ms")
                            or deal.get("create_timestamp_ms")
                            or 0
                        )
                        trade_side = deal.get("trade_side")
                        if reference_timestamp and trade_side in {1, 2}:
                            attempted_slippage_deals += 1
                            quote_type = "ASK" if trade_side == 1 else "BID"
                            quote_result = broker.historical_ticks(
                                broker_symbol,
                                quote_type,
                                max(0, reference_timestamp - quote_tolerance_ms),
                                reference_timestamp + quote_tolerance_ms + 1,
                                timeout=15.0,
                            )
                            quote_candidates = quote_result.get("ticks", [])
                            if quote_result.get("status") == "complete" and quote_candidates:
                                quote = min(
                                    quote_candidates,
                                    key=lambda item: abs(item["timestamp_ms"] - reference_timestamp),
                                )
                                if (abs(quote["timestamp_ms"] - reference_timestamp)
                                        <= quote_tolerance_ms and quote["price"] > 0):
                                    lower_price = min(float(execution_price), float(quote["price"]))
                                    higher_price = max(float(execution_price), float(quote["price"]))
                                    # A fill and its execution-time quote should be on
                                    # the same price scale. A >10x difference indicates
                                    # malformed or misdecoded market data, not usable
                                    # slippage evidence. Fail closed instead of turning
                                    # it into a gigantic but apparently valid cost.
                                    if lower_price <= 0 or higher_price / lower_price > 10.0:
                                        fill_quote_scale_mismatches += 1
                                        continue
                                    adverse = (
                                        float(execution_price) - quote["price"]
                                        if trade_side == 1
                                        else quote["price"] - float(execution_price)
                                    )
                                    slippage_bps_samples.append(
                                        max(0.0, adverse / quote["price"] * 10000.0)
                                    )
                                    if len(slippage_bps_samples) >= minimum_deals:
                                        break

                profile = None
                reasons = []
                commission_type = metadata.get("commissionType")
                precise_rate = metadata.get("preciseTradingCommissionRate")
                commission_base = None
                commission_source = "cTrader symbol commission schedule"
                quote_to_usd = conversion_to_usd(quote_asset)
                if not isinstance(precise_rate, (int, float)):
                    reasons.append("cTrader precise symbol commission rate is unavailable")
                elif commission_type == 1:  # USD per million USD notional
                    if account_currency != "USD":
                        reasons.append("USD-denominated commission schedule requires a USD account")
                    else:
                        usd_per_million = float(precise_rate) / 1e8
                        commission_base = usd_per_million / 1_000_000.0 * 10_000.0
                elif commission_type == 2:  # USD per lot
                    if account_currency != "USD":
                        reasons.append("USD-denominated commission schedule requires a USD account")
                    elif float(precise_rate) == 0:
                        commission_base = 0.0
                    else:
                        lot_size = metadata.get("lotSize")
                        if (commission_price_reference is None or quote_to_usd is None
                                or not isinstance(lot_size, (int, float)) or lot_size <= 0):
                            reasons.append("cannot convert cTrader per-lot commission into USD notional bps")
                        else:
                            usd_per_lot = float(precise_rate) / 1e8
                            usd_notional_per_lot = (float(lot_size) / 100.0) * commission_price_reference * quote_to_usd
                            if usd_notional_per_lot <= 0:
                                reasons.append("cTrader symbol metadata produced an invalid USD notional per lot")
                            else:
                                commission_base = usd_per_lot / usd_notional_per_lot * 10_000.0
                elif commission_type == 3:  # percentage of trading value
                    commission_base = (float(precise_rate) / 1e5) * 100.0
                else:
                    reasons.append(f"unsupported cTrader commission type: {commission_type}")
                if (commission_base is not None
                        and (not math.isfinite(commission_base)
                             or commission_base < 0 or commission_base > 10000)):
                    reasons.append("derived commission cost is outside supported [0, 10000] bps bounds")
                if fill_quote_scale_mismatches:
                    reasons.append(
                        f"{fill_quote_scale_mismatches} fill quotes differ from execution prices by more than 10x; "
                        "market price scale is invalid"
                    )
                if not bid_complete or not ask_complete:
                    reasons.append("complete, non-truncated bid and ask tick windows are required")
                if not deal_status_ok:
                    reasons.append("complete cTrader deal history is required")
                if len(symbol_deals) < minimum_deals:
                    reasons.append(
                        f"only {len(symbol_deals)} actual filled deals; {minimum_deals} required for measured slippage"
                    )
                if len(paired_spreads) < minimum_quotes:
                    reasons.append(f"only {len(paired_spreads)} matched bid/ask observations; {minimum_quotes} required")
                minimum_commission = metadata.get("preciseMinCommission")
                if isinstance(minimum_commission, (int, float)) and minimum_commission > 0:
                    reasons.append("symbol minimum commission requires per-trade modeling, which the current bps backtester does not support")
                if len(slippage_bps_samples) < minimum_deals:
                    reasons.append(
                        f"only {len(slippage_bps_samples)} matched fill/slippage observations; {minimum_deals} required"
                    )

                def percentile(values, fraction):
                    ordered = sorted(float(value) for value in values)
                    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]

                spread_base = percentile(paired_spreads, 0.95) if len(paired_spreads) >= minimum_quotes else None
                spread_stress = percentile(paired_spreads, 0.99) if len(paired_spreads) >= minimum_quotes else None
                slippage_base = percentile(slippage_bps_samples, 0.95) if len(slippage_bps_samples) >= minimum_deals else None
                slippage_stress = percentile(slippage_bps_samples, 0.99) if len(slippage_bps_samples) >= minimum_deals else None

                if not reasons:
                    if spread_stress < spread_base or slippage_stress < slippage_base:
                        reasons.append("stress percentile calculation was inconsistent")
                    else:
                        profile = {
                            "broker": "cTrader account metadata",
                            "account_type": str(getattr(broker.status, "account_type", "unknown")),
                            "account_id": int(getattr(broker, "_account_id")),
                            "broker_symbol": broker_symbol,
                            "alias_schema": "oracle.ctrader_research_symbol_aliases.v1",
                            "profile_version": f"ctrader-cost-v2-{now_ms}-{symbol.lower()}",
                            "profile_status": "api_observed_costs",
                            "promotion_eligible": False,
                            "price_basis": "bid",
                            "commission_bps": commission_base,
                            "slippage_bps": slippage_base,
                            "spread_bps": spread_base,
                            "stress_slippage_bps": slippage_stress,
                            "stress_spread_bps": spread_stress,
                            "commission_source": commission_source,
                            "slippage_status": "api_observed",
                            "slippage_method": "matched cTrader fills versus execution-time historical quotes; p95 base / p99 stress",
                            "method": "cTrader commission schedule and historical BID/ASK ticks; p95 base / p99 stress",
                            "evidence": {
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                                "window_start_ms": window_start_ms, "window_end_ms": now_ms,
                                "window_hours": window_hours,
                                "matched_spread_quotes": len(paired_spreads),
                                "symbol_filled_deals": len(symbol_deals),
                                "deal_history_records": len(all_deals),
                                "deal_history_days": deal_history_days,
                                "deal_history_request_count": deals_result.get("request_count"),
                                "commission_deals": len(commission_bps_samples),
                                "slippage_deals_checked": attempted_slippage_deals,
                                "matched_slippage_deals": len(slippage_bps_samples),
                                "fill_quote_scale_mismatches": fill_quote_scale_mismatches,
                                "commission_type": metadata.get("commissionType"),
                                "precise_trading_commission_rate": metadata.get("preciseTradingCommissionRate"),
                                "minimum_commission": metadata.get("preciseMinCommission"),
                                "base_asset": base_asset, "quote_asset": quote_asset,
                                "quote_asset_to_usd_rate": quote_to_usd,
                                "median_mid_price": commission_price_reference,
                                "reference_bar_close": reference_mid,
                                "live_spread_bps": live_spread_bps,
                                "rejected_tick_price_scale_count": rejected_scale_ticks,
                                "bid_raw_tick_min": min(bid_raw_ticks) if bid_raw_ticks else None,
                                "bid_raw_tick_max": max(bid_raw_ticks) if bid_raw_ticks else None,
                                "ask_raw_tick_min": min(ask_raw_ticks) if ask_raw_ticks else None,
                                "ask_raw_tick_max": max(ask_raw_ticks) if ask_raw_ticks else None,
                                "deal_history_status": deals_result.get("status", "error"),
                                "pip_position": pip_position,
                            },
                        }
                        profiles[symbol] = profile
                observations.append({
                    "symbol": symbol,
                    "broker_symbol": broker_symbol,
                    "status": "complete" if profile else "blocked",
                    "metadata": metadata,
                    "observed_components": {
                        "commission_bps_per_side": commission_base,
                        "current_spread_bps": live_spread_bps,
                        "spread_bps_p95": spread_base,
                        "spread_bps_p99_stress": spread_stress,
                        "slippage_bps_p95_per_side": slippage_base,
                        "slippage_bps_p99_stress": slippage_stress,
                        "profile_status": "api_observed_costs" if profile else "incomplete_missing_evidence",
                        "profile_usable_for_experiments": bool(profile),
                    },
                    "sample": {"matched_spreads": len(paired_spreads),
                               "median_mid_price": current_mid,
                               "reference_bar_close": reference_mid,
                               "live_quote_status": live_quote_result.get("status") if isinstance(live_quote_result, dict) else "error",
                               "live_spread_bps": live_spread_bps,
                               "rejected_tick_price_scale_count": rejected_scale_ticks,
                               "bid_raw_tick_min": min(bid_raw_ticks) if bid_raw_ticks else None,
                               "bid_raw_tick_max": max(bid_raw_ticks) if bid_raw_ticks else None,
                               "ask_raw_tick_min": min(ask_raw_ticks) if ask_raw_ticks else None,
                               "ask_raw_tick_max": max(ask_raw_ticks) if ask_raw_ticks else None,
                               "filled_deals": len(symbol_deals),
                               "slippage_deals_checked": attempted_slippage_deals,
                               "commission_observations": len(commission_bps_samples),
                               "matched_slippage_deals": len(slippage_bps_samples),
                               "fill_quote_scale_mismatches": fill_quote_scale_mismatches,
                               "bid_tick_status": "complete" if bid_complete else "incomplete",
                               "ask_tick_status": "complete" if ask_complete else "incomplete"},
                    "cost_profile": profile,
                    "reasons": reasons,
                })
            except Exception as exc:
                log.warning("cTrader cost observation failed for %s: %s", symbol, type(exc).__name__)
                observations.append({"status": "error", "symbol": symbol,
                                     "message": f"cost observation failed: {type(exc).__name__}"})
        unmatched = [item for item in observations
                     if item.get("status") == "blocked"
                     and str(item.get("message", "")).startswith("broker does not list mapped symbol ")]
        observations_by_symbol = {
            str(item.get("symbol", "")).upper(): item
            for item in observations if isinstance(item, dict)
        }
        profile_ready = sum(
            isinstance(item.get("cost_profile"), dict)
            for item in observations if isinstance(item, dict)
        )
        symbols_with_fills = 0
        symbols_meeting_fill_threshold = 0
        symbols_with_commission = 0
        symbols_with_live_spread = 0
        symbols_with_spread_history = 0
        symbols_with_measured_slippage = 0
        per_symbol_summary = []
        for symbol in symbols:
            key = str(symbol).strip().upper()
            item = observations_by_symbol.get(key)
            if item is None:
                continue
            sample = item.get("sample", {}) if isinstance(item.get("sample"), dict) else {}
            components = item.get("observed_components", {}) if isinstance(item.get("observed_components"), dict) else {}
            filled_deals = int(sample.get("filled_deals") or 0)
            matched_slippage = int(sample.get("matched_slippage_deals") or 0)
            matched_spreads = int(sample.get("matched_spreads") or 0)
            symbols_with_fills += filled_deals > 0
            symbols_meeting_fill_threshold += filled_deals >= minimum_deals
            symbols_with_commission += components.get("commission_bps_per_side") is not None
            symbols_with_live_spread += components.get("current_spread_bps") is not None
            complete_tick_window = (
                sample.get("bid_tick_status") == "complete"
                and sample.get("ask_tick_status") == "complete"
            )
            symbols_with_spread_history += matched_spreads >= minimum_quotes and complete_tick_window
            symbols_with_measured_slippage += matched_slippage >= minimum_deals and deal_status_ok
            blockers = item.get("reasons")
            if not isinstance(blockers, list) or not blockers:
                message = item.get("message")
                blockers = [message] if isinstance(message, str) and message else []
            per_symbol_summary.append({
                "symbol": key,
                "broker_symbol": item.get("broker_symbol"),
                "status": item.get("status", "error"),
                "filled_deals": filled_deals,
                "minimum_fills_required": minimum_deals,
                "matched_slippage_deals": matched_slippage,
                "matched_spread_quotes": matched_spreads,
                "minimum_spread_quotes_required": minimum_quotes,
                "spread_history_complete": complete_tick_window,
                "commission_observed": components.get("commission_bps_per_side") is not None,
                "live_spread_snapshot_observed": components.get("current_spread_bps") is not None,
                "slippage_history_complete": deal_status_ok,
                "cost_profile_ready": isinstance(item.get("cost_profile"), dict),
                "reasons": blockers,
            })
        collection_summary = {
            "schema": "forge.oracle.cost_observation_summary.v1",
            "symbol_count": len(observations),
            "profiles_ready": profile_ready,
            "symbols_with_any_fills": symbols_with_fills,
            "symbols_meeting_fill_threshold": symbols_meeting_fill_threshold,
            "symbols_with_commission_metadata": symbols_with_commission,
            "symbols_with_live_spread_snapshot": symbols_with_live_spread,
            "symbols_with_qualified_historical_spread": symbols_with_spread_history,
            "symbols_with_qualified_slippage": symbols_with_measured_slippage,
            "minimum_fills_required": minimum_deals,
            "minimum_spread_quotes_required": minimum_quotes,
            "symbols": per_symbol_summary,
        }
        result = {"status": "complete", "source": "ctrader_open_api",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "count": len(observations), "observations": observations,
                "cost_profiles": profiles,
                "summary": collection_summary,
                "deal_history_status": deals_result.get("status", "error"),
                "deal_history_records": len(all_deals),
                "deal_history_days": deal_history_days,
                "deal_history_request_count": deals_result.get("request_count"),
                # Exact names are diagnostic evidence only. Oracle never maps a
                # canonical symbol to a broker instrument implicitly.
                "broker_symbol_catalog": broker.symbol_catalog() if unmatched else []}
        result["chronicle_artifacts"] = self._persist_cost_observation_files(result)
        return result

    def _persist_cost_observation_files(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Keep versioned broker evidence in Chronicle's durable research area."""
        root = Path(__file__).resolve().parents[2] / "Chronicle" / "memory" / "research" / "oracle_cost_profiles"
        profiles = result.get("cost_profiles", {})
        observations = result.get("observations", [])
        by_symbol = {str(item.get("symbol", "")).upper(): item
                     for item in observations if isinstance(item, dict)}
        artifacts = {}
        failures = []
        for symbol, observation in by_symbol.items():
            if not re.fullmatch(r"[A-Z0-9._-]{1,24}", symbol):
                failures.append(symbol)
                continue
            profile = profiles.get(symbol) if isinstance(profiles, dict) else None
            version = profile.get("profile_version") if isinstance(profile, dict) else result.get("observed_at", "unknown")
            version_id = hashlib.sha256(str(version).encode("utf-8")).hexdigest()[:20]
            record = {
                "schema": "forge.oracle.cost_profile_record.v1",
                "symbol": symbol, "status": observation.get("status", "blocked"),
                "account_id": profile.get("account_id") if isinstance(profile, dict) else None,
                "broker_symbol": observation.get("broker_symbol"),
                "profile": profile, "observation": observation,
                "observed_at": result.get("observed_at"),
                "source": result.get("source", "ctrader_open_api"),
            }
            directory = root / symbol
            version_path = directory / f"{version_id}.json"
            latest_path = directory / "latest.json"
            try:
                directory.mkdir(parents=True, exist_ok=True)
                for path, value in ((version_path, record), (latest_path, record)):
                    encoded = (json.dumps(value, sort_keys=True, indent=2,
                                          ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="wb", dir=directory,
                                                         prefix=".cost.", suffix=".tmp", delete=False) as stream:
                            temporary = Path(stream.name)
                            stream.write(encoded)
                            stream.flush()
                            os.fsync(stream.fileno())
                        temporary.replace(path)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                artifacts[symbol] = {
                    "status": record["status"],
                    "version_path": str(version_path.relative_to(root.parent.parent.parent)).replace("\\", "/"),
                    "latest_path": str(latest_path.relative_to(root.parent.parent.parent)).replace("\\", "/"),
                }
            except (OSError, TypeError, ValueError) as exc:
                log.error("Could not persist cost evidence for %s: %s", symbol, type(exc).__name__)
                failures.append(symbol)
        summary = result.get("summary")
        if isinstance(summary, dict):
            summary_path = root / "collection_summary.json"
            temporary = None
            try:
                root.mkdir(parents=True, exist_ok=True)
                encoded = (json.dumps(summary, sort_keys=True, indent=2,
                                      ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".summary.",
                                                 suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(summary_path)
                artifacts["_summary"] = {
                    "status": "complete",
                    "latest_path": str(summary_path.relative_to(root.parent.parent.parent)).replace("\\", "/"),
                }
            except (OSError, TypeError, ValueError) as exc:
                log.error("Could not persist cost collection summary: %s", type(exc).__name__)
                failures.append("_summary")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"status": "partial" if failures else "complete",
                "storage_backend": "chronicle_research_branch_snapshot",
                "artifacts": artifacts, "failures": sorted(set(failures))}
