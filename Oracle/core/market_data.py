"""
Oracle.core.market_data
======================
Institutional market data acquisition. (Book I Part IV Article VII; Book II Ch IV.)

Real OHLCV data from live sources, with honest degradation:
  * yfinance   free equities/FX/crypto/indices OHLCV     [needs yfinance]
  * StooqCSV   free key-less daily OHLCV CSV endpoint     [key-free fallback]

If neither is reachable, the loader returns an explicit empty result with a
reason. It NEVER fabricates prices. A small synthetic generator exists ONLY
for offline unit tests and is clearly labeled as such (never used for live signals).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import time
import urllib.parse
import urllib.request
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import os  # noqa: E402 (needed for env var above; placed after existing imports)

_UA = "MarketOracleAI/1.0 (AI Ecosystem financial intelligence)"
_TIMEOUT = 15
try:
    _YFINANCE_TIMEOUT_SEC = max(1.0, float(os.getenv("YFINANCE_TIMEOUT_SEC", "10")))
except ValueError:
    _YFINANCE_TIMEOUT_SEC = 10.0

# ---- In-memory TTL cache for market data ----
# Prevents repeated yfinance/Stooq HTTP calls within the same trading cycle.
# Default: 240 s (4 min) — safe for a 300 s interval loop.
# Override: set MARKET_DATA_CACHE_TTL_SEC env var (e.g. "60" for 1-min bars).
_MARKET_DATA_CACHE_TTL_SEC: float = float(os.getenv("MARKET_DATA_CACHE_TTL_SEC", "240"))
_BROKER_DATA_CACHE_TTL_SEC: float = float(os.getenv("ORACLE_BROKER_DATA_TTL_SEC", "15"))
_MAX_CORRUPTED_BAR_FRACTION: float = float(os.getenv("ORACLE_MAX_CORRUPTED_BAR_FRACTION", "0.10"))
_MAX_EXTERNAL_PRICE_DRIFT_PCT: float = float(os.getenv("ORACLE_MAX_EXTERNAL_PRICE_DRIFT_PCT", "0.025"))
_market_data_mem_cache: Dict[str, Dict[str, Any]] = {}   # key -> {result, ts}


@dataclass
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Series:
    symbol: str
    bars: List[Bar] = field(default_factory=list)
    source: str = ""
    discarded_bars: int = 0
    total_raw_bars: int = 0
    latest_bar_discarded: bool = False

    @property
    def closes(self) -> List[float]:
        return [b.close for b in self.bars]

    @property
    def opens(self) -> List[float]:
        return [b.open for b in self.bars]

    @property
    def highs(self) -> List[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> List[float]:
        return [b.low for b in self.bars]

    @property
    def last(self) -> Optional[float]:
        return self.bars[-1].close if self.bars else None

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "source": self.source, "bars": len(self.bars),
                "last": self.last, "first_ts": self.bars[0].ts if self.bars else None,
                "last_ts": self.bars[-1].ts if self.bars else None,
                "discarded_bars": self.discarded_bars,
                "latest_bar_discarded": self.latest_bar_discarded}


def _bar_validation_error(bar: Bar) -> Optional[str]:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if any(not math.isfinite(value) for value in values):
        return "non-finite OHLCV value"
    if bar.low <= 0:
        return "non-positive low"
    if bar.volume < 0:
        return "negative volume"
    if bar.high < max(bar.open, bar.close):
        return "high below open or close"
    if bar.low > min(bar.open, bar.close):
        return "low above open or close"
    return None


def _get(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logging.getLogger("oracle.market_data").warning("stooq request failed for %s: %s", url, exc)
        return None


class BrokerMarketDataSource:
    """Priority #1 OHLCV source: pulls directly from a connected MT5 or cTrader broker.

    Eliminates Yahoo Finance / Stooq latency, broken bars, and futures-vs-spot
    price mismatches whenever a live or demo broker connection is active.
    """
    name = "broker"

    _INTERVAL_TO_PERIOD: Dict[str, str] = {
        "1m": "M1", "m1": "M1",
        "5m": "M5", "m5": "M5",
        "15m": "M15", "m15": "M15",
        "30m": "M30", "m30": "M30",
        "45m": "M45", "m45": "M45",
        "1h": "H1", "60m": "H1", "h1": "H1",
        "4h": "H4", "h4": "H4",
        "1d": "D1", "d1": "D1",
        "1wk": "W1", "w1": "W1",
    }
    _PERIOD_DAYS: Dict[str, int] = {
        "1mo": 35, "3mo": 100, "6mo": 210, "1y": 380, "2y": 760, "5y": 1830,
    }
    _PERIOD_BARS_D1: Dict[str, int] = {
        "1mo": 30, "3mo": 75, "6mo": 160, "1y": 265, "2y": 530, "5y": 1300,
    }

    def __init__(self, broker: Any = None, symbol_mapper: Any = None):
        self.broker = broker
        self.symbol_mapper = symbol_mapper

    def bind(self, broker: Any, symbol_mapper: Any = None) -> None:
        self.broker = broker
        self.symbol_mapper = symbol_mapper

    @property
    def available(self) -> bool:
        if self.broker is None:
            return False
        status = getattr(self.broker, "status", None)
        connected = bool(getattr(status, "connected", False))
        return connected and callable(getattr(self.broker, "historical_bars", None))

    def resolve_broker_symbol(self, symbol: str) -> Optional[str]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        if self.symbol_mapper is not None and callable(getattr(self.symbol_mapper, "translate", None)):
            mapped = self.symbol_mapper.translate(sym)
            if mapped:
                return str(mapped)
            # If the mapper was built with symbols and explicitly left this unmapped, respect that
            mapper_map = getattr(self.symbol_mapper, "_map", None)
            if isinstance(mapper_map, dict) and mapper_map:
                return None
        symbol_map = getattr(self.broker, "symbol_map", None)
        if isinstance(symbol_map, dict) and sym in symbol_map:
            return str(symbol_map[sym])
        return sym

    def fetch(self, symbol: str, period: str = "6mo", interval: str = "1d") -> Optional[Series]:
        if not self.available:
            return None
        logger = logging.getLogger("oracle.market_data")
        broker_sym = self.resolve_broker_symbol(symbol)
        if not broker_sym:
            return None
        tf_period = self._INTERVAL_TO_PERIOD.get(str(interval or "1d").lower(), "D1")
        days = self._PERIOD_DAYS.get(str(period or "6mo").lower(), 210)
        if tf_period == "D1":
            bar_count = self._PERIOD_BARS_D1.get(str(period or "6mo").lower(), 160)
        elif tf_period == "W1":
            bar_count = max(52, days // 7 + 10)
        elif tf_period == "H4":
            bar_count = min(1500, max(180, days * 6))
        elif tf_period == "H1":
            bar_count = min(2000, max(240, days * 24))
        else:
            bar_count = 500

        now_ms = int(time.time() * 1000)
        from_ms = max(0, now_ms - days * 86400 * 1000)
        try:
            history = self.broker.historical_bars(
                symbol=broker_sym,
                from_timestamp_ms=from_ms,
                to_timestamp_ms=now_ms,
                period=tf_period,
                count=bar_count,
            )
        except TypeError:
            try:
                history = self.broker.historical_bars(
                    broker_sym, from_ms, now_ms, period=tf_period, count=bar_count
                )
            except Exception as exc:
                logger.warning("broker historical_bars failed for %s (%s): %s", symbol, broker_sym, exc)
                return None
        except Exception as exc:
            logger.warning("broker historical_bars failed for %s (%s): %s", symbol, broker_sym, exc)
            return None

        if not isinstance(history, dict) or history.get("status") != "complete":
            return None
        raw_bars = history.get("bars") or []
        if not raw_bars:
            return None

        bars: List[Bar] = []
        invalid_bars = []
        last_bar_invalid = False
        for idx, item in enumerate(raw_bars):
            ts_val = str(item.get("timestamp") or item.get("ts") or "")
            if tf_period == "D1" and "T" in ts_val:
                ts_val = ts_val.split("T", 1)[0]
            try:
                bar = Bar(
                    ts=ts_val,
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item.get("volume", 0.0) or 0.0),
                )
            except (KeyError, TypeError, ValueError):
                invalid_bars.append((ts_val, "malformed bar fields"))
                if idx == len(raw_bars) - 1:
                    last_bar_invalid = True
                continue
            reason = _bar_validation_error(bar)
            if reason:
                invalid_bars.append((bar.ts, reason))
                if idx == len(raw_bars) - 1:
                    last_bar_invalid = True
                continue
            bars.append(bar)

        if not bars:
            return None
        source_label = str(history.get("source") or f"broker:{broker_sym}")
        return Series(
            symbol=symbol.upper(),
            bars=bars,
            source=source_label,
            discarded_bars=len(invalid_bars),
            total_raw_bars=len(raw_bars),
            latest_bar_discarded=last_bar_invalid,
        )


class YFinanceSource:
    name = "yfinance"

    @property
    def available(self) -> bool:
        try:
            import yfinance  # noqa
            return True
        except Exception:
            return False

    def fetch(self, symbol: str, period: str = "6mo", interval: str = "1d") -> Optional[Series]:
        try:
            import warnings
            import yfinance as yf
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=".*inplace keyword.*",
                    category=Warning,
                )
                df = yf.Ticker(self._map(symbol)).history(
                    period=period, interval=interval, timeout=_YFINANCE_TIMEOUT_SEC)
            if df is None or df.empty:
                return None
            bars = []
            invalid_bars = []
            total_rows = len(df)
            last_bar_invalid = False
            for row_idx, (ts, row) in enumerate(df.iterrows()):
                ts_str = str(ts.date()) if hasattr(ts, "date") else str(ts)
                bar = Bar(ts_str, float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]),
                          float(row.get("Volume", 0) or 0))
                reason = _bar_validation_error(bar)
                if reason:
                    invalid_bars.append((bar.ts, reason))
                    if row_idx == total_rows - 1:
                        last_bar_invalid = True
                    continue
                bars.append(bar)
            if invalid_bars:
                logging.getLogger("oracle.market_data").warning(
                    "discarding %d/%d invalid yfinance bars for %s "
                    "(first invalid bar %s: %s); retaining %d valid bars",
                    len(invalid_bars), total_rows, symbol,
                    invalid_bars[0][0], invalid_bars[0][1], len(bars))
            if not bars:
                return None
            return Series(
                symbol,
                bars,
                "yfinance",
                discarded_bars=len(invalid_bars),
                total_raw_bars=total_rows,
                latest_bar_discarded=last_bar_invalid,
            )
        except Exception as exc:
            logging.getLogger("oracle.market_data").warning(
                "yfinance fetch failed for %s: %s", symbol, exc)
            return None

    # ── FIX-YF: comprehensive Oracle→yfinance ticker map ──────────────────────
    # Env-var override: YFINANCE_TICKER_MAP=USOIL:CL=F,NATGAS:NG=F
    # Entries here take precedence over the built-in table below.
    # ──────────────────────────────────────────────────────────────────────────
    _YFINANCE_MAP: Dict[str, str] = {
        # ── FX majors ─────────────────────────────────────────────────────────
        "EURUSD": "EURUSD=X",  "GBPUSD": "GBPUSD=X",  "USDJPY": "JPY=X",
        "AUDUSD": "AUDUSD=X",  "USDCAD": "USDCAD=X",  "USDCHF": "USDCHF=X",
        "NZDUSD": "NZDUSD=X",  "EURGBP": "EURGBP=X",  "EURJPY": "EURJPY=X",
        "GBPJPY": "GBPJPY=X",
        # ── FX crosses (all need =X suffix on Yahoo) ──────────────────────────
        "AUDJPY":  "AUDJPY=X",  "CADJPY":  "CADJPY=X",  "CHFJPY":  "CHFJPY=X",
        "EURAUD":  "EURAUD=X",  "EURCAD":  "EURCAD=X",  "EURCHF":  "EURCHF=X",
        "EURNZD":  "EURNZD=X",  "GBPAUD":  "GBPAUD=X",  "GBPCAD":  "GBPCAD=X",
        "GBPCHF":  "GBPCHF=X",  "GBPNZD":  "GBPNZD=X",  "AUDCAD":  "AUDCAD=X",
        "AUDCHF":  "AUDCHF=X",  "AUDNZD":  "AUDNZD=X",  "NZDCAD":  "NZDCAD=X",
        "NZDCHF":  "NZDCHF=X",  "NZDJPY":  "NZDJPY=X",  "CADCHF":  "CADCHF=X",
        "USDNOK":  "USDNOK=X",  "USDSEK":  "USDSEK=X",  "USDDKK":  "USDDKK=X",
        "USDSGD":  "USDSGD=X",  "USDHKD":  "USDHKD=X",  "USDMXN":  "USDMXN=X",
        "USDZAR":  "USDZAR=X",  "USDTRY":  "USDTRY=X",  "USDPLN":  "USDPLN=X",
        "USDCZK":  "USDCZK=X",  "USDHUF":  "USDHUF=X",
        # ── Metals ────────────────────────────────────────────────────────────
        "XAUUSD":  "GC=F",      # Gold futures
        "XAGUSD":  "SI=F",      # Silver futures
        "XPTUSD":  "PL=F",      # Platinum futures
        "XPDUSD":  "PA=F",      # Palladium futures
        # ── Energy ────────────────────────────────────────────────────────────
        "USOIL":   "CL=F",      # WTI Crude Oil futures
        "UKOIL":   "BZ=F",      # Brent Crude Oil futures
        "NATGAS":  "NG=F",      # Natural Gas futures
        # ── Crypto ────────────────────────────────────────────────────────────
        "BTCUSD":  "BTC-USD",   "ETHUSD":  "ETH-USD",   "SOLUSD":  "SOL-USD",
        "XRPUSD":  "XRP-USD",   "BNBUSD":  "BNB-USD",   "ADAUSD":  "ADA-USD",
        "LTCUSD":  "LTC-USD",   "DOTUSD":  "DOT-USD",   "LINKUSD": "LINK-USD",
        "AVAXUSD": "AVAX-USD",  "MATICUSD":"MATIC-USD", "DOGEUSD": "DOGE-USD",
        # ── Indices ───────────────────────────────────────────────────────────
        "US30":    "^DJI",      # Dow Jones Industrial Average
        "US500":   "^GSPC",     # S&P 500
        "NAS100":  "^IXIC",     # Nasdaq Composite (closest to NAS100)
        "GER40":   "^GDAXI",    # DAX 40
        "UK100":   "^FTSE",     # FTSE 100
        "JPN225":  "^N225",     # Nikkei 225
        "AUS200":  "^AXJO",     # ASX 200
        "HK50":    "^HSI",      # Hang Seng
        "FRA40":   "^FCHI",     # CAC 40
        "ESP35":   "^IBEX",     # IBEX 35
        "ITA40":   "FTSEMIB.MI",# FTSE MIB
        "VIX":     "^VIX",      # Volatility Index
        "DXY":     "DX-Y.NYB",  # US Dollar Index
        # legacy / alternate index names
        "SPX":     "^GSPC",     "NASDAQ":  "^IXIC",     "DJI":     "^DJI",
        "RUT":     "^RUT",      "FTSE":    "^FTSE",     "DAX":     "^GDAXI",
        "CAC40":   "^FCHI",     "NIKKEI":  "^N225",     "HSI":     "^HSI",
        "SENSEX":  "^BSESN",    "ASX200":  "^AXJO",
        # ── US mega-cap stocks (pass-through — already Yahoo format) ──────────
        "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA", "GOOGL": "GOOGL",
        "AMZN": "AMZN", "META": "META", "TSLA": "TSLA", "BRKB":  "BRK-B",
        "LLY":  "LLY",  "V":    "V",    "JPM":  "JPM",  "NFLX":  "NFLX",
        "AMD":  "AMD",  "INTC": "INTC", "CRM":  "CRM",  "ORCL":  "ORCL",
    }

    @classmethod
    def _load_env_overrides(cls) -> Dict[str, str]:
        """Parse YFINANCE_TICKER_MAP=USOIL:CL=F,NATGAS:NG=F from environment."""
        raw = os.getenv("YFINANCE_TICKER_MAP", "").strip()
        overrides: Dict[str, str] = {}
        if not raw:
            return overrides
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" not in pair:
                logging.getLogger("oracle.market_data").warning(
                    "YFINANCE_TICKER_MAP: ignoring malformed entry %r (expected KEY:VALUE)", pair)
                continue
            k, v = pair.split(":", 1)
            k, v = k.strip().upper(), v.strip()
            if k and v:
                overrides[k] = v
                logging.getLogger("oracle.market_data").info(
                    "YFINANCE_TICKER_MAP override: %s → %s", k, v)
        return overrides

    def _map(self, symbol: str) -> str:
        """Translate Oracle canonical symbol name to yfinance ticker.

        Resolution order:
          1. YFINANCE_TICKER_MAP env-var overrides (user-configurable)
          2. Built-in _YFINANCE_MAP table (covers all 41 DEFAULT_SYMBOLS)
          3. Auto-detect: 6-char all-alpha → append =X (FX pair heuristic)
          4. Pass-through unchanged (stocks, already-correct tickers)
        """
        sym = symbol.upper()
        # 1. env-var overrides
        overrides = self._load_env_overrides()
        if sym in overrides:
            return overrides[sym]
        # 2. built-in map
        if sym in self._YFINANCE_MAP:
            return self._YFINANCE_MAP[sym]
        # 3. auto-detect FX pair: 6 uppercase letters → append =X
        if len(sym) == 6 and sym.isalpha():
            logging.getLogger("oracle.market_data").debug(
                "yfinance auto-map: %s → %s=X (6-char FX heuristic)", sym, sym)
            return f"{sym}=X"
        # 4. pass-through
        logging.getLogger("oracle.market_data").debug(
            "yfinance pass-through: %s (no map entry; may fail if not a valid Yahoo ticker)", sym)
        return sym


class StooqSource:
    name = "stooq"
    available = True
    URL = "https://stooq.com/q/d/l/?s={sym}&i=d"

    def fetch(self, symbol: str, period: str = "6mo", interval: str = "1d") -> Optional[Series]:
        sym = self._map(symbol)
        body = _get(self.URL.format(sym=urllib.parse.quote(sym)))
        if not body or "Date" not in body[:200]:
            return None
        bars = []
        invalid_bars = []
        raw_rows = list(csv.DictReader(io.StringIO(body)))
        last_bar_invalid = False
        for row_idx, row in enumerate(raw_rows):
            try:
                bar = Bar(row["Date"], float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]),
                          float(row.get("Volume", 0) or 0))
            except (ValueError, KeyError):
                invalid_bars.append((str(row.get("Date", "?")), "parse error"))
                if row_idx == len(raw_rows) - 1:
                    last_bar_invalid = True
                continue
            reason = _bar_validation_error(bar)
            if reason:
                invalid_bars.append((bar.ts, reason))
                if row_idx == len(raw_rows) - 1:
                    last_bar_invalid = True
                continue
            bars.append(bar)
        if not bars:
            return None
        # keep roughly `period`'s worth of daily bars (rough trading-day estimates)
        _PERIOD_BARS = {"1mo": 22, "3mo": 65, "6mo": 130, "1y": 252, "2y": 504, "5y": 1260, "max": None}
        n = _PERIOD_BARS.get(period, 504)
        sliced = bars[-n:] if n else bars
        return Series(
            symbol,
            sliced,
            "stooq",
            discarded_bars=len(invalid_bars),
            total_raw_bars=len(raw_rows),
            latest_bar_discarded=last_bar_invalid,
        )

    # ── FIX-YF: comprehensive Oracle→Stooq ticker map ─────────────────────────
    _STOOQ_MAP: Dict[str, str] = {
        # ── FX majors ─────────────────────────────────────────────────────────
        "EURUSD": "eurusd",  "GBPUSD": "gbpusd",  "USDJPY": "usdjpy",
        "AUDUSD": "audusd",  "USDCAD": "usdcad",  "USDCHF": "usdchf",
        "NZDUSD": "nzdusd",  "EURGBP": "eurgbp",  "EURJPY": "eurjpy",
        "GBPJPY": "gbpjpy",
        # ── FX crosses ────────────────────────────────────────────────────────
        "AUDJPY":  "audjpy",  "CADJPY":  "cadjpy",  "CHFJPY":  "chfjpy",
        "EURAUD":  "euraud",  "EURCAD":  "eurcad",  "EURCHF":  "eurchf",
        "EURNZD":  "eurnzd",  "GBPAUD":  "gbpaud",  "GBPCAD":  "gbpcad",
        "GBPCHF":  "gbpchf",  "GBPNZD":  "gbpnzd",  "AUDCAD":  "audcad",
        "AUDCHF":  "audchf",  "AUDNZD":  "audnzd",  "NZDCAD":  "nzdcad",
        "NZDCHF":  "nzdchf",  "NZDJPY":  "nzdjpy",  "CADCHF":  "cadchf",
        # ── Metals ────────────────────────────────────────────────────────────
        "XAUUSD":  "xauusd",  "XAGUSD":  "xagusd",
        # ── Energy ────────────────────────────────────────────────────────────
        "USOIL":   "cl.f",    # WTI Crude Oil (Stooq futures format)
        "UKOIL":   "lco.f",   # Brent Crude Oil
        "NATGAS":  "ng.f",    # Natural Gas
        # ── Crypto ────────────────────────────────────────────────────────────
        "BTCUSD":  "btcusd",  "ETHUSD":  "ethusd",  "SOLUSD":  "solusd",
        "XRPUSD":  "xrpusd",  "BNBUSD":  "bnbusd",  "ADAUSD":  "adausd",
        "LTCUSD":  "ltcusd",  "DOGEUSD": "dogeusd",
        # ── Indices ───────────────────────────────────────────────────────────
        "US30":    "^dji",    "US500":   "^spx",    "NAS100":  "^ndq",
        "GER40":   "^dax",    "UK100":   "^ftm",    "JPN225":  "^nkx",
        "AUS200":  "^asx",    "HK50":    "^hsi",    "FRA40":   "^cac",
        "VIX":     "^vix",
        # legacy names
        "SPX":     "^spx",    "NASDAQ":  "^ndq",    "DJI":     "^dji",
        "RUT":     "^rut",    "FTSE":    "^ftm",    "DAX":     "^dax",
        "CAC40":   "^cac",    "NIKKEI":  "^nkx",    "HSI":     "^hsi",
        "SENSEX":  "^sensex", "ASX200":  "^asx",
    }

    def _map(self, symbol: str) -> str:
        """Translate Oracle canonical symbol to Stooq ticker.

        Resolution order:
          1. Built-in _STOOQ_MAP table
          2. 6-char all-alpha → lowercase (Stooq FX pair heuristic)
          3. Pass-through as lowercase
        """
        sym = symbol.upper()
        if sym in self._STOOQ_MAP:
            return self._STOOQ_MAP[sym]
        # auto-detect FX pair
        if len(sym) == 6 and sym.isalpha():
            return sym.lower()
        return sym.lower()


class MarketData:
    """Aggregates sources with failover. Priority #1 is the live connected broker
    (MT5 or cTrader) when attached via attach_broker(), followed by yfinance and
    StooqCSV. Falls back to a local disk cache of the last successful fetch per
    symbol if every live source fails -- always honest about whether data is
    live or cached, and how stale a cache hit is."""

    def __init__(self, cache_dir: Optional[str] = None, broker: Any = None,
                 symbol_mapper: Any = None):
        self.broker_source = BrokerMarketDataSource(broker=broker, symbol_mapper=symbol_mapper)
        self.sources: List[Any] = [self.broker_source, YFinanceSource(), StooqSource()]
        oracle_root = Path(__file__).resolve().parents[1]
        self.cache_dir = Path(cache_dir) if cache_dir else oracle_root / "memory" / "data_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if cache_dir is None:
            legacy_cache = oracle_root / "data_cache"
            if legacy_cache.is_dir():
                for source in legacy_cache.glob("*.json"):
                    target = self.cache_dir / source.name
                    if not target.exists():
                        shutil.copy2(source, target)

    def attach_broker(self, broker: Any, symbol_mapper: Any = None) -> None:
        """Attach a connected MT5Broker or CTraderBroker as Priority #1 market data source."""
        self.broker_source.bind(broker, symbol_mapper)
        if self.broker_source not in self.sources:
            self.sources.insert(0, self.broker_source)
        # Purge any non-broker cached entries so subsequent .get() calls immediately
        # pull broker-native bars instead of stale yfinance/stooq bars.
        for key in list(_market_data_mem_cache.keys()):
            cached_src = str((_market_data_mem_cache[key].get("result") or {}).get("source", ""))
            if not cached_src.startswith(("mt5", "ctrader", "broker")):
                _market_data_mem_cache.pop(key, None)

    def _cache_path(self, symbol: str) -> Path:
        safe = symbol.upper().replace("/", "_")
        return self.cache_dir / f"{safe}.json"

    def _save_cache(self, symbol: str, series: "Series") -> None:
        try:
            payload = {
                "symbol": series.symbol, "source": series.source,
                "cached_at": time.time(),
                "bars": [{"ts": b.ts, "open": b.open, "high": b.high,
                          "low": b.low, "close": b.close, "volume": b.volume}
                         for b in series.bars],
            }
            self._cache_path(symbol).write_text(json.dumps(payload), encoding="utf-8")
        except Exception as exc:
            logging.getLogger("oracle.market_data").warning(
                "failed to cache %s: %s", symbol, exc)

    def _load_cache(self, symbol: str) -> Optional[Dict[str, Any]]:
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            bars = [Bar(**b) for b in payload["bars"]]
            series = Series(symbol=payload["symbol"], bars=bars,
                           source=f"cache:{payload.get('source', 'unknown')}")
            invalid = self._series_validation_error(series)
            if invalid:
                logging.getLogger("oracle.market_data").warning(
                    "ignoring invalid cached history for %s: %s", symbol, invalid)
                return None
            age_hours = (time.time() - payload.get("cached_at", 0)) / 3600.0
            return {"series": series, "age_hours": age_hours}
        except Exception as exc:
            logging.getLogger("oracle.market_data").warning(
                "failed to read cache for %s: %s", symbol, exc)
            return None

    def _verify_and_anchor_with_broker_quote(self, symbol: str, series: Series) -> Optional[str]:
        """When a broker is connected and an external source (yfinance/stooq) was used,
        verify the external close price matches the broker's real quote scale and anchor
        the latest bar's close to the live broker bid."""
        if not self.broker_source.available:
            return None
        src_name = str(series.source or "").lower()
        if src_name.startswith(("mt5", "ctrader", "broker")):
            return None
        broker = self.broker_source.broker
        live_quote_fn = getattr(broker, "live_quote", None)
        if not callable(live_quote_fn):
            return None
        broker_sym = self.broker_source.resolve_broker_symbol(symbol)
        if not broker_sym:
            return None
        try:
            quote = live_quote_fn(broker_sym)
        except Exception:
            return None
        if not isinstance(quote, dict):
            return None
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        mid = float(quote.get("mid") or ((bid + ask) / 2.0 if (bid > 0 and ask > 0) else bid))
        if mid <= 0 or not series.bars or series.last is None or series.last <= 0:
            return None
        drift = abs(series.last - mid) / mid
        if drift > _MAX_EXTERNAL_PRICE_DRIFT_PCT:
            return (
                f"external {series.source} price {series.last:.5f} deviates from "
                f"live broker {broker_sym} quote {mid:.5f} by {drift * 100:.2f}% "
                f"(max allowed {_MAX_EXTERNAL_PRICE_DRIFT_PCT * 100:.2f}%)"
            )
        if bid > 0:
            last_bar = series.bars[-1]
            last_bar.close = bid
            if bid > last_bar.high:
                last_bar.high = bid
            if bid < last_bar.low:
                last_bar.low = bid
        return None

    def get(self, symbol: str, period: str = "6mo", interval: str = "1d",
           allow_cache_fallback: bool = True) -> Dict[str, Any]:
        logger = logging.getLogger("oracle.market_data")
        # ---- in-memory TTL cache (prevents redundant HTTP calls per cycle) ----
        _mem_key = f"{symbol.upper()}|{period}|{interval}"
        _now = time.time()
        _cached_mem = _market_data_mem_cache.get(_mem_key)
        if _cached_mem:
            cached_src = str((_cached_mem["result"].get("source") or "")).lower()
            is_broker_src = cached_src.startswith(("mt5", "ctrader", "broker"))
            ttl = _BROKER_DATA_CACHE_TTL_SEC if is_broker_src else _MARKET_DATA_CACHE_TTL_SEC
            # If a broker just connected, do not serve a cached non-broker series
            if self.broker_source.available and not is_broker_src:
                _market_data_mem_cache.pop(_mem_key, None)
            elif (_now - _cached_mem["ts"]) < ttl:
                invalid = self._series_validation_error(_cached_mem["result"].get("series"))
                if not invalid:
                    logger.debug(
                        "market_data mem-cache HIT for %s (%.0fs old, source=%s)",
                        symbol, _now - _cached_mem["ts"], cached_src)
                    return _cached_mem["result"]
                logger.warning("discarding invalid in-memory history for %s: %s", symbol, invalid)
                _market_data_mem_cache.pop(_mem_key, None)

        source_errors = []
        for src in self.sources:
            if not getattr(src, "available", False):
                continue
            series = src.fetch(symbol, period, interval)
            if series and series.bars:
                invalid = self._series_validation_error(series)
                if invalid:
                    source_errors.append(f"{src.name}: {invalid}")
                    logger.warning("rejecting invalid %s history for %s: %s",
                                   src.name, symbol, invalid)
                    continue
                completeness_err = self._series_completeness_error(series)
                if completeness_err:
                    source_errors.append(f"{src.name}: {completeness_err}")
                    logger.warning("rejecting incomplete %s history for %s: %s",
                                   src.name, symbol, completeness_err)
                    continue
                quote_err = self._verify_and_anchor_with_broker_quote(symbol, series)
                if quote_err:
                    source_errors.append(f"{src.name}: {quote_err}")
                    logger.warning("rejecting mismatched %s history for %s: %s",
                                   src.name, symbol, quote_err)
                    continue
                self._save_cache(symbol, series)
                _result = {"status": "complete", "series": series,
                           "source": series.source, "bars": len(series.bars)}
                _market_data_mem_cache[_mem_key] = {"result": _result, "ts": _now}
                return _result
            source_errors.append(f"{src.name}: no data returned")

        if allow_cache_fallback:
            cached = self._load_cache(symbol)
            if cached is not None:
                quote_err = self._verify_and_anchor_with_broker_quote(symbol, cached["series"])
                if quote_err:
                    source_errors.append(f"cache: {quote_err}")
                else:
                    logging.getLogger("oracle.market_data").warning(
                        "all live sources failed for %s; using cached data (%.1fh old)",
                        symbol, cached["age_hours"])
                    _result = {"status": "complete", "series": cached["series"],
                               "source": cached["series"].source, "bars": len(cached["series"].bars),
                               "cache_age_hours": round(cached["age_hours"], 1),
                               "warning": f"live sources unreachable, using {cached['age_hours']:.1f}h-old cached data"}
                    _market_data_mem_cache[_mem_key] = {"result": _result, "ts": _now}
                    return _result

        detail = "; ".join(source_errors) if source_errors else "no configured source is available"
        return {"status": "error", "series": None,
               "message": f"no valid live market data for {symbol}: {detail}"}

    @staticmethod
    def _series_completeness_error(series: Any) -> Optional[str]:
        """Reject series whose most recent bar was discarded or whose corruption rate is excessive."""
        if not isinstance(series, Series) or not series.bars:
            return "series is empty"
        if series.latest_bar_discarded and series.total_raw_bars >= 3:
            return "latest bar in series was invalid and discarded (stale price tail)"
        if series.total_raw_bars >= 20 and series.discarded_bars > 0:
            frac = series.discarded_bars / float(series.total_raw_bars)
            if frac > _MAX_CORRUPTED_BAR_FRACTION:
                return (
                    f"{series.discarded_bars}/{series.total_raw_bars} bars ({frac * 100:.1f}%) "
                    f"were corrupted and discarded (max {_MAX_CORRUPTED_BAR_FRACTION * 100:.1f}%)"
                )
        return None

    @staticmethod
    def _series_validation_error(series: Any) -> Optional[str]:
        """Reject malformed market bars before caching or returning them."""
        if not isinstance(series, Series) or not series.bars:
            return "series is empty"
        invalid_count = 0
        first_invalid = None
        for index, bar in enumerate(series.bars):
            if _bar_validation_error(bar):
                invalid_count += 1
                if first_invalid is None:
                    first_invalid = index
        if invalid_count:
            return f"{invalid_count}/{len(series.bars)} bars violate OHLCV bounds (first invalid index {first_invalid})"
        return None

    @staticmethod
    def synthetic(symbol: str, n: int = 130, seed: int = 7) -> Series:
        """OFFLINE TEST ONLY: labeled synthetic series. Never used for live signals."""
        import random
        rng = random.Random(seed)
        price = 100.0
        bars = []
        for i in range(n):
            drift = math.sin(i / 15) * 0.4
            price *= (1 + (rng.gauss(0, 0.01) + drift * 0.002))
            o = price * (1 + rng.gauss(0, 0.002))
            h = max(o, price) * (1 + abs(rng.gauss(0, 0.003)))
            lo = min(o, price) * (1 - abs(rng.gauss(0, 0.003)))
            bars.append(Bar(f"t{i}", round(o, 4), round(h, 4), round(lo, 4),
                          round(price, 4), rng.randint(1000, 9000)))
        return Series(symbol, bars, "synthetic_test")
