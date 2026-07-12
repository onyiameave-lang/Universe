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

_UA = "MarketOracleAI/1.0 (AI Ecosystem financial intelligence)"
_TIMEOUT = 15


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

    def _map(self, symbol: str) -> str:
        # map common FX/crypto notations to yfinance tickers
        m = {
            # FX majors and crosses (Yahoo requires =X on 6-char pairs; USDJPY is the one exception)
            "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
            "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
            "NZDUSD": "NZDUSD=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
            "GBPJPY": "GBPJPY=X",
            # metals / commodities
            "XAUUSD": "GC=F", "USOIL": "CL=F",
            # crypto (Yahoo requires -USD)
            "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "SOLUSD": "SOL-USD",
            "XRPUSD": "XRP-USD", "BNBUSD": "BNB-USD", "ADAUSD": "ADA-USD",
            # indices (Yahoo requires ^ prefix)
            "SPX": "^GSPC", "NASDAQ": "^IXIC", "DJI": "^DJI", "RUT": "^RUT",
            "VIX": "^VIX", "FTSE": "^FTSE", "DAX": "^GDAXI", "CAC40": "^FCHI",
            "NIKKEI": "^N225", "HSI": "^HSI", "SENSEX": "^BSESN", "ASX200": "^AXJO",
            "DXY": "DX-Y.NYB",
            # major US mega-cap stocks (already in Yahoo's native format, pass through)
            "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA", "GOOGL": "GOOGL",
            "AMZN": "AMZN", "META": "META", "TSLA": "TSLA", "BRKB": "BRK-B",
            "LLY": "LLY", "V": "V", "JPM": "JPM",
        }
        return m.get(symbol.upper(), symbol)


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

    def _map(self, symbol: str) -> str:
        m = {
            "EURUSD": "eurusd", "GBPUSD": "gbpusd", "USDJPY": "usdjpy",
            "AUDUSD": "audusd", "USDCAD": "usdcad", "USDCHF": "usdchf",
            "NZDUSD": "nzdusd", "EURGBP": "eurgbp", "EURJPY": "eurjpy",
            "GBPJPY": "gbpjpy",
            "XAUUSD": "xauusd", "USOIL": "cl.f",
            "BTCUSD": "btcusd", "ETHUSD": "ethusd", "SOLUSD": "solusd",
            "XRPUSD": "xrpusd", "BNBUSD": "bnbusd", "ADAUSD": "adausd",
            "SPX": "^spx", "NASDAQ": "^ndq", "DJI": "^dji", "RUT": "^rut",
            "VIX": "^vix", "FTSE": "^ftm", "DAX": "^dax", "CAC40": "^cac",
            "NIKKEI": "^nkx", "HSI": "^hsi", "SENSEX": "^sensex", "ASX200": "^asx",
        }
        return m.get(symbol.upper(), symbol.lower())


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
        for src in self.sources:
            if not getattr(src, "available", False):
                continue
            series = src.fetch(symbol, period, interval)
            if series and series.bars:
                self._save_cache(symbol, series)
                return {"status": "complete", "series": series,
                       "source": series.source, "bars": len(series.bars)}

        if allow_cache_fallback:
            cached = self._load_cache(symbol)
            if cached is not None:
                logging.getLogger("oracle.market_data").warning(
                    "all live sources failed for %s; using cached data (%.1fh old)",
                    symbol, cached["age_hours"])
                return {"status": "complete", "series": cached["series"],
                       "source": cached["series"].source, "bars": len(cached["series"].bars),
                       "cache_age_hours": round(cached["age_hours"], 1),
                       "warning": f"live sources unreachable, using {cached['age_hours']:.1f}h-old cached data"}

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