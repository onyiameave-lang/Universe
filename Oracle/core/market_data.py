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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import os  # noqa: E402 (needed for env var above; placed after existing imports)

_UA = "MarketOracleAI/1.0 (AI Ecosystem financial intelligence)"
_TIMEOUT = 15

# ---- In-memory TTL cache for market data ----
# Prevents repeated yfinance/Stooq HTTP calls within the same trading cycle.
# Default: 240 s (4 min) — safe for a 300 s interval loop.
# Override: set MARKET_DATA_CACHE_TTL_SEC env var (e.g. "60" for 1-min bars).
_MARKET_DATA_CACHE_TTL_SEC: float = float(os.getenv("MARKET_DATA_CACHE_TTL_SEC", "240"))
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

    @property
    def closes(self) -> List[float]:
        return [b.close for b in self.bars]

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
                "last_ts": self.bars[-1].ts if self.bars else None}


def _get(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logging.getLogger("oracle.market_data").warning("stooq request failed for %s: %s", url, exc)
        return None


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
            import yfinance as yf
            df = yf.Ticker(self._map(symbol)).history(period=period, interval=interval)
            if df is None or df.empty:
                return None
            bars = []
            for ts, row in df.iterrows():
                bars.append(Bar(str(ts.date()), float(row["Open"]), float(row["High"]),
                              float(row["Low"]), float(row["Close"]),
                              float(row.get("Volume", 0) or 0)))
            return Series(symbol, bars, "yfinance")
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
        for row in csv.DictReader(io.StringIO(body)):
            try:
                bars.append(Bar(row["Date"], float(row["Open"]), float(row["High"]),
                              float(row["Low"]), float(row["Close"]),
                              float(row.get("Volume", 0) or 0)))
            except (ValueError, KeyError):
                continue
        if not bars:
            return None
        # keep roughly `period`'s worth of daily bars (rough trading-day estimates)
        _PERIOD_BARS = {"1mo": 22, "3mo": 65, "6mo": 130, "1y": 252, "2y": 504, "5y": 1260, "max": None}
        n = _PERIOD_BARS.get(period, 504)
        return Series(symbol, bars[-n:] if n else bars, "stooq")

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
    """Aggregates sources with failover. Falls back to a local disk cache
    of the last successful fetch per symbol if every live source fails
    (offline, rate-limited, network down) -- always honest about whether
    data is live or cached, and how stale a cache hit is."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.sources = [YFinanceSource(), StooqSource()]
        self.cache_dir = Path(cache_dir or (Path(__file__).resolve().parent.parent / "data_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

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
            age_hours = (time.time() - payload.get("cached_at", 0)) / 3600.0
            return {"series": series, "age_hours": age_hours}
        except Exception as exc:
            logging.getLogger("oracle.market_data").warning(
                "failed to read cache for %s: %s", symbol, exc)
            return None

    def get(self, symbol: str, period: str = "6mo", interval: str = "1d",
           allow_cache_fallback: bool = True) -> Dict[str, Any]:
        # ---- in-memory TTL cache (prevents redundant HTTP calls per cycle) ----
        _mem_key = f"{symbol.upper()}|{period}|{interval}"
        _now = time.time()
        _cached_mem = _market_data_mem_cache.get(_mem_key)
        if _cached_mem and (_now - _cached_mem["ts"]) < _MARKET_DATA_CACHE_TTL_SEC:
            logging.getLogger("oracle.market_data").debug(
                "market_data mem-cache HIT for %s (%.0fs old)", symbol,
                _now - _cached_mem["ts"])
            return _cached_mem["result"]

        for src in self.sources:
            if not getattr(src, "available", False):
                continue
            series = src.fetch(symbol, period, interval)
            if series and series.bars:
                self._save_cache(symbol, series)
                _result = {"status": "complete", "series": series,
                           "source": series.source, "bars": len(series.bars)}
                _market_data_mem_cache[_mem_key] = {"result": _result, "ts": _now}
                return _result

        if allow_cache_fallback:
            cached = self._load_cache(symbol)
            if cached is not None:
                logging.getLogger("oracle.market_data").warning(
                    "all live sources failed for %s; using cached data (%.1fh old)",
                    symbol, cached["age_hours"])
                _result = {"status": "complete", "series": cached["series"],
                           "source": cached["series"].source, "bars": len(cached["series"].bars),
                           "cache_age_hours": round(cached["age_hours"], 1),
                           "warning": f"live sources unreachable, using {cached['age_hours']:.1f}h-old cached data"}
                _market_data_mem_cache[_mem_key] = {"result": _result, "ts": _now}
                return _result

        return {"status": "error", "series": None,
               "message": f"no live market data for {symbol} (sources unreachable, no cache available)"}
        return {"status": "error", "series": None,
               "message": f"no live market data for {symbol} (sources unreachable)"}

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