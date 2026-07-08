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
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
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
    except Exception:
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
        except Exception:
            return None

    def _map(self, symbol: str) -> str:
        # map common FX/crypto notations to yfinance tickers
        m = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
             "XAUUSD": "GC=F", "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
             "SPX": "^GSPC", "NASDAQ": "^IXIC", "DXY": "DX-Y.NYB", "USOIL": "CL=F"}
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
        # keep ~6 months of daily bars
        return Series(symbol, bars[-130:], "stooq")

    def _map(self, symbol: str) -> str:
        m = {"EURUSD": "eurusd", "GBPUSD": "gbpusd", "USDJPY": "usdjpy",
             "XAUUSD": "xauusd", "BTCUSD": "btcusd", "SPX": "^spx", "USOIL": "cl.f"}
        return m.get(symbol.upper(), symbol.lower())


class MarketData:
    """Aggregates sources with failover. Honest empty result when all fail."""

    def __init__(self):
        self.sources = [YFinanceSource(), StooqSource()]

    def get(self, symbol: str, period: str = "6mo", interval: str = "1d") -> Dict[str, Any]:
        for src in self.sources:
            if not getattr(src, "available", False):
                continue
            series = src.fetch(symbol, period, interval)
            if series and series.bars:
                return {"status": "complete", "series": series,
                       "source": series.source, "bars": len(series.bars)}
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
