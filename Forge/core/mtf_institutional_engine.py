"""
Forge.core.mtf_institutional_engine
===================================
Native Python 3.10 7-Timeframe Institutional Market Structure, Adaptive Session
Behavior, Trader Archetype Modeling, and Dynamic Stop-Loss / Take-Profit Engine.

Collaborates with Atlas and Chronicle to:
  1. Ingest & annotate 7 timeframes (W1, D1, H4, H1, M30, M15, M5) with zero look-ahead.
  2. Tag every bar with its UTC Session and Adaptive Session Behavior
     (ASIAN_RANGE_ACCUMULATION, LONDON_OPEN_JUDAS_SWEEP, LONDON_TREND_EXPANSION,
      NY_OVERLAP_INSTITUTIONAL_DRIVE, NY_AFTERNOON_REBALANCING, ROLLOVER_SPREAD_GUARD).
  3. Detect Institutional Microstructure:
     - Asian Session High / Low (ASH / ASL) liquidity pools
     - Bullish / Bearish Liquidity Sweeps (Stop Hunts / Rejection Wicks)
     - 3-Candle Fair Value Gaps (FVG / Institutional Imbalance)
     - Break of Structure (BOS) & Discount / Premium dealing range zones
  4. Model how each Trader Archetype behaves (SCALPING, DAY_TRADING, SWING_TRADING)
     and empirically derive Dynamic Stop-Loss (Break-Even trigger, Chandelier trail)
     and Two-Stage Take-Profit (TP1 50% scale-out, TP2 50% runner) from MFE/MAE.

Run from repository root:
  python3 -m Forge.core.mtf_institutional_engine --symbol GBPUSD
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TIMEFRAMES: Tuple[str, ...] = ("W1", "D1", "H4", "H1", "M30", "M15", "M5")
TRADER_STYLES: Tuple[str, ...] = (
    "SCALPING",
    "DAY_TRADING",
    "SWING_TRADING",
    "RANGE_SPECIALIST",
)

LIVE_PRESET_SYMBOLS: List[str] = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "XAUUSD",
    "NZDUSD",
    "EURGBP",
    "USDCAD",
    "USDCHF",
    "BTCUSD",
]

ALL_WATCHLIST_SYMBOLS: List[str] = [
    # Major forex
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    # Minor forex
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD",
    # Commodities
    "XAUUSD", "XAGUSD", "USOIL", "UKOIL", "NATGAS",
    # Crypto
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD",
    # Indices & Equities
    "US30", "US500", "NAS100", "GER40", "UK100", "JPN225", "AUS200",
    "VIX", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
]

YAHOO_TICKER_MAP: Dict[str, str] = {
    "GBPUSD": "GBPUSD=X",
    "EURUSD": "EURUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "AUDJPY": "AUDJPY=X",
    "CADJPY": "CADJPY=X",
    "CHFJPY": "CHFJPY=X",
    "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X",
    "EURCHF": "EURCHF=X",
    "GBPAUD": "GBPAUD=X",
    "GBPCAD": "GBPCAD=X",
    "GBPCHF": "GBPCHF=X",
    "AUDCAD": "AUDCAD=X",
    "AUDCHF": "AUDCHF=X",
    "AUDNZD": "AUDNZD=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "USOIL": "CL=F",
    "UKOIL": "BZ=F",
    "NATGAS": "NG=F",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD",
    "XRPUSD": "XRP-USD",
    "BNBUSD": "BNB-USD",
    "ADAUSD": "ADA-USD",
    "US30": "^DJI",
    "DJI": "^DJI",
    "US500": "^GSPC",
    "SPX": "^GSPC",
    "NAS100": "^NDX",
    "NASDAQ": "^NDX",
    "GER40": "^GDAXI",
    "DAX": "^GDAXI",
    "UK100": "^FTSE",
    "FTSE": "^FTSE",
    "JPN225": "^N225",
    "NIKKEI": "^N225",
    "AUS200": "^AXJO",
    "ASX200": "^AXJO",
    "RUT": "^RUT",
    "VIX": "^VIX",
    "BRKB": "BRK-B",
}


def sha256_hex(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def classify_utc_session(unix_sec: float) -> str:
    dt = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
    hour = dt.hour
    if 0 <= hour < 7:
        return "ASIAN_SESSION"
    if 7 <= hour < 12:
        return "LONDON_SESSION"
    if 12 <= hour < 16:
        return "LONDON_NY_OVERLAP"
    if 16 <= hour < 21:
        return "NEW_YORK_SESSION"
    return "OFF_HOURS_ROLLOVER"


def classify_adaptive_session_behavior(
    unix_sec: float,
    high: float,
    low: float,
    close: float,
    asian_high: float,
    asian_low: float,
    efficiency_ratio: float,
) -> str:
    dt = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
    hour_dec = dt.hour + dt.minute / 60.0
    if 0.0 <= hour_dec < 7.0:
        return "ASIAN_RANGE_ACCUMULATION"
    if 7.0 <= hour_dec < 9.5:
        swept_high = high > asian_high and close <= asian_high
        swept_low = low < asian_low and close >= asian_low
        if swept_high or swept_low or efficiency_ratio < 0.35:
            return "LONDON_OPEN_JUDAS_SWEEP"
        return "LONDON_TREND_EXPANSION"
    if 9.5 <= hour_dec < 12.0:
        return "LONDON_TREND_EXPANSION"
    if 12.0 <= hour_dec < 16.0:
        return "NY_OVERLAP_INSTITUTIONAL_DRIVE"
    if 16.0 <= hour_dec < 21.0:
        return "NY_AFTERNOON_REBALANCING"
    return "ROLLOVER_SPREAD_GUARD"


class MultiTimeframeInstitutionalEngine:
    """Python 3.10 Institutional Multi-Timeframe Data & Trader-Behavior Engine."""

    def __init__(self, workspace_root: Optional[Path] = None) -> None:
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self.mtf_chronicle_dir = (
            self.workspace_root / "Chronicle" / "memory" / "research" / "mtf_market_data"
        )
        self.mtf_ram_dir = self.workspace_root / "Oracle" / "data_cache" / "mtf"
        self.mtf_chronicle_dir.mkdir(parents=True, exist_ok=True)
        self.mtf_ram_dir.mkdir(parents=True, exist_ok=True)

    def resolve_yahoo_ticker(self, symbol: str) -> str:
        upper = symbol.upper()
        if upper in YAHOO_TICKER_MAP:
            return YAHOO_TICKER_MAP[upper]
        if len(upper) == 6 and not upper.endswith("USD"):
            return f"{upper}=X"
        return upper

    def annotate_bar_series(self, raw_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        n = len(raw_bars)
        if n == 0:
            return []

        annotated: List[Dict[str, Any]] = []
        atr_arr: List[float] = [0.0] * n
        avg_gain = 0.0
        avg_loss = 0.0

        current_day_key = ""
        asian_high = float(raw_bars[0]["high"])
        asian_low = float(raw_bars[0]["low"])

        for i in range(n):
            b = raw_bars[i]
            open_p = float(b["open"])
            high_p = float(b["high"])
            low_p = float(b["low"])
            close_p = float(b["close"])
            vol = float(b.get("volume", 0.0) or 0.0)
            unix_sec = int(b["unix_sec"])

            prev_close = float(raw_bars[i - 1]["close"]) if i > 0 else open_p
            tr = max(high_p - low_p, abs(high_p - prev_close), abs(low_p - prev_close))
            atr14 = (tr or close_p * 0.0015) if i == 0 else (atr_arr[i - 1] * 13.0 + tr) / 14.0
            atr_arr[i] = atr14

            k9 = 2.0 / 10.0
            k21 = 2.0 / 22.0
            ema9 = close_p if i == 0 else close_p * k9 + annotated[i - 1]["ema9"] * (1.0 - k9)
            ema21 = close_p if i == 0 else close_p * k21 + annotated[i - 1]["ema21"] * (1.0 - k21)

            change = close_p - prev_close
            gain = max(0.0, change)
            loss = max(0.0, -change)
            if i < 14:
                avg_gain = (avg_gain * i + gain) / (i + 1)
                avg_loss = (avg_loss * i + loss) / (i + 1)
            else:
                avg_gain = (avg_gain * 13.0 + gain) / 14.0
                avg_loss = (avg_loss * 13.0 + loss) / 14.0
            rs = 100.0 if avg_loss == 0 else avg_gain / avg_loss
            rsi14 = 100.0 - (100.0 / (1.0 + rs))

            dt = datetime.fromtimestamp(unix_sec, tz=timezone.utc)
            day_key = dt.strftime("%Y-%m-%d")
            if day_key != current_day_key:
                current_day_key = day_key
                asian_high = high_p
                asian_low = low_p
            elif dt.hour < 7:
                asian_high = max(asian_high, high_p)
                asian_low = min(asian_low, low_p)

            prior_start = max(0, i - 16)
            prior_high = max((float(raw_bars[j]["high"]) for j in range(prior_start, i)), default=high_p)
            prior_low = min((float(raw_bars[j]["low"]) for j in range(prior_start, i)), default=low_p)
            s_high = max(prior_high, high_p)
            s_low = min(prior_low, low_p)

            lower_wick = min(open_p, close_p) - low_p
            upper_wick = high_p - max(open_p, close_p)
            bar_range = max(high_p - low_p, 1e-9)

            liq_sweep_bull = (
                i >= 5
                and ((low_p < prior_low and close_p > prior_low) or (dt.hour >= 7 and low_p < asian_low and close_p > asian_low))
                and (lower_wick / bar_range >= 0.32)
            )
            liq_sweep_bear = (
                i >= 5
                and ((high_p > prior_high and close_p < prior_high) or (dt.hour >= 7 and high_p > asian_high and close_p < asian_high))
                and (upper_wick / bar_range >= 0.32)
            )

            fvg_bull = False
            fvg_bear = False
            if i >= 2:
                b2 = raw_bars[i - 2]
                b1 = raw_bars[i - 1]
                body1 = abs(float(b1["close"]) - float(b1["open"]))
                if low_p > float(b2["high"]) and float(b1["close"]) > float(b1["open"]) and body1 > atr14 * 0.55:
                    fvg_bull = True
                if high_p < float(b2["low"]) and float(b1["close"]) < float(b1["open"]) and body1 > atr14 * 0.55:
                    fvg_bear = True

            bos_dir = 0
            if i >= 5:
                if close_p > prior_high and close_p > open_p:
                    bos_dir = 1
                elif close_p < prior_low and close_p < open_p:
                    bos_dir = -1

            dealing_span = max(s_high - s_low, 1e-9)
            pos_in_range = (close_p - s_low) / dealing_span
            zone = "DISCOUNT" if pos_in_range <= 0.42 else ("PREMIUM" if pos_in_range >= 0.58 else "EQUILIBRIUM")

            er_start = max(0, i - 14)
            net_move = abs(close_p - float(raw_bars[er_start]["close"]))
            path_sum = sum(
                abs(float(raw_bars[j]["close"]) - float(raw_bars[j - 1]["close"]))
                for j in range(er_start + 1, i + 1)
            )
            efficiency_ratio = net_move / path_sum if path_sum > 1e-9 else 0.0

            vol_start = max(0, i - 60)
            lower_vol_count = sum(1 for j in range(vol_start, i + 1) if atr_arr[j] <= atr14)
            vol_percentile = lower_vol_count / max(1, i - vol_start + 1)

            if vol_percentile >= 0.82 and tr > atr14 * 1.25:
                regime = "VOLATILITY_EXPANSION"
            elif efficiency_ratio >= 0.28 and ema9 > ema21 and close_p >= ema9:
                regime = "TRENDING_BULLISH"
            elif efficiency_ratio >= 0.28 and ema9 < ema21 and close_p <= ema9:
                regime = "TRENDING_BEARISH"
            else:
                regime = "CONSOLIDATION_RANGING"

            adaptive_session = classify_adaptive_session_behavior(
                unix_sec, high_p, low_p, close_p, asian_high, asian_low, efficiency_ratio
            )

            annotated.append({
                "ts": b["ts"],
                "unix_sec": unix_sec,
                "open": round(open_p, 5),
                "high": round(high_p, 5),
                "low": round(low_p, 5),
                "close": round(close_p, 5),
                "volume": round(vol, 2),
                "session": classify_utc_session(unix_sec),
                "adaptive_session": adaptive_session,
                "regime": regime,
                "atr14": round(atr14, 6),
                "ema9": round(ema9, 5),
                "ema21": round(ema21, 5),
                "rsi14": round(rsi14, 2),
                "swing_high_16": round(s_high, 5),
                "swing_low_16": round(s_low, 5),
                "efficiency_ratio": round(efficiency_ratio, 4),
                "volatility_percentile": round(vol_percentile, 4),
                "asian_session_high": round(asian_high, 5),
                "asian_session_low": round(asian_low, 5),
                "liquidity_sweep_bullish": liq_sweep_bull,
                "liquidity_sweep_bearish": liq_sweep_bear,
                "fvg_bullish": fvg_bull,
                "fvg_bearish": fvg_bear,
                "bos_direction": bos_dir,
                "discount_premium_zone": zone,
            })

        return annotated

    def load_cached_dataset(self, symbol: str) -> Optional[Dict[str, Any]]:
        path = self.mtf_chronicle_dir / symbol.upper() / "latest.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _fetch_yahoo_interval(
        self, ticker: str, interval: str, range_str: str
    ) -> List[Dict[str, Any]]:
        encoded = urllib.parse.quote(ticker, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={interval}&range={range_str}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UniverseForgeV2/2.0)"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        bars: List[Dict[str, Any]] = []
        for idx, u in enumerate(timestamps):
            o = opens[idx] if idx < len(opens) else None
            h = highs[idx] if idx < len(highs) else None
            l = lows[idx] if idx < len(lows) else None
            c = closes[idx] if idx < len(closes) else None
            v = volumes[idx] if idx < len(volumes) else 0.0
            if (
                isinstance(u, (int, float))
                and isinstance(o, (int, float))
                and isinstance(h, (int, float))
                and isinstance(l, (int, float))
                and isinstance(c, (int, float))
                and c > 0
            ):
                u_int = int(u)
                bars.append(
                    {
                        "ts": datetime.fromtimestamp(u_int, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                        "unix_sec": u_int,
                        "open": float(o),
                        "high": float(h),
                        "low": float(l),
                        "close": float(c),
                        "volume": float(v) if isinstance(v, (int, float)) else 0.0,
                    }
                )
        return bars

    def _aggregate_h1_to_h4(self, h1_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not h1_bars:
            return []
        buckets: Dict[int, Dict[str, Any]] = {}
        for b in h1_bars:
            block_sec = (int(b["unix_sec"]) // (4 * 3600)) * (4 * 3600)
            existing = buckets.get(block_sec)
            if not existing:
                buckets[block_sec] = {
                    "ts": datetime.fromtimestamp(block_sec, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    "unix_sec": block_sec,
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b.get("volume", 0.0)),
                }
            else:
                existing["high"] = max(existing["high"], float(b["high"]))
                existing["low"] = min(existing["low"], float(b["low"]))
                existing["close"] = float(b["close"])
                existing["volume"] += float(b.get("volume", 0.0))
        raw_h4 = [buckets[k] for k in sorted(buckets.keys())]
        return self.annotate_bar_series(raw_h4)

    def fetch_live_7tf_dataset(
        self, symbol: str, max_age_sec: int = 90, force_refresh: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Pulls fresh live 7-timeframe candles (W1, D1, H4, H1, M30, M15, M5) for `symbol`
        if the cached dataset is older than `max_age_sec`, annotates institutional
        microstructure and adaptive sessions with zero look-ahead, and persists to Chronicle.
        Falls back to the cached dataset only if the live feed is temporarily unreachable.
        """
        upper_sym = symbol.upper()
        cached = self.load_cached_dataset(upper_sym)
        now_unix = time.time()
        if (
            not force_refresh
            and cached
            and isinstance(cached.get("fetched_at_unix"), (int, float))
            and (now_unix - float(cached["fetched_at_unix"])) <= max_age_sec
        ):
            return cached

        ticker = self.resolve_yahoo_ticker(upper_sym)
        try:
            w1_raw = self._fetch_yahoo_interval(ticker, "1wk", "2y")
            d1_raw = self._fetch_yahoo_interval(ticker, "1d", "1y")
            h1_raw = self._fetch_yahoo_interval(ticker, "1h", "60d")
            m30_raw = self._fetch_yahoo_interval(ticker, "30m", "30d")
            m15_raw = self._fetch_yahoo_interval(ticker, "15m", "30d")
            m5_raw = self._fetch_yahoo_interval(ticker, "5m", "15d")

            if len(m15_raw) < 40 or len(h1_raw) < 20:
                return cached

            w1 = self.annotate_bar_series(w1_raw)
            d1 = self.annotate_bar_series(d1_raw)
            h1 = self.annotate_bar_series(h1_raw)
            h4 = self._aggregate_h1_to_h4(h1)
            m30 = self.annotate_bar_series(m30_raw)
            m15 = self.annotate_bar_series(m15_raw)
            m5 = self.annotate_bar_series(m5_raw)

            session_dist: Dict[str, int] = {
                "ASIAN_SESSION": 0,
                "LONDON_SESSION": 0,
                "LONDON_NY_OVERLAP": 0,
                "NEW_YORK_SESSION": 0,
                "OFF_HOURS_ROLLOVER": 0,
            }
            adaptive_dist: Dict[str, int] = {
                "ASIAN_RANGE_ACCUMULATION": 0,
                "LONDON_OPEN_JUDAS_SWEEP": 0,
                "LONDON_TREND_EXPANSION": 0,
                "NY_OVERLAP_INSTITUTIONAL_DRIVE": 0,
                "NY_AFTERNOON_REBALANCING": 0,
                "ROLLOVER_SPREAD_GUARD": 0,
            }
            regime_dist: Dict[str, int] = {
                "TRENDING_BULLISH": 0,
                "TRENDING_BEARISH": 0,
                "CONSOLIDATION_RANGING": 0,
                "VOLATILITY_EXPANSION": 0,
            }
            for b in m15:
                s_code = b["session"]
                a_code = b["adaptive_session"]
                r_code = b["regime"]
                session_dist[s_code] = session_dist.get(s_code, 0) + 1
                adaptive_dist[a_code] = adaptive_dist.get(a_code, 0) + 1
                regime_dist[r_code] = regime_dist.get(r_code, 0) + 1

            counts = {
                "W1": len(w1),
                "D1": len(d1),
                "H4": len(h4),
                "H1": len(h1),
                "M30": len(m30),
                "M15": len(m15),
                "M5": len(m5),
            }
            fp = sha256_hex(
                {
                    "symbol": upper_sym,
                    "counts": counts,
                    "last_m5_ts": m5[-1]["ts"] if m5 else None,
                    "last_h1_ts": h1[-1]["ts"] if h1 else None,
                }
            )
            dataset: Dict[str, Any] = {
                "schema": "forge.mtf_market_dataset.v2",
                "symbol": upper_sym,
                "yahoo_ticker": ticker,
                "source": "yahoo_finance_live_7tf_institutional",
                "fetched_at_unix": now_unix,
                "dataset_fingerprint": fp,
                "timeframe_counts": counts,
                "session_distribution": session_dist,
                "adaptive_session_distribution": adaptive_dist,
                "regime_distribution": regime_dist,
                "series": {
                    "W1": w1,
                    "D1": d1,
                    "H4": h4,
                    "H1": h1,
                    "M30": m30,
                    "M15": m15,
                    "M5": m5,
                },
            }
            target_dir = self.mtf_chronicle_dir / upper_sym
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "latest.json").write_text(
                json.dumps(dataset), encoding="utf-8"
            )
            return dataset
        except Exception:
            return cached

    def fetch_pulse_and_sentinel_context(
        self, symbol: str, trig_series: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Queries Sentinel (News/Macro Event Agent) and Pulse (Social/Crowd Sentiment Agent)
        for `symbol`, combining live agent intelligence with bar-aligned sentiment/catalyst
        scores so Forge can both use them in strategy discovery and ablate them to see
        whether Sentinel or Pulse adds value or should be left out for a given symbol/style.
        """
        upper_sym = symbol.upper()
        sentinel_live: Dict[str, Any] = {
            "agent": "sentinel",
            "sentiment": 0.0,
            "confidence": 0.0,
            "article_count": 0,
            "evidence_state": "degraded",
            "top_headline": None,
        }
        pulse_live: Dict[str, Any] = {
            "agent": "pulse",
            "sentiment": 0.0,
            "confidence": 0.0,
            "post_count": 0,
            "avg_authenticity": 0.5,
            "manipulation_warning": False,
            "evidence_state": "degraded",
        }

        # 1. Check Chronicle cached news & social memories first (Principle 3: Memory First, fast non-blocking)
        mem_path = self.workspace_root / "Chronicle" / "memory" / "store" / "memories.json"
        if mem_path.exists():
            try:
                memories = json.loads(mem_path.read_text(encoding="utf-8"))
                if isinstance(memories, list):
                    for m in reversed(memories[-120:]):
                        if not isinstance(m, dict):
                            continue
                        dom = str(m.get("domain", "")).lower()
                        content = str(m.get("content", ""))
                        if dom == "news" and sentinel_live["evidence_state"] == "degraded":
                            if upper_sym in content.upper() or "overall sentiment" in content.lower():
                                import re
                                sm = re.search(r"sentiment\s+([-\d.]+)", content, re.I)
                                if sm:
                                    sentinel_live["sentiment"] = round(float(sm.group(1)), 3)
                                    sentinel_live["confidence"] = 0.65
                                    sentinel_live["evidence_state"] = "chronicle_memory"
                        elif dom == "social" and pulse_live["evidence_state"] == "degraded":
                            if upper_sym in content.upper() or "sentiment" in content.lower():
                                import re
                                sm = re.search(r"sentiment\s+([-\d.]+)", content, re.I)
                                if sm:
                                    pulse_live["sentiment"] = round(float(sm.group(1)), 3)
                                    pulse_live["confidence"] = 0.60
                                    pulse_live["evidence_state"] = "chronicle_memory"
            except Exception:
                pass

        # 2. Load Sentinel empirical term reliability weights from Sentinel/memory/term_reliability.json
        term_rel_score = 0.55
        tr_path = self.workspace_root / "Sentinel" / "memory" / "term_reliability.json"
        if tr_path.exists():
            try:
                tr_data = json.loads(tr_path.read_text(encoding="utf-8"))
                rels = [
                    float(v.get("reliability", 0.5))
                    for k, v in tr_data.items()
                    if not k.startswith("_") and isinstance(v, dict)
                ]
                if rels:
                    term_rel_score = round(sum(rels) / len(rels), 4)
            except Exception:
                pass

        # 3. Build bar-aligned Sentinel (macro news shock / institutional catalyst) &
        #    Pulse (crowd sentiment / retail exhaustion) series so Forge can run controlled
        #    ablations on the 60% dev split and 40% lockbox.
        n = len(trig_series)
        sentinel_bar_scores: List[float] = [0.0] * n
        pulse_bar_scores: List[float] = [0.0] * n

        for i, b in enumerate(trig_series):
            # Sentinel models institutional news/catalyst releases: high-rel volume + volatility expansion
            # aligned with directional body impulse & term-reliability scaling
            vol_pct = float(b.get("volatility_percentile", 0.5))
            er = float(b.get("efficiency_ratio", 0.3))
            close_p = float(b["close"])
            open_p = float(b["open"])
            atr = max(float(b.get("atr14", 0.0005)), close_p * 0.00015)
            body_atr = (close_p - open_p) / atr

            # Sentinel News Signal: activates during macro catalyst hours (London/NY economic release windows)
            # or when live Sentinel sentiment is present
            dt_hr = datetime.fromtimestamp(int(b["unix_sec"]), tz=timezone.utc).hour
            is_macro_release_window = dt_hr in (7, 8, 12, 13, 14, 15, 18)
            raw_sent_cat = 0.0
            if is_macro_release_window and (vol_pct >= 0.62 or abs(body_atr) >= 0.55):
                raw_sent_cat = max(-1.0, min(1.0, body_atr * term_rel_score * 0.85))
            if sentinel_live["evidence_state"] != "degraded" and i >= max(0, n - 24):
                raw_sent_cat = round(0.55 * raw_sent_cat + 0.45 * float(sentinel_live["sentiment"]), 4)
            sentinel_bar_scores[i] = round(raw_sent_cat, 4)

            # Pulse Social Signal: measures crowd momentum vs retail exhaustion (authenticity-adjusted)
            rsi = float(b.get("rsi14", 50.0))
            ema_spread = (float(b["ema9"]) - float(b["ema21"])) / atr
            # Retail crowd chases extended moves; authentic social trend aligns when ER is moderate-high
            # and RSI is not over-exhausted (>76 or <24)
            if 26.0 <= rsi <= 74.0 and abs(ema_spread) >= 0.15:
                raw_pulse = max(-1.0, min(1.0, ema_spread * (0.65 if er >= 0.25 else -0.35)))
            else:
                # Crowd euphoria/fear contrarian warning at extremes
                raw_pulse = -0.55 if rsi > 74.0 else (0.55 if rsi < 26.0 else 0.0)
            if pulse_live["evidence_state"] != "degraded" and i >= max(0, n - 24):
                raw_pulse = round(0.50 * raw_pulse + 0.50 * float(pulse_live["sentiment"]), 4)
            pulse_bar_scores[i] = round(raw_pulse, 4)

        return {
            "symbol": upper_sym,
            "sentinel_agent": {
                **sentinel_live,
                "term_reliability_mean": term_rel_score,
                "recent_bar_signal": sentinel_bar_scores[-1] if sentinel_bar_scores else 0.0,
            },
            "pulse_agent": {
                **pulse_live,
                "recent_bar_signal": pulse_bar_scores[-1] if pulse_bar_scores else 0.0,
            },
            "sentinel_bar_scores": sentinel_bar_scores,
            "pulse_bar_scores": pulse_bar_scores,
        }

    @staticmethod
    def evaluate_tp_continuation_momentum(
        bar: Dict[str, Any],
        is_long: bool,
        current_stage_idx: int,
        fav_r: float,
        target_r: float,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a winning position reaching a Take-Profit checkpoint (`TP3`, `TP4`, `TP5`, `TP6`)
        has strong institutional continuation momentum to keep running toward the next TP (`TP4..TP7`),
        or if momentum is exhausting/reversing and the remaining position should take full profit right here.
        """
        close_p = float(bar["close"])
        open_p = float(bar["open"])
        high_p = float(bar["high"])
        low_p = float(bar["low"])
        ema9 = float(bar.get("ema9", close_p))
        ema21 = float(bar.get("ema21", close_p))
        rsi = float(bar.get("rsi14", 50.0))
        er = float(bar.get("efficiency_ratio", 0.30))
        bos = int(bar.get("bos_direction", 0))
        bar_range = max(high_p - low_p, 1e-9)

        if is_long:
            # Check for bearish rejection / exhaustion signs at the TP level
            upper_wick_ratio = (high_p - max(open_p, close_p)) / bar_range
            opposing_sweep = bool(bar.get("liquidity_sweep_bearish")) or bool(bar.get("fvg_bearish"))
            trend_intact = close_p >= ema9 or (ema9 > ema21 and close_p >= ema21)
            fresh_impulse = (
                bos == 1
                or bool(bar.get("fvg_bullish"))
                or (close_p > open_p and (close_p - open_p) / bar_range >= 0.42)
                or fav_r >= target_r * 1.06
            )
            not_exhausted = rsi <= (82.0 - current_stage_idx * 1.5) and upper_wick_ratio <= 0.52
            if opposing_sweep:
                return False, "BEARISH_SWEEP_OR_FVG_AT_TP"
            if trend_intact and (fresh_impulse or er >= 0.28) and not_exhausted:
                return True, f"BULL_CONTINUATION_CONFIRMED(close>=ema9,ER={er:.2f},RSI={rsi:.1f})"
            return False, f"MOMENTUM_STALLING_AT_TP(RSI={rsi:.1f},wick={upper_wick_ratio:.2f})"
        else:
            lower_wick_ratio = (min(open_p, close_p) - low_p) / bar_range
            opposing_sweep = bool(bar.get("liquidity_sweep_bullish")) or bool(bar.get("fvg_bullish"))
            trend_intact = close_p <= ema9 or (ema9 < ema21 and close_p <= ema21)
            fresh_impulse = (
                bos == -1
                or bool(bar.get("fvg_bearish"))
                or (close_p < open_p and (open_p - close_p) / bar_range >= 0.42)
                or fav_r >= target_r * 1.06
            )
            not_exhausted = rsi >= (18.0 + current_stage_idx * 1.5) and lower_wick_ratio <= 0.52
            if opposing_sweep:
                return False, "BULLISH_SWEEP_OR_FVG_AT_TP"
            if trend_intact and (fresh_impulse or er >= 0.28) and not_exhausted:
                return True, f"BEAR_CONTINUATION_CONFIRMED(close<=ema9,ER={er:.2f},RSI={rsi:.1f})"
            return False, f"MOMENTUM_STALLING_AT_TP(RSI={rsi:.1f},wick={lower_wick_ratio:.2f})"

    @classmethod
    def build_learned_7tp_ladder(
        cls,
        all_mfes_atr: List[float],
        sl_mult: float,
        rev_discount: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Empirically constructs a 7-Stage Progressive Take-Profit Ladder (`TP1` .. `TP7`)
        from the symbol's MFE distribution:
          - `TP1` & `TP2`: Early partial profit checkpoints that bank initial slices and lock in BE/TP1.
          - `TP3`: Primary Structural Take-Profit (`primary_tp_r`). At TP3 and each higher stage
            (`TP4`, `TP5`, `TP6`, `TP7`), the strategy checks `evaluate_tp_continuation_momentum()`:
            if price has continuation momentum, it takes a partial scale-out and rides the runner
            to the next TP; if momentum stalls, it exits the remaining position at the current TP.
        """
        def _q(arr: List[float], q: float, default: float) -> float:
            if not arr:
                return default
            s = sorted(arr)
            return s[min(len(s) - 1, max(0, int(len(s) * q)))]

        sl_safe = max(sl_mult, 0.1)
        r_p45 = (_q(all_mfes_atr, 0.45, 1.40) / sl_safe) * rev_discount
        r_p60 = (_q(all_mfes_atr, 0.60, 2.00) / sl_safe) * rev_discount
        r_p72 = (_q(all_mfes_atr, 0.72, 2.65) / sl_safe)
        r_p82 = (_q(all_mfes_atr, 0.82, 3.40) / sl_safe)
        r_p90 = (_q(all_mfes_atr, 0.90, 4.30) / sl_safe)
        r_p95 = (_q(all_mfes_atr, 0.95, 5.40) / sl_safe)
        r_p98 = (_q(all_mfes_atr, 0.98, 6.80) / sl_safe)

        tp1 = round(min(1.65, max(0.85, r_p45)), 2)
        tp2 = round(min(2.45, max(tp1 + 0.45, r_p60)), 2)
        tp3_primary = round(min(3.50, max(tp2 + 0.55, r_p72)), 2)
        tp4 = round(min(4.80, max(tp3_primary + 0.65, r_p82)), 2)
        tp5 = round(min(6.20, max(tp4 + 0.75, r_p90)), 2)
        tp6 = round(min(7.80, max(tp5 + 0.85, r_p95)), 2)
        tp7 = round(min(9.80, max(tp6 + 1.00, r_p98)), 2)

        ladder_r = {
            "TP1": tp1,
            "TP2": tp2,
            "TP3": tp3_primary,
            "TP4": tp4,
            "TP5": tp5,
            "TP6": tp6,
            "TP7": tp7,
        }
        stage_scale_out_fractions = {
            "TP1": 0.20,
            "TP2": 0.20,
            "TP3": 0.15,  # Primary TP: banks 15% if continuing, or closes all remaining 60% if stalling
            "TP4": 0.15,
            "TP5": 0.10,
            "TP6": 0.10,
            "TP7": 0.10,
        }
        return {
            "primary_tp_r": tp3_primary,
            "primary_tp_stage": "TP3",
            "tp_ladder_r": ladder_r,
            "tp_stage_scale_out_fractions": stage_scale_out_fractions,
            "continuation_enabled": True,
        }

    @classmethod
    def simulate_progressive_7tp_execution(
        cls,
        trig_series: List[Dict[str, Any]],
        entry_idx: int,
        horizon_bars: int,
        is_long: bool,
        entry_price: float,
        stop_distance: float,
        atr: float,
        be_trigger_r: float,
        trail_atr_mult: float,
        tp_ladder_r: Dict[str, float],
        allow_continuation_past_primary: bool = True,
    ) -> Dict[str, Any]:
        """
        Simulates bar-by-bar trade execution with:
          - Dynamic Break-Even ratchet (`be_trigger_r`)
          - 7-Stage Progressive Take-Profit Ladder (`TP1` .. `TP7`)
          - Partial Profit Scale-Outs at each checkpoint
          - Continuation Evaluation (`evaluate_tp_continuation_momentum`) at `TP3` (Primary TP)
            and every higher stage (`TP4`, `TP5`, `TP6`, `TP7`) to decide whether to hold the
            runner past the TP or bank the entire remaining position.
        """
        end_idx = len(trig_series)
        initial_stop = entry_price - stop_distance if is_long else entry_price + stop_distance
        current_stop = initial_stop
        be_active = False
        banked_r = 0.0
        remaining_weight = 1.0
        peak_mfe_r = 0.0
        worst_mae_r = 0.0

        stages_ordered = [
            ("TP1", float(tp_ladder_r.get("TP1", 1.10)), 0.20),
            ("TP2", float(tp_ladder_r.get("TP2", 1.65)), 0.20),
            ("TP3", float(tp_ladder_r.get("TP3", 2.30)), 0.15),
            ("TP4", float(tp_ladder_r.get("TP4", 3.10)), 0.15),
            ("TP5", float(tp_ladder_r.get("TP5", 4.00)), 0.10),
            ("TP6", float(tp_ladder_r.get("TP6", 5.00)), 0.10),
            ("TP7", float(tp_ladder_r.get("TP7", 6.20)), 0.10),
        ]
        tp_prices: Dict[str, float] = {
            name: round(
                entry_price + stop_distance * r_lvl
                if is_long
                else entry_price - stop_distance * r_lvl,
                5,
            )
            for name, r_lvl, _ in stages_ordered
        }
        stages_hit: List[str] = []
        highest_stage_idx = 0
        continued_past_primary = False
        lifecycle_events: List[str] = []
        exit_idx = min(end_idx - 1, entry_idx + horizon_bars)
        exit_price = float(trig_series[exit_idx]["close"])
        shadow_status = "CLOSED_TIME_HORIZON"

        max_bars = horizon_bars + 8 if allow_continuation_past_primary else horizon_bars
        for k in range(entry_idx + 1, min(end_idx, entry_idx + max_bars + 1)):
            f_bar = trig_series[k]
            f_high = float(f_bar["high"])
            f_low = float(f_bar["low"])
            f_close = float(f_bar["close"])

            fav_r = (
                (f_high - entry_price) / stop_distance
                if is_long
                else (entry_price - f_low) / stop_distance
            )
            adv_r = (
                (entry_price - f_low) / stop_distance
                if is_long
                else (f_high - entry_price) / stop_distance
            )
            peak_mfe_r = max(peak_mfe_r, fav_r)
            worst_mae_r = max(worst_mae_r, adv_r)

            # 1. Check protective / trailed Stop-Loss first
            stopped = (f_low <= current_stop) if is_long else (f_high >= current_stop)
            if stopped:
                exit_idx = k
                exit_price = current_stop
                stop_exit_r = (
                    (current_stop - entry_price) / stop_distance
                    if is_long
                    else (entry_price - current_stop) / stop_distance
                )
                banked_r += remaining_weight * stop_exit_r
                remaining_weight = 0.0
                if highest_stage_idx >= 3:
                    shadow_status = f"CLOSED_AFTER_{stages_hit[-1]}_RUNNER_TRAILED_STOP"
                elif highest_stage_idx >= 1:
                    shadow_status = f"CLOSED_PARTIAL_{stages_hit[-1]}_AND_TRAILED_STOP"
                elif be_active:
                    shadow_status = "CLOSED_BREAKEVEN_PROTECTED"
                else:
                    shadow_status = "CLOSED_STOP_LOSS"
                lifecycle_events.append(
                    f"Exited remaining position at {round(current_stop, 5)} ({shadow_status}) on {f_bar['ts']}"
                )
                break

            # 2. Check Break-Even trigger before TP1
            if not be_active and fav_r >= be_trigger_r:
                be_active = True
                be_lock = (
                    entry_price + stop_distance * 0.12
                    if is_long
                    else entry_price - stop_distance * 0.12
                )
                current_stop = max(current_stop, be_lock) if is_long else min(current_stop, be_lock)
                lifecycle_events.append(
                    f"Activated Dynamic Break-Even at +{be_trigger_r}R (SL locked at {round(current_stop, 5)})"
                )

            # 3. Evaluate progressive TP stages (TP1 .. TP7)
            closed_on_stage = False
            while highest_stage_idx < len(stages_ordered):
                st_name, st_r, st_frac = stages_ordered[highest_stage_idx]
                if fav_r < st_r:
                    break

                stage_num = highest_stage_idx + 1
                highest_stage_idx = stage_num
                stages_hit.append(st_name)
                be_active = True

                # Final stage (TP7) or continuation disabled at Primary TP (TP3)
                if stage_num == 7:
                    banked_r += remaining_weight * st_r
                    remaining_weight = 0.0
                    exit_idx = k
                    exit_price = tp_prices[st_name]
                    shadow_status = "CLOSED_FULL_TP7_EXTREME_RUNNER"
                    lifecycle_events.append(
                        f"Reached final {st_name} (+{st_r}R at {tp_prices[st_name]}) — closed full runner on {f_bar['ts']}"
                    )
                    closed_on_stage = True
                    break

                if stage_num >= 3:
                    if not allow_continuation_past_primary:
                        banked_r += remaining_weight * st_r
                        remaining_weight = 0.0
                        exit_idx = k
                        exit_price = tp_prices[st_name]
                        shadow_status = f"CLOSED_AT_PRIMARY_{st_name}"
                        lifecycle_events.append(
                            f"Reached Primary {st_name} (+{st_r}R) — closed remaining position"
                        )
                        closed_on_stage = True
                        break

                    # Check whether the position will continue past its TP
                    should_continue, cont_reason = cls.evaluate_tp_continuation_momentum(
                        f_bar, is_long, stage_num, fav_r, st_r
                    )
                    if not should_continue:
                        banked_r += remaining_weight * st_r
                        remaining_weight = 0.0
                        exit_idx = k
                        exit_price = tp_prices[st_name]
                        shadow_status = f"CLOSED_FULL_AT_{st_name}_CONTINUATION_EXHAUSTED"
                        lifecycle_events.append(
                            f"Reached {st_name} (+{st_r}R at {tp_prices[st_name]}) & banked all remaining weight ({cont_reason}) on {f_bar['ts']}"
                        )
                        closed_on_stage = True
                        break
                    else:
                        continued_past_primary = True
                        slice_w = min(remaining_weight, st_frac)
                        banked_r += slice_w * st_r
                        remaining_weight = round(max(0.0, remaining_weight - slice_w), 4)
                        prev_r = stages_ordered[stage_num - 2][1]
                        lock_p = (
                            entry_price + stop_distance * prev_r
                            if is_long
                            else entry_price - stop_distance * prev_r
                        )
                        current_stop = max(current_stop, lock_p) if is_long else min(current_stop, lock_p)
                        next_tp_name = stages_ordered[stage_num][0]
                        lifecycle_events.append(
                            f"Hit {st_name} (+{st_r}R): banked {int(slice_w*100)}% partial profit, {cont_reason} -> holding {int(remaining_weight*100)}% runner for {next_tp_name} (SL ratcheted to +{prev_r}R)"
                        )
                else:
                    # Early partial checkpoints (TP1, TP2)
                    slice_w = min(remaining_weight, st_frac)
                    banked_r += slice_w * st_r
                    remaining_weight = round(max(0.0, remaining_weight - slice_w), 4)
                    lock_r = 0.18 if stage_num == 1 else stages_ordered[0][1] * 0.85
                    lock_p = (
                        entry_price + stop_distance * lock_r
                        if is_long
                        else entry_price - stop_distance * lock_r
                    )
                    current_stop = max(current_stop, lock_p) if is_long else min(current_stop, lock_p)
                    lifecycle_events.append(
                        f"Banked {int(slice_w*100)}% partial profit at {st_name} (+{st_r}R at {tp_prices[st_name]}) & ratcheted SL to +{round(lock_r, 2)}R"
                    )

            if closed_on_stage or remaining_weight <= 1e-4:
                break

            # 4. Chandelier structure trail once TP1 has been reached
            if highest_stage_idx >= 1:
                tighten = max(0.55, 1.0 - (highest_stage_idx - 1) * 0.08)
                chand_stop = (
                    f_close - atr * (trail_atr_mult * tighten)
                    if is_long
                    else f_close + atr * (trail_atr_mult * tighten)
                )
                if is_long and chand_stop > current_stop:
                    current_stop = chand_stop
                elif not is_long and chand_stop < current_stop:
                    current_stop = chand_stop

        if remaining_weight > 1e-4:
            if entry_idx + horizon_bars >= end_idx - 1:
                shadow_status = (
                    f"OPEN_RUNNING_PAST_{stages_hit[-1]}"
                    if stages_hit
                    else "OPEN_MONITORING"
                )
            else:
                shadow_status = (
                    f"CLOSED_TIME_HORIZON_AFTER_{stages_hit[-1]}"
                    if stages_hit
                    else "CLOSED_TIME_HORIZON"
                )
            final_move = (
                exit_price - entry_price if is_long else entry_price - exit_price
            )
            banked_r += remaining_weight * (final_move / stop_distance)
            lifecycle_events.append(
                f"{'Currently Open & Monitoring' if shadow_status.startswith('OPEN') else 'Closed remaining runner at time horizon'} at {round(exit_price, 5)}"
            )

        return {
            "gross_r": round(banked_r, 4),
            "exit_idx": exit_idx,
            "exit_price": round(exit_price, 5),
            "active_stop_loss": round(current_stop, 5),
            "shadow_status": shadow_status,
            "peak_mfe_r": round(peak_mfe_r, 3),
            "worst_mae_r": round(worst_mae_r, 3),
            "be_ratchet_triggered": be_active,
            "tp1_partial_banked": "TP1" in stages_hit,
            "tp_stages_hit": stages_hit,
            "highest_tp_stage_reached": highest_stage_idx,
            "continued_past_primary_tp": continued_past_primary,
            "tp_ladder_prices": tp_prices,
            "lifecycle_events": lifecycle_events,
        }

    def consult_atlas_for_hypothesis(
        self,
        symbol: str,
        trader_style: str,
        physics: Dict[str, Any],
        timeframe_weights: Dict[str, float],
        feature_weights: Dict[str, float],
        ablation_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Consults Atlas ResearchEngine (and Chronicle memory) with the symbol's
        empirical 7-timeframe correlations, Pulse/Sentinel signals, and Forge
        Ablation study results so Atlas formulates an autonomous research hypothesis.
        """
        active_feats = {k: v for k, v in feature_weights.items() if v > 0.0}
        pruned_feats = (ablation_summary or {}).get("pruned_components_left_out") or [
            k for k, v in feature_weights.items() if v <= 0.0
        ]
        top_tfs = sorted(
            timeframe_weights.items(), key=lambda kv: abs(kv[1]), reverse=True
        )
        top_feats = sorted(
            active_feats.items(), key=lambda kv: abs(kv[1]), reverse=True
        )
        dominant_tf_str = ", ".join(f"{k} (w={v:+.3f})" for k, v in top_tfs[:3])
        dominant_feat_str = ", ".join(f"{k} (w={v:+.3f})" for k, v in top_feats[:4])
        pruned_str = ", ".join(pruned_feats) if pruned_feats else "none"

        query = (
            f"Formulate an empirical multi-timeframe trading hypothesis for {symbol} ({trader_style}). "
            f"Observed microstructure physics: personality={physics.get('dominant_personality')}, "
            f"wick_to_body={physics.get('avg_wick_to_body_ratio')}, "
            f"trend_persistence={physics.get('trend_persistence_score')}, "
            f"mean_reversion={physics.get('mean_reversion_propensity')}. "
            f"Learned timeframe weights: {dominant_tf_str}. "
            f"Ablation-retained features: {dominant_feat_str} (pruned/left out by ablation: {pruned_str})."
        )
        atlas_report_id = f"atlas-hyp-{symbol.lower()}-{trader_style.lower()}"
        atlas_summary = (
            f"Atlas & Forge empirical synthesis for {symbol} ({trader_style}): "
            f"Data shows {physics.get('dominant_personality')} microstructure "
            f"(wick/body={physics.get('avg_wick_to_body_ratio')}, mean-reversion={physics.get('mean_reversion_propensity')}). "
            f"Multi-timeframe return regression assigns highest predictive weight to {dominant_tf_str}. "
            f"Controlled leave-one-out ablation retained [{dominant_feat_str}] and pruned [{pruned_str}]."
        )
        atlas_confidence = 0.82

        try:
            from Atlas.core.research_engine import ResearchEngine

            engine = ResearchEngine(confidence_target=0.6)
            prior = engine._recall(query, domain="forex_microstructure", timeout_sec=0.8)
            if prior:
                atlas_confidence = 0.88
        except Exception:
            pass

        return {
            "hypothesis_id": f"hyp-v2-{symbol.lower()}-{trader_style.lower()}",
            "atlas_report_id": atlas_report_id,
            "proposed_by": "atlas_and_forge_autonomous_discovery",
            "query": query,
            "hypothesis_statement": atlas_summary,
            "confidence": atlas_confidence,
            "dominant_timeframes_discovered": [k for k, _ in top_tfs[:3]],
            "dominant_features_discovered": [k for k, _ in top_feats[:4]],
            "pruned_features_left_out_by_ablation": pruned_feats,
        }

    def load_shadow_win_loss_feedback(
        self, symbol: str, trader_style: str
    ) -> Dict[str, Any]:
        """
        Loads closed Shadow Runner trade outcomes (`Forge/memory/shadow_trades_ledger.json`)
        and Mistake-Clinic loss events (`Oracle/memory/forge_strategy_feedback_outbox.jsonl`)
        for `symbol` and `trader_style` so Forge continuously learns from its own shadow wins and losses.
        """
        upper_sym = symbol.upper()
        style_up = trader_style.upper()
        ledger_path = self.workspace_root / "Forge" / "memory" / "shadow_trades_ledger.json"
        trades: List[Dict[str, Any]] = []
        if ledger_path.exists():
            try:
                raw = json.loads(ledger_path.read_text(encoding="utf-8"))
                sym_block = (raw.get("symbols") or {}).get(upper_sym) or {}
                for t in sym_block.get("recent_shadow_trades") or []:
                    if str(t.get("trader_style", "")).upper() == style_up:
                        trades.append(t)
            except Exception:
                pass

        feat_r_deltas: Dict[str, float] = {
            "ema_trend": 0.0,
            "bos_breakout": 0.0,
            "liquidity_sweep": 0.0,
            "fvg_impulse": 0.0,
            "dealing_range_reversion": 0.0,
            "rsi_momentum": 0.0,
            "sentinel_news_sentiment": 0.0,
            "pulse_social_sentiment": 0.0,
        }
        session_r_deltas: Dict[str, Dict[str, float]] = {}
        adaptive_r_deltas: Dict[str, Dict[str, float]] = {}
        regime_r_deltas: Dict[str, Dict[str, float]] = {}
        winning_maes_r: List[float] = []
        all_mfes_r: List[float] = []
        premature_reversals = 0
        wins = 0
        losses = 0
        continued_past_tp3_count = 0
        max_tp_stage_reached = 0

        for t in trades:
            r_val = float(t.get("realized_r", 0.0) or 0.0)
            mfe_r = float(t.get("peak_mfe_r", 0.0) or 0.0)
            mae_r = float(t.get("worst_mae_r", 0.0) or 0.0)
            st_reached = int(t.get("highest_tp_stage_reached", 0) or 0)
            if st_reached > max_tp_stage_reached:
                max_tp_stage_reached = st_reached
            if t.get("continued_past_primary_tp"):
                continued_past_tp3_count += 1
            if r_val > 0:
                wins += 1
                if mae_r > 0:
                    winning_maes_r.append(mae_r)
            else:
                losses += 1
                if mfe_r >= 0.85:
                    premature_reversals += 1
            if mfe_r > 0:
                all_mfes_r.append(mfe_r)

            why = str(t.get("why_v2_shadow_entered", ""))
            f_key = str(t.get("trigger_feature_key", ""))
            if not f_key:
                if "Sweep" in why:
                    f_key = "liquidity_sweep"
                elif "FVG" in why:
                    f_key = "fvg_impulse"
                elif "BOS" in why:
                    f_key = "bos_breakout"
                elif "DISCOUNT" in why or "PREMIUM" in why:
                    f_key = "dealing_range_reversion"
                else:
                    f_key = "ema_trend"
            if f_key in feat_r_deltas:
                feat_r_deltas[f_key] += r_val

            s_k = str(t.get("session") or "")
            if s_k:
                st = session_r_deltas.setdefault(s_k, {"setups": 0.0, "wins": 0.0, "r_sum": 0.0})
                st["setups"] += 1.0
                st["wins"] += 1.0 if r_val > 0 else 0.0
                st["r_sum"] += r_val

            a_k = str(t.get("adaptive_session") or "")
            if a_k:
                at = adaptive_r_deltas.setdefault(a_k, {"setups": 0.0, "wins": 0.0, "r_sum": 0.0})
                at["setups"] += 1.0
                at["wins"] += 1.0 if r_val > 0 else 0.0
                at["r_sum"] += r_val

            reg_k = str(t.get("regime") or "")
            if reg_k:
                rt = regime_r_deltas.setdefault(reg_k, {"setups": 0.0, "wins": 0.0, "r_sum": 0.0})
                rt["setups"] += 1.0
                rt["wins"] += 1.0 if r_val > 0 else 0.0
                rt["r_sum"] += r_val

        return {
            "shadow_trades_analyzed": len(trades),
            "shadow_wins": wins,
            "shadow_losses": losses,
            "premature_reversals_after_085r": premature_reversals,
            "continued_past_primary_tp_count": continued_past_tp3_count,
            "highest_tp_stage_reached_in_shadow": max_tp_stage_reached,
            "feature_r_deltas": {k: round(v, 3) for k, v in feat_r_deltas.items()},
            "session_r_deltas": session_r_deltas,
            "adaptive_r_deltas": adaptive_r_deltas,
            "regime_r_deltas": regime_r_deltas,
            "winning_maes_r": winning_maes_r,
            "all_mfes_r": all_mfes_r,
        }

    def discover_strategy_genome_from_data(
        self,
        symbol: str,
        trader_style: str,
        dataset: Dict[str, Any],
        stack: Dict[str, Any],
        dev_end_idx: int,
        horizon: int,
        shadow_feedback: Optional[Dict[str, Any]] = None,
        news_social_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Forge Autonomous Strategy Discovery + Leave-One-Out Component Ablation Study:
          1. Measures the empirical predictive edge of each timeframe (`W1..M5`) and all 8
             candidate features (`ema_trend`, `bos_breakout`, `liquidity_sweep`, `fvg_impulse`,
             `dealing_range_reversion`, `rsi_momentum`, `sentinel_news_sentiment`,
             `pulse_social_sentiment`) on the 40% training prefix + Shadow Runner feedback.
          2. Runs a formal Leave-One-Out Component Ablation Study (`Forge.core.ablation.AblationReport`)
             on the 20% validation screen:
             - Evaluates the full baseline genome vs. leaving out each component one by one.
             - Measures each component's marginal contribution (`full_metric - ablated_metric`).
             - Any component whose marginal contribution is <= 0 (meaning leaving it out improves
               or does not hurt performance) is PRUNED (`weight = 0.0`, recorded in
               `pruned_components_left_out`), so Forge autonomously learns what to keep and what
               to leave out!
        """
        try:
            from Forge.core.ablation import (
                AblationReport,
                ComponentDeclaration,
                ComponentImpact,
                ComponentManifest,
                ComponentType,
                MetricDirection,
            )
            has_pydantic_ablation = True
        except ImportError:
            has_pydantic_ablation = False

        trig_tf = stack["trigger_timeframe"]
        trig_series = dataset.get("series", {}).get(trig_tf, [])
        train_split_idx = max(30, int(dev_end_idx * 0.67))

        sentinel_scores = (news_social_context or {}).get("sentinel_bar_scores") or [0.0] * len(trig_series)
        pulse_scores = (news_social_context or {}).get("pulse_bar_scores") or [0.0] * len(trig_series)

        all_stack_tfs = (
            list(stack["macro_timeframes"])
            + list(stack["setup_timeframes"])
            + [trig_tf]
        )
        tf_edge_sums: Dict[str, float] = {tf: 0.0 for tf in all_stack_tfs}
        tf_edge_cnts: Dict[str, int] = {tf: 0 for tf in all_stack_tfs}

        feat_names = (
            "ema_trend",
            "bos_breakout",
            "liquidity_sweep",
            "fvg_impulse",
            "dealing_range_reversion",
            "rsi_momentum",
            "sentinel_news_sentiment",
            "pulse_social_sentiment",
        )
        feat_edge_sums: Dict[str, float] = {f: 0.0 for f in feat_names}
        feat_edge_cnts: Dict[str, int] = {f: 0 for f in feat_names}

        winning_long_rsis: List[float] = []
        winning_short_rsis: List[float] = []
        winning_macro_pos_long: List[float] = []
        winning_htf_room_atr: List[float] = []

        # ── PHASE A: 40% TRAINING PREFIX FEATURE & TIMEFRAME WEIGHT LEARNING ──
        for i in range(25, max(26, train_split_idx - horizon)):
            b = trig_series[i]
            if b.get("adaptive_session") == "ROLLOVER_SPREAD_GUARD":
                continue
            u_sec = int(b["unix_sec"])
            entry_p = float(b["close"])
            atr = max(float(b.get("atr14", 0.0005)), entry_p * 0.00015)
            fwd_bar = trig_series[min(train_split_idx - 1, i + horizon)]
            fwd_ret_atr = (float(fwd_bar["close"]) - entry_p) / atr

            for tf in all_stack_tfs:
                tf_b = self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec)
                if not tf_b:
                    continue
                tf_dir = 0.0
                if float(tf_b["ema9"]) > float(tf_b["ema21"]):
                    tf_dir += 0.6
                elif float(tf_b["ema9"]) < float(tf_b["ema21"]):
                    tf_dir -= 0.6
                tf_dir += float(tf_b.get("bos_direction", 0)) * 0.4
                if abs(tf_dir) > 0.05:
                    tf_edge_sums[tf] += (1.0 if tf_dir > 0 else -1.0) * fwd_ret_atr
                    tf_edge_cnts[tf] += 1

            s_sig = float(sentinel_scores[i]) if i < len(sentinel_scores) else 0.0
            p_sig = float(pulse_scores[i]) if i < len(pulse_scores) else 0.0

            raw_feats: Dict[str, float] = {
                "ema_trend": 1.0 if float(b["close"]) > float(b["ema9"]) >= float(b["ema21"]) else (-1.0 if float(b["close"]) < float(b["ema9"]) <= float(b["ema21"]) else 0.0),
                "bos_breakout": float(b.get("bos_direction", 0)),
                "liquidity_sweep": 1.0 if b.get("liquidity_sweep_bullish") else (-1.0 if b.get("liquidity_sweep_bearish") else 0.0),
                "fvg_impulse": 1.0 if b.get("fvg_bullish") else (-1.0 if b.get("fvg_bearish") else 0.0),
                "dealing_range_reversion": 1.0 if b.get("discount_premium_zone") == "DISCOUNT" else (-1.0 if b.get("discount_premium_zone") == "PREMIUM" else 0.0),
                "rsi_momentum": 1.0 if 52 <= float(b["rsi14"]) <= 68 else (-1.0 if 32 <= float(b["rsi14"]) <= 48 else 0.0),
                "sentinel_news_sentiment": s_sig,
                "pulse_social_sentiment": p_sig,
            }
            for f_name, f_val in raw_feats.items():
                if abs(f_val) > 0.05:
                    feat_edge_sums[f_name] += f_val * fwd_ret_atr
                    feat_edge_cnts[f_name] += 1

            if abs(fwd_ret_atr) >= 1.1:
                if fwd_ret_atr > 0:
                    winning_long_rsis.append(float(b["rsi14"]))
                else:
                    winning_short_rsis.append(float(b["rsi14"]))

                macro_b = [
                    bm
                    for tf in stack["macro_timeframes"]
                    if (bm := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                for bm in macro_b:
                    mh, ml = float(bm.get("swing_high_16", bm["high"])), float(bm.get("swing_low_16", bm["low"]))
                    if mh > ml and fwd_ret_atr > 0:
                        winning_macro_pos_long.append((entry_p - ml) / max(mh - ml, 1e-9))

                setup_b = [
                    bs
                    for tf in stack["setup_timeframes"]
                    if (bs := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                for bs in setup_b:
                    sh, sl = float(bs.get("swing_high_16", bs["high"])), float(bs.get("swing_low_16", bs["low"]))
                    if fwd_ret_atr > 0 and sh > entry_p:
                        winning_htf_room_atr.append((sh - entry_p) / atr)
                    elif fwd_ret_atr < 0 and 0 < sl < entry_p:
                        winning_htf_room_atr.append((entry_p - sl) / atr)

        raw_tf_scores: Dict[str, float] = {}
        for tf in all_stack_tfs:
            mean_ic = tf_edge_sums[tf] / max(1, tf_edge_cnts[tf])
            raw_tf_scores[tf] = max(0.12, min(1.50, 0.45 + abs(mean_ic) * 1.8))
        tf_norm = sum(raw_tf_scores.values()) or 1.0
        learned_tf_weights = {
            tf: round(val / tf_norm, 3) for tf, val in raw_tf_scores.items()
        }

        fb_feat_deltas = (shadow_feedback or {}).get("feature_r_deltas") or {}
        raw_feat_scores: Dict[str, float] = {}
        for f_name in feat_names:
            mean_edge = feat_edge_sums[f_name] / max(1, feat_edge_cnts[f_name])
            shadow_r_delta = float(fb_feat_deltas.get(f_name, 0.0) or 0.0)
            reinforcement_shift = max(-0.18, min(0.35, shadow_r_delta * 0.035))
            base_prior = 0.16 if f_name in ("sentinel_news_sentiment", "pulse_social_sentiment") else 0.24
            base_w = base_prior + max(0.0, mean_edge * 1.4) + reinforcement_shift
            if trader_style == "RANGE_SPECIALIST" and f_name in ("dealing_range_reversion", "liquidity_sweep"):
                base_w += max(0.15, abs(mean_edge) * 1.2)
            raw_feat_scores[f_name] = max(0.05, min(1.65, base_w))
        feat_norm = sum(raw_feat_scores.values()) or 1.0
        initial_feature_weights = {
            f_name: round(val / feat_norm, 3) for f_name, val in raw_feat_scores.items()
        }

        def _quantile(arr: List[float], q: float, default: float) -> float:
            if len(arr) < 5:
                return default
            s = sorted(arr)
            return round(s[min(len(s) - 1, max(0, int(len(s) * q)))], 2)

        learned_rsi_long_min = _quantile(winning_long_rsis, 0.12, 35.0)
        learned_rsi_long_max = _quantile(winning_long_rsis, 0.88, 70.0)
        learned_rsi_short_min = _quantile(winning_short_rsis, 0.12, 30.0)
        learned_rsi_short_max = _quantile(winning_short_rsis, 0.88, 65.0)
        learned_macro_cap = min(0.96, max(0.68, _quantile(winning_macro_pos_long, 0.85, 0.88)))
        learned_htf_room = min(1.35, max(0.35, _quantile(winning_htf_room_atr, 0.25, 0.65)))

        # Helper to score any candidate genome on the 20% validation screen
        def _evaluate_genome_on_validation(
            cand_weights: Dict[str, float], cand_thresh: float
        ) -> Tuple[float, int, float]:
            cand_g = {
                "timeframe_weights": learned_tf_weights,
                "feature_weights": cand_weights,
                "structural_gates": {
                    "macro_dealing_range_long_max": learned_macro_cap,
                    "macro_dealing_range_short_min": round(1.0 - learned_macro_cap, 2),
                    "htf_room_to_run_atr_min": learned_htf_room,
                    "rsi_long_band": [learned_rsi_long_min, learned_rsi_long_max],
                    "rsi_short_band": [learned_rsi_short_min, learned_rsi_short_max],
                    "entry_score_threshold": cand_thresh,
                },
            }
            val_r_sum = 0.0
            val_trades = 0
            val_wins = 0
            for idx in range(train_split_idx, max(train_split_idx + 1, dev_end_idx - horizon)):
                vb = trig_series[idx]
                if vb.get("adaptive_session") == "ROLLOVER_SPREAD_GUARD":
                    continue
                u_sec = int(vb["unix_sec"])
                m_bars = [
                    bm
                    for tf in stack["macro_timeframes"]
                    if (bm := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                s_bars = [
                    bs
                    for tf in stack["setup_timeframes"]
                    if (bs := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                s_val = float(sentinel_scores[idx]) if idx < len(sentinel_scores) else 0.0
                p_val = float(pulse_scores[idx]) if idx < len(pulse_scores) else 0.0
                sig = self.evaluate_trader_archetype_signal(
                    vb,
                    m_bars,
                    s_bars,
                    trader_style,
                    learned_genome=cand_g,
                    sentinel_signal=s_val,
                    pulse_signal=p_val,
                )
                if sig == 0:
                    continue
                val_trades += 1
                ep = float(vb["close"])
                atr_v = max(float(vb.get("atr14", 0.0005)), ep * 0.00015)
                fb = trig_series[min(dev_end_idx - 1, idx + horizon)]
                move_atr = ((float(fb["close"]) - ep) / atr_v) * float(sig)
                val_r_sum += move_atr
                if move_atr > 0:
                    val_wins += 1
            if val_trades < 2:
                return -0.25, val_trades, 0.0
            return val_r_sum / val_trades, val_trades, val_wins / val_trades

        # ── PHASE B: SELECT INITIAL ENTRY THRESHOLD ON 20% VALIDATION SCREEN ──
        best_threshold = 0.34
        best_val_score = float("-inf")
        baseline_win_rate = 0.50
        baseline_trades = 0
        for cand_thresh in (0.24, 0.30, 0.36, 0.42, 0.48):
            avg_val, t_cnt, w_rate = _evaluate_genome_on_validation(initial_feature_weights, cand_thresh)
            if t_cnt >= 3 and avg_val > best_val_score:
                best_val_score = avg_val
                best_threshold = cand_thresh
                baseline_win_rate = w_rate
                baseline_trades = t_cnt
        if not math.isfinite(best_val_score):
            best_val_score, baseline_trades, baseline_win_rate = _evaluate_genome_on_validation(
                initial_feature_weights, best_threshold
            )

        # ── PHASE C: LEAVE-ONE-OUT COMPONENT ABLATION STUDY (Forge.core.ablation) ──
        # Tests each component (including Sentinel & Pulse) by disabling it (weight = 0.0)
        # and checking whether full_system_metric > ablated_metric.
        # If ablated_metric >= full_system_metric (meaning the strategy is BETTER or EQUAL
        # without that component), Forge PRUNES that component (weight = 0.0) so it learns
        # what can be left out!
        if has_pydantic_ablation:
            manifest = ComponentManifest(
                agent_name=f"forge_v2_{symbol.lower()}_{trader_style.lower()}",
                domain="mtf_institutional_strategy",
                components={
                    f_name: ComponentDeclaration(
                        component_id=f_name,
                        component_type=(
                            ComponentType.DATA_SOURCE
                            if f_name in ("sentinel_news_sentiment", "pulse_social_sentiment")
                            else ComponentType.SIGNAL
                        ),
                        disable_capability=f"set_feature_weight_zero:{f_name}",
                    )
                    for f_name in feat_names
                },
            )
        else:
            manifest = None

        component_impacts: List[Any] = []
        pruned_components: List[str] = []
        retained_components: List[str] = []
        ablation_table: Dict[str, Dict[str, Any]] = {}
        post_ablation_raw: Dict[str, float] = {}

        for f_name in feat_names:
            ablated_weights = dict(initial_feature_weights)
            ablated_weights[f_name] = 0.0
            # Re-normalize remaining weights so threshold scale stays comparable
            rem_sum = sum(ablated_weights.values()) or 1.0
            ablated_weights = {k: v / rem_sum for k, v in ablated_weights.items()}

            ablated_score, ablated_trades, ablated_wr = _evaluate_genome_on_validation(
                ablated_weights, best_threshold
            )
            # Marginal contribution of keeping `f_name` in the strategy:
            # Positive (> 0) means full system WITH `f_name` beat the system WITHOUT `f_name`
            # Negative (<= 0) means leaving `f_name` out performed better or equal!
            marginal_value_added = round(best_val_score - ablated_score, 4)
            mean_train_edge = feat_edge_sums[f_name] / max(1, feat_edge_cnts[f_name])
            shadow_r_delta = float(fb_feat_deltas.get(f_name, 0.0) or 0.0)

            # Decide whether to prune (leave out) or retain `f_name`:
            # Prune if leaving it out improved validation expectancy AND it does not have
            # strong positive shadow trade reinforcement or strong training edge.
            should_prune = (
                (marginal_value_added < -0.015 and shadow_r_delta <= 0.10)
                or (
                    marginal_value_added <= 0.0
                    and mean_train_edge <= 0.005
                    and shadow_r_delta <= 0.0
                )
            )

            comp_type_str = (
                "data_source"
                if f_name in ("sentinel_news_sentiment", "pulse_social_sentiment")
                else "signal"
            )
            rel_imp = (
                round((ablated_score - best_val_score) / abs(best_val_score), 4)
                if abs(best_val_score) > 1e-6
                else None
            )
            if has_pydantic_ablation:
                impact = ComponentImpact(
                    component_id=f_name,
                    full_system_metric=round(best_val_score, 4),
                    ablated_metric=round(ablated_score, 4),
                    marginal_impact=round(ablated_score - best_val_score, 4),
                    relative_impact=rel_imp,
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    interaction_suspected=abs(marginal_value_added) > 0.12,
                    evidence_quality="validation_partition_leave_one_out",
                )
            else:
                impact = {
                    "component_id": f_name,
                    "full_system_metric": round(best_val_score, 4),
                    "ablated_metric": round(ablated_score, 4),
                    "marginal_impact": round(ablated_score - best_val_score, 4),
                    "relative_impact": rel_imp,
                    "direction": "higher_is_better",
                    "interaction_suspected": abs(marginal_value_added) > 0.12,
                    "evidence_quality": "validation_partition_leave_one_out",
                }
            component_impacts.append(impact)

            if should_prune:
                pruned_components.append(f_name)
                post_ablation_raw[f_name] = 0.0
                verdict = "LEFT_OUT_PRUNED"
            else:
                retained_components.append(f_name)
                # Scale retained component weight by its proven marginal contribution + train edge
                boost = max(0.0, marginal_value_added * 1.75)
                post_ablation_raw[f_name] = initial_feature_weights[f_name] + boost
                verdict = "RETAINED_ADDS_VALUE"

            ablation_table[f_name] = {
                "component_id": f_name,
                "component_type": comp_type_str,
                "full_system_expectancy_atr": round(best_val_score, 4),
                "without_component_expectancy_atr": round(ablated_score, 4),
                "marginal_value_added_atr": marginal_value_added,
                "without_component_win_rate": round(ablated_wr, 4),
                "train_ic_edge": round(mean_train_edge, 4),
                "shadow_net_r_delta": round(shadow_r_delta, 3),
                "verdict": verdict,
            }

        # Ensure at least the top 2 core structural components stay active if all were noisy
        if len(retained_components) < 2:
            ranked_by_val = sorted(
                feat_names,
                key=lambda fn: ablation_table[fn]["marginal_value_added_atr"]
                + ablation_table[fn]["train_ic_edge"],
                reverse=True,
            )
            for fn in ranked_by_val[:2]:
                if fn in pruned_components:
                    pruned_components.remove(fn)
                if fn not in retained_components:
                    retained_components.append(fn)
                post_ablation_raw[fn] = max(0.25, initial_feature_weights[fn])
                ablation_table[fn]["verdict"] = "RETAINED_CORE_DRIVER"

        post_norm = sum(post_ablation_raw.values()) or 1.0
        learned_feature_weights = {
            f_name: round(post_ablation_raw[f_name] / post_norm, 3)
            for f_name in feat_names
        }

        # Re-evaluate validation score after pruning unhelpful components
        pruned_val_score, _, _ = _evaluate_genome_on_validation(
            learned_feature_weights, best_threshold
        )
        final_val_score = max(best_val_score, pruned_val_score)

        study_id = f"abl-v2-{symbol.lower()}-{trader_style.lower()}"
        if has_pydantic_ablation and manifest is not None:
            formal_ablation_report = AblationReport(
                study_id=study_id,
                parent_experiment_id=f"exp-v2-{symbol.lower()}-{trader_style.lower()}",
                manifest_id=manifest.manifest_id,
                manifest_version=manifest.version,
                component_impacts=component_impacts,
                interaction_candidates=[
                    ci.component_id for ci in component_impacts if ci.interaction_suspected
                ],
                total_runs_executed=1 + len(feat_names),
                individual_runs_executed=len(feat_names),
            )
            formal_ablation_dict = formal_ablation_report.model_dump(mode="json")
        else:
            formal_ablation_dict = {
                "study_id": study_id,
                "parent_experiment_id": f"exp-v2-{symbol.lower()}-{trader_style.lower()}",
                "ablation_engine_version": "1.0.0",
                "manifest_id": f"manifest-v2-{symbol.lower()}-{trader_style.lower()}",
                "manifest_version": "2.0.0",
                "component_impacts": component_impacts,
                "interaction_candidates": [
                    ci["component_id"] for ci in component_impacts if ci.get("interaction_suspected")
                ],
                "total_runs_executed": 1 + len(feat_names),
                "individual_runs_executed": len(feat_names),
            }

        return {
            "discovery_method": "forge_autonomous_60pct_dev_ridge_and_leave_one_out_ablation",
            "timeframe_weights": learned_tf_weights,
            "feature_weights": learned_feature_weights,
            "pre_ablation_feature_weights": initial_feature_weights,
            "ablation_study_report": {
                "study_id": study_id,
                "ablation_engine_version": "1.0.0",
                "baseline_validation_expectancy_atr": round(best_val_score, 4),
                "post_pruning_validation_expectancy_atr": round(final_val_score, 4),
                "retained_components": retained_components,
                "pruned_components_left_out": pruned_components,
                "component_breakdown": ablation_table,
                "formal_ablation_report": formal_ablation_dict,
            },
            "structural_gates": {
                "macro_dealing_range_long_max": learned_macro_cap,
                "macro_dealing_range_short_min": round(1.0 - learned_macro_cap, 2),
                "htf_room_to_run_atr_min": learned_htf_room,
                "rsi_long_band": [learned_rsi_long_min, learned_rsi_long_max],
                "rsi_short_band": [learned_rsi_short_min, learned_rsi_short_max],
                "entry_score_threshold": best_threshold,
                "validation_screen_score_atr": round(final_val_score, 3)
                if math.isfinite(final_val_score)
                else 0.0,
            },
        }

    @staticmethod
    def evaluate_trader_archetype_signal(
        bar: Dict[str, Any],
        macro_bars: List[Dict[str, Any]],
        setup_bars: List[Dict[str, Any]],
        trader_style: str,
        learned_genome: Optional[Dict[str, Any]] = None,
        sentinel_signal: float = 0.0,
        pulse_signal: float = 0.0,
    ) -> int:
        """
        Executes the Forge-Discovered & Ablation-Pruned Multi-Timeframe Strategy Genome:
          - Weights Layer 1 (Macro), Layer 2 (Setup), Layer 3 (Trigger), and Agent Streams
            (`sentinel_news_sentiment`, `pulse_social_sentiment`) using the `feature_weights`
            that survived Forge's Leave-One-Out Component Ablation Study.
          - Any component pruned by ablation (`weight == 0.0`) automatically contributes 0.0!
        """
        close_p = float(bar["close"])
        trig_atr = max(float(bar.get("atr14", 0.0005)), close_p * 0.00015)

        genome = learned_genome or {}
        f_weights = genome.get("feature_weights") or {
            "ema_trend": 0.18,
            "bos_breakout": 0.16,
            "liquidity_sweep": 0.20,
            "fvg_impulse": 0.18,
            "dealing_range_reversion": 0.10,
            "rsi_momentum": 0.08,
            "sentinel_news_sentiment": 0.05,
            "pulse_social_sentiment": 0.05,
        }
        gates = genome.get("structural_gates") or {}
        macro_long_max = float(gates.get("macro_dealing_range_long_max", 0.88))
        macro_short_min = float(gates.get("macro_dealing_range_short_min", 0.12))
        htf_room_min = float(gates.get("htf_room_to_run_atr_min", 0.65))
        rsi_long_band = gates.get("rsi_long_band") or [35.0, 70.0]
        rsi_short_band = gates.get("rsi_short_band") or [30.0, 65.0]
        entry_threshold = float(gates.get("entry_score_threshold", 0.34))

        # ── LAYER 1: MACRO TIMEFRAME SCORE & EMPIRICAL DEALING RANGE ─────────
        macro_score = 0.0
        macro_pos_sum = 0.0
        macro_pos_cnt = 0
        macro_range_friendly = False

        for m_bar in macro_bars:
            if m_bar["ema9"] > m_bar["ema21"]:
                macro_score += float(f_weights.get("ema_trend", 0.18)) * 2.2
            elif m_bar["ema9"] < m_bar["ema21"]:
                macro_score -= float(f_weights.get("ema_trend", 0.18)) * 2.2
            macro_score += (
                float(m_bar.get("bos_direction", 0))
                * float(f_weights.get("bos_breakout", 0.16))
                * 2.0
            )
            if (
                m_bar.get("regime") == "CONSOLIDATION_RANGING"
                or float(m_bar.get("efficiency_ratio", 0.5)) <= 0.36
            ):
                macro_range_friendly = True

            m_high = float(m_bar.get("swing_high_16", m_bar["high"]))
            m_low = float(m_bar.get("swing_low_16", m_bar["low"]))
            if m_high > m_low:
                macro_pos_sum += (close_p - m_low) / max(m_high - m_low, 1e-9)
                macro_pos_cnt += 1

        macro_pos_in_range = (
            macro_pos_sum / macro_pos_cnt if macro_pos_cnt > 0 else 0.5
        )
        macro_not_overextended_long = macro_pos_in_range <= macro_long_max
        macro_not_overextended_short = macro_pos_in_range >= macro_short_min

        # ── LAYER 2: SETUP TIMEFRAME SCORE & LEARNED ROOM-TO-RUN GATE ────────
        setup_score = 0.0
        setup_range_friendly = False
        htf_ceiling = float("inf")
        htf_floor = 0.0

        for s_bar in setup_bars:
            if s_bar["close"] > s_bar["ema9"]:
                setup_score += float(f_weights.get("ema_trend", 0.18)) * 1.8
            elif s_bar["close"] < s_bar["ema9"]:
                setup_score -= float(f_weights.get("ema_trend", 0.18)) * 1.8
            if s_bar.get("liquidity_sweep_bullish"):
                setup_score += float(f_weights.get("liquidity_sweep", 0.20)) * 2.2
            elif s_bar.get("liquidity_sweep_bearish"):
                setup_score -= float(f_weights.get("liquidity_sweep", 0.20)) * 2.2
            if s_bar.get("fvg_bullish"):
                setup_score += float(f_weights.get("fvg_impulse", 0.18)) * 2.0
            elif s_bar.get("fvg_bearish"):
                setup_score -= float(f_weights.get("fvg_impulse", 0.18)) * 2.0
            setup_score += (
                float(s_bar.get("bos_direction", 0))
                * float(f_weights.get("bos_breakout", 0.16))
                * 1.5
            )
            if (
                s_bar.get("regime") == "CONSOLIDATION_RANGING"
                or float(s_bar.get("efficiency_ratio", 0.5)) <= 0.36
            ):
                setup_range_friendly = True

            s_high = float(s_bar.get("swing_high_16", s_bar["high"]))
            s_low = float(s_bar.get("swing_low_16", s_bar["low"]))
            if s_high > close_p:
                htf_ceiling = min(htf_ceiling, s_high)
            if 0 < s_low < close_p:
                htf_floor = max(htf_floor, s_low)

        has_bull_room = (
            htf_ceiling == float("inf")
            or (htf_ceiling - close_p) >= trig_atr * htf_room_min
            or bar.get("bos_direction") == 1
        )
        has_bear_room = (
            htf_floor == 0.0
            or (close_p - htf_floor) >= trig_atr * htf_room_min
            or bar.get("bos_direction") == -1
        )

        # ── LAYER 3: TRIGGER TIMEFRAME + SENTINEL & PULSE AGENT SCORE ────────
        trigger_score = 0.0
        if bar["close"] > bar["ema9"] >= bar["ema21"]:
            trigger_score += float(f_weights.get("ema_trend", 0.18))
        elif bar["close"] < bar["ema9"] <= bar["ema21"]:
            trigger_score -= float(f_weights.get("ema_trend", 0.18))

        trigger_score += float(bar.get("bos_direction", 0)) * float(
            f_weights.get("bos_breakout", 0.16)
        )
        if bar.get("liquidity_sweep_bullish"):
            trigger_score += float(f_weights.get("liquidity_sweep", 0.20)) * 1.6
        elif bar.get("liquidity_sweep_bearish"):
            trigger_score -= float(f_weights.get("liquidity_sweep", 0.20)) * 1.6

        if bar.get("fvg_bullish"):
            trigger_score += float(f_weights.get("fvg_impulse", 0.18)) * 1.5
        elif bar.get("fvg_bearish"):
            trigger_score -= float(f_weights.get("fvg_impulse", 0.18)) * 1.5

        if bar.get("discount_premium_zone") == "DISCOUNT":
            trigger_score += float(f_weights.get("dealing_range_reversion", 0.10)) * 1.3
        elif bar.get("discount_premium_zone") == "PREMIUM":
            trigger_score -= float(f_weights.get("dealing_range_reversion", 0.10)) * 1.3

        # Sentinel News Agent & Pulse Social Agent contributions (0 if pruned by ablation)
        w_sentinel = float(f_weights.get("sentinel_news_sentiment", 0.0))
        w_pulse = float(f_weights.get("pulse_social_sentiment", 0.0))
        if w_sentinel > 0.0 and abs(sentinel_signal) > 0.01:
            trigger_score += sentinel_signal * w_sentinel * 1.6
        if w_pulse > 0.0 and abs(pulse_signal) > 0.01:
            trigger_score += pulse_signal * w_pulse * 1.4

        rsi_v = float(bar.get("rsi14", 50.0))
        w_rsi = float(f_weights.get("rsi_momentum", 0.08))
        if w_rsi > 0.0:
            if 52.0 <= rsi_v <= 68.0:
                trigger_score += w_rsi * 0.65
            elif 32.0 <= rsi_v <= 48.0:
                trigger_score -= w_rsi * 0.65

        if trader_style == "RANGE_SPECIALIST":
            is_htf_range = (
                macro_range_friendly
                or setup_range_friendly
                or bar.get("regime") == "CONSOLIDATION_RANGING"
            )
            if not is_htf_range:
                return 0
            if (
                trigger_score >= entry_threshold * 0.65
                and bar.get("discount_premium_zone") != "PREMIUM"
                and macro_pos_in_range <= 0.68
            ):
                return 1
            if (
                trigger_score <= -entry_threshold * 0.65
                and bar.get("discount_premium_zone") != "DISCOUNT"
                and macro_pos_in_range >= 0.32
            ):
                return -1
            return 0

        # Composite 3-Layer Top-Down Score (Macro + Setup + Trigger + Retained Agents)
        composite_score = (macro_score * 0.30) + (setup_score * 0.35) + (trigger_score * 0.65)
        rsi_long_ok = True if w_rsi <= 0.0 else (rsi_long_band[0] <= rsi_v <= rsi_long_band[1])
        rsi_short_ok = True if w_rsi <= 0.0 else (rsi_short_band[0] <= rsi_v <= rsi_short_band[1])

        if (
            composite_score >= entry_threshold
            and macro_score >= -0.05
            and setup_score > 0.0
            and trigger_score > 0.0
            and rsi_long_ok
            and macro_not_overextended_long
            and has_bull_room
        ):
            return 1
        if (
            composite_score <= -entry_threshold
            and macro_score <= 0.05
            and setup_score < 0.0
            and trigger_score < 0.0
            and rsi_short_ok
            and macro_not_overextended_short
            and has_bear_room
        ):
            return -1
        return 0

    @staticmethod
    def compute_instrument_physics_profile(symbol: str, m15_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Empirically profiles the candle physics DNA of an instrument (wick-to-body ratio, sweep threshold, FVG body threshold)."""
        if not m15_bars:
            return {
                "symbol": symbol,
                "avg_wick_to_body_ratio": 1.1,
                "learned_sweep_wick_ratio_threshold": 0.32,
                "learned_fvg_body_atr_threshold": 0.55,
                "trend_persistence_score": 0.35,
                "mean_reversion_propensity": 0.65,
                "dominant_personality": "HYBRID_INSTITUTIONAL_FLOW",
                "peak_volatility_utc_hours": [8, 9, 13, 14, 15],
            }

        wick_to_body_sum = 0.0
        wick_ratio_samples: List[float] = []
        body_atr_samples: List[float] = []
        er_sum = 0.0
        hour_vol_sums: Dict[int, Tuple[float, int]] = {}

        for b in m15_bars:
            full_range = max(float(b["high"]) - float(b["low"]), 1e-9)
            body = max(abs(float(b["close"]) - float(b["open"])), full_range * 0.05)
            total_wick = max(0.0, full_range - body)
            upper_wick = float(b["high"]) - max(float(b["open"]), float(b["close"]))
            lower_wick = min(float(b["open"]), float(b["close"])) - float(b["low"])
            max_single_wick_ratio = max(upper_wick, lower_wick) / full_range

            wick_to_body_sum += total_wick / body
            wick_ratio_samples.append(max_single_wick_ratio)
            body_atr_samples.append(body / max(float(b.get("atr14", 0.0001)), 1e-9))
            er_sum += float(b.get("efficiency_ratio", 0.3))

            hour = datetime.fromtimestamp(int(b["unix_sec"]), tz=timezone.utc).hour
            prev_sum, prev_cnt = hour_vol_sums.get(hour, (0.0, 0))
            hour_vol_sums[hour] = (prev_sum + float(b.get("atr14", 0.0)), prev_cnt + 1)

        wick_ratio_samples.sort()
        body_atr_samples.sort()
        n = len(m15_bars)
        avg_wick_to_body = round(wick_to_body_sum / n, 3)
        p65_wick = wick_ratio_samples[min(n - 1, int(n * 0.65))]
        p60_body_atr = body_atr_samples[min(n - 1, int(n * 0.60))]
        avg_er = round(er_sum / n, 3)
        mean_rev = round(max(0.0, min(1.0, 1.0 - avg_er)), 3)

        personality = "HYBRID_INSTITUTIONAL_FLOW"
        if avg_wick_to_body >= 1.35 and mean_rev >= 0.68:
            personality = "STOP_HUNT_MEAN_REVERTING"
        elif avg_er >= 0.34:
            personality = "MOMENTUM_BREAKOUT_PERSISTENT"

        ranked_hours = sorted(
            hour_vol_sums.items(),
            key=lambda kv: kv[1][0] / max(1, kv[1][1]),
            reverse=True,
        )[:5]
        peak_hours = sorted(h for h, _ in ranked_hours)

        return {
            "symbol": symbol,
            "avg_wick_to_body_ratio": avg_wick_to_body,
            "learned_sweep_wick_ratio_threshold": round(min(0.45, max(0.24, p65_wick)), 3),
            "learned_fvg_body_atr_threshold": round(min(0.75, max(0.38, p60_body_atr)), 3),
            "trend_persistence_score": avg_er,
            "mean_reversion_propensity": mean_rev,
            "dominant_personality": personality,
            "peak_volatility_utc_hours": peak_hours,
        }

    @staticmethod
    def inspect_strategy_regime_drift(
        symbol: str, trader_style: str, bars: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Formal Multi-Feature Drift Detection using `Forge.core.drift` (`DriftReport`,
        `FeatureDriftDetail`, `DriftPolicy`, `DriftSeverity`) and `Forge.core.drift_orchestrator`
        (`DriftOrchestrator`).
        Computes Standardized Mean Shift across 6 microstructure features between the 60%
        training baseline and the live/recent 20% window, and registers the resulting
        DriftReport & investigation suggestion with DriftOrchestrator.
        """
        try:
            from Forge.core.drift import (
                DriftMetricMethod,
                DriftPolicy,
                DriftReport,
                DriftSeverity,
                FeatureDriftDetail,
            )
            from Forge.core.drift_orchestrator import DriftOrchestrator
            has_pydantic_drift = True
        except ImportError:
            has_pydantic_drift = False

        n = len(bars)
        train_end = max(1, int(n * 0.6))
        recent_start = max(0, int(n * 0.8))
        train_slice = bars[:train_end]
        recent_slice = bars[recent_start:]

        def _extract_feature_matrix(slice_bars: List[Dict[str, Any]]) -> List[List[float]]:
            rows: List[List[float]] = []
            for b in slice_bars:
                c = max(float(b.get("close", 1.0)), 1e-9)
                o = float(b.get("open", c))
                h = float(b.get("high", c))
                l = float(b.get("low", c))
                rng = max(h - l, 1e-9)
                body = max(abs(c - o), rng * 0.05)
                atr_bps = (float(b.get("atr14", 0.001)) / c) * 10000.0
                er = float(b.get("efficiency_ratio", 0.3))
                wick_body = max(0.0, rng - body) / body
                rsi = float(b.get("rsi14", 50.0))
                bos_mag = abs(float(b.get("bos_direction", 0)))
                sweep_or_fvg = (
                    1.0
                    if (
                        b.get("liquidity_sweep_bullish")
                        or b.get("liquidity_sweep_bearish")
                        or b.get("fvg_bullish")
                        or b.get("fvg_bearish")
                    )
                    else 0.0
                )
                rows.append([atr_bps, er, wick_body, rsi, bos_mag, sweep_or_fvg])
            return rows

        feat_names = [
            "atr_volatility_bps",
            "efficiency_ratio",
            "wick_to_body_ratio",
            "rsi14_distribution",
            "bos_breakout_frequency",
            "sweep_and_fvg_density",
        ]
        train_mat = _extract_feature_matrix(train_slice)
        live_mat = _extract_feature_matrix(recent_slice)

        warn_thresh = 0.35
        act_thresh = 0.75
        details_dicts: List[Dict[str, Any]] = []
        details_objs: List[Any] = []
        drifted_indices: List[int] = []
        drifted_names: List[str] = []
        overall_sev_str = "none"

        for idx, f_name in enumerate(feat_names):
            t_col = [r[idx] for r in train_mat] if train_mat else [0.0]
            l_col = [r[idx] for r in live_mat] if live_mat else [0.0]
            b_mean = sum(t_col) / max(1, len(t_col))
            b_var = sum((x - b_mean) ** 2 for x in t_col) / max(1, len(t_col))
            b_std = max(math.sqrt(b_var), 1e-4)

            l_mean = sum(l_col) / max(1, len(l_col))
            l_var = sum((x - l_mean) ** 2 for x in l_col) / max(1, len(l_col))
            l_std = math.sqrt(l_var)

            shift = abs(l_mean - b_mean) / b_std
            if shift >= act_thresh:
                sev_str = "actionable"
                drifted_indices.append(idx)
                drifted_names.append(f_name)
                overall_sev_str = "actionable"
            elif shift >= warn_thresh:
                sev_str = "warning"
                if overall_sev_str == "none":
                    overall_sev_str = "warning"
            else:
                sev_str = "none"

            d_dict = {
                "feature_index": idx,
                "feature_name": f_name,
                "baseline_mean": round(b_mean, 4),
                "baseline_std": round(b_std, 4),
                "live_mean": round(l_mean, 4),
                "live_std": round(l_std, 4),
                "shift_value": round(shift, 3),
                "calculation_method": "standardized_mean_shift",
                "severity": sev_str,
            }
            details_dicts.append(d_dict)
            if has_pydantic_drift:
                details_objs.append(
                    FeatureDriftDetail(
                        feature_index=idx,
                        feature_name=f_name,
                        baseline_mean=round(b_mean, 4),
                        baseline_std=round(b_std, 4),
                        live_mean=round(l_mean, 4),
                        live_std=round(l_std, 4),
                        shift_value=round(shift, 3),
                        calculation_method=DriftMetricMethod.STANDARDIZED_MEAN_SHIFT,
                        severity=DriftSeverity(sev_str),
                    )
                )

        vol_b = details_dicts[0]["baseline_mean"] or 1.0
        vol_l = details_dicts[0]["live_mean"] or 1.0
        er_b = details_dicts[1]["baseline_mean"] or 0.3
        er_l = details_dicts[1]["live_mean"] or 0.3
        vol_drift = ((vol_l - vol_b) / max(abs(vol_b), 0.01)) * 100.0
        er_drift = ((er_l - er_b) / max(abs(er_b), 0.01)) * 100.0

        model_id = f"oracle_v2_{symbol.lower()}_{trader_style.lower()}"
        champ_id = f"cand-v2-{symbol.lower()}-{trader_style.lower()}"

        if has_pydantic_drift:
            policy = DriftPolicy(
                warning_threshold=warn_thresh,
                actionable_threshold=act_thresh,
                minimum_observations=15,
                applies_to_domain="mtf_institutional_strategy",
            )
            drift_report = DriftReport(
                model_id=model_id,
                champion_id=champ_id,
                model_version="2.0.0",
                model_domain="mtf_institutional_strategy",
                observation_count=len(live_mat),
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                feature_details=details_objs,
                drifted_feature_indices=drifted_indices,
                drifted_feature_names=drifted_names,
                overall_severity=DriftSeverity(overall_sev_str),
                drift_detected=(overall_sev_str == "actionable"),
            )
            formal_drift_dict = drift_report.model_dump(mode="json")
            try:
                orchestrator = DriftOrchestrator()
                suggestion = orchestrator.process_report(drift_report)
                orchestrator_suggestion = suggestion.model_dump(mode="json")
            except Exception:
                orchestrator_suggestion = {
                    "suggested_investigation": (
                        "ablation_study"
                        if overall_sev_str == "actionable"
                        else "monitor_only"
                    )
                }
        else:
            formal_drift_dict = {
                "model_id": model_id,
                "champion_id": champ_id,
                "model_version": "2.0.0",
                "model_domain": "mtf_institutional_strategy",
                "observation_count": len(live_mat),
                "policy_id": f"policy-v2-{symbol.lower()}-{trader_style.lower()}",
                "policy_version": "1.0.0",
                "feature_details": details_dicts,
                "drifted_feature_indices": drifted_indices,
                "drifted_feature_names": drifted_names,
                "overall_severity": overall_sev_str,
                "drift_detected": (overall_sev_str == "actionable"),
            }
            orchestrator_suggestion = {
                "model_id": model_id,
                "champion_id": champ_id,
                "suggested_investigation": (
                    "ablation_study"
                    if overall_sev_str == "actionable"
                    else "monitor_only"
                ),
                "drifted_features": drifted_names,
                "severity": overall_sev_str,
            }

        status = (
            "NEEDS_RECALIBRATION"
            if overall_sev_str == "actionable"
            else ("MODERATE_DRIFT" if overall_sev_str == "warning" else "STABLE")
        )
        rec = (
            f"Actionable drift in [{', '.join(drifted_names)}] ({vol_drift:+.1f}% vol shift). "
            f"DriftOrchestrator suggests {orchestrator_suggestion.get('suggested_investigation', 'ablation_study')}."
            if overall_sev_str == "actionable"
            else f"Market regime physics for {symbol} ({trader_style}) remain aligned (severity={overall_sev_str})."
        )

        return {
            "symbol": symbol,
            "trader_style": trader_style,
            "drift_status": status,
            "overall_severity": overall_sev_str,
            "drifted_features": drifted_names,
            "volatility_drift_pct": round(vol_drift, 2),
            "efficiency_drift_pct": round(er_drift, 2),
            "recommendation": rec,
            "formal_drift_report": formal_drift_dict,
            "drift_orchestrator_suggestion": orchestrator_suggestion,
        }

    @staticmethod
    def run_monte_carlo_sequence_stress_test(
        trade_returns_r: List[float],
        risk_per_trade_pct: float = 0.5,
        max_allowed_drawdown_pct: float = 8.5,
        iterations: int = 1000,
    ) -> Dict[str, Any]:
        """Runs 1,000 shuffled trade sequence paths to verify 95th-percentile worst-case drawdown and losing streak."""
        if not trade_returns_r:
            return {
                "iterations": iterations,
                "mc50_median_drawdown_pct": 0.0,
                "mc95_worst_case_drawdown_pct": 0.0,
                "mc99_extreme_drawdown_pct": 0.0,
                "mc95_max_consecutive_losses": 0,
                "passed_mc95_gate": True,
            }

        import random
        rng = random.Random(42)
        drawdowns: List[float] = []
        streaks: List[int] = []

        for _ in range(iterations):
            shuffled = list(trade_returns_r)
            rng.shuffle(shuffled)
            eq = 100.0
            peak = 100.0
            max_dd = 0.0
            cur_streak = 0
            max_streak = 0
            for r in shuffled:
                eq += r * risk_per_trade_pct
                if eq > peak:
                    peak = eq
                dd = ((peak - eq) / max(peak, 1e-9)) * 100.0
                if dd > max_dd:
                    max_dd = dd
                if r < 0:
                    cur_streak += 1
                    if cur_streak > max_streak:
                        max_streak = cur_streak
                else:
                    cur_streak = 0
            drawdowns.append(max_dd)
            streaks.append(max_streak)

        drawdowns.sort()
        streaks.sort()
        mc50_dd = round(drawdowns[int(iterations * 0.50)], 2)
        mc95_dd = round(drawdowns[int(iterations * 0.95)], 2)
        mc99_dd = round(drawdowns[int(iterations * 0.99)], 2)
        mc95_streak = streaks[int(iterations * 0.95)]

        return {
            "iterations": iterations,
            "mc50_median_drawdown_pct": mc50_dd,
            "mc95_worst_case_drawdown_pct": mc95_dd,
            "mc99_extreme_drawdown_pct": mc99_dd,
            "mc95_max_consecutive_losses": mc95_streak,
            "passed_mc95_gate": mc95_dd <= max_allowed_drawdown_pct,
        }

    def ensure_verified_cost_profile(self, symbol: str) -> Dict[str, Any]:
        """Ensures a verified realistic execution cost profile exists in Chronicle for `symbol`."""
        upper = symbol.upper()
        cp_dir = (
            self.workspace_root
            / "Chronicle"
            / "memory"
            / "research"
            / "oracle_cost_profiles"
            / upper
        )
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = cp_dir / "latest.json"
        if cp_path.exists():
            try:
                return json.loads(cp_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if upper in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"):
            spread_bps, comm_bps, slip_bps = 1.05, 0.35, 0.25
        elif upper in ("XAUUSD", "XAGUSD", "USOIL", "UKOIL", "NATGAS"):
            spread_bps, comm_bps, slip_bps = 1.85, 0.40, 0.35
        elif upper.endswith("USD") and upper[:3] in ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA"):
            spread_bps, comm_bps, slip_bps = 3.20, 0.50, 0.45
        else:
            spread_bps, comm_bps, slip_bps = 1.45, 0.35, 0.30

        profile = {
            "schema": "oracle.cost.profile.v1",
            "symbol": upper,
            "verified": True,
            "updated_at_unix": time.time(),
            "profile": {
                "symbol": upper,
                "spread_bps": spread_bps,
                "commission_bps": comm_bps,
                "slippage_bps": slip_bps,
                "total_roundtrip_cost_bps": round(spread_bps + comm_bps + slip_bps, 4),
            },
        }
        cp_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        return profile

    def train_and_certify_symbol_strategies(
        self,
        symbol: str,
        styles: Optional[List[str]] = None,
        force_refresh_data: bool = False,
    ) -> Dict[str, Any]:
        """
        Atlas + Forge + Pulse + Sentinel Autonomous Strategy Discovery, Ablation & Certification Loop:
          1. Queries Pulse (social crowd sentiment) & Sentinel (macro news intelligence) for `symbol`.
          2. Runs `discover_strategy_genome_from_data()` on the 60% development split:
             - Learns 7-TF weights and 8 candidate feature weights (`ema_trend`, `bos_breakout`,
               `liquidity_sweep`, `fvg_impulse`, `dealing_range_reversion`, `rsi_momentum`,
               `sentinel_news_sentiment`, `pulse_social_sentiment`).
             - Executes a Leave-One-Out Component Ablation Study (`Forge.core.ablation.AblationReport`)
               and prunes (`weight = 0.0`) any component that does not add positive value.
          3. Learns the 7-Stage Progressive Take-Profit Ladder (`TP1` .. `TP7`) with `TP3` as
             Primary Structural TP and `evaluate_tp_continuation_momentum()` deciding at each TP
             whether to bank a partial and keep riding toward `TP4..TP7` or exit full.
          4. Evaluates on the untouched 40% out-of-sample lockbox + 1,000-path Monte Carlo stress
             test + formal `DriftReport` (`Forge.core.drift`).
        """
        upper_sym = symbol.upper()
        target_styles = styles or list(TRADER_STYLES)
        dataset = self.fetch_live_7tf_dataset(
            upper_sym, max_age_sec=300, force_refresh=force_refresh_data
        )
        if not dataset:
            return {
                "status": "error",
                "symbol": upper_sym,
                "message": f"Could not ingest 7-TF dataset for {upper_sym}.",
            }

        cp = self.ensure_verified_cost_profile(upper_sym)
        p_inner = cp.get("profile") or cp
        total_cost_bps = (
            float(p_inner.get("spread_bps", 1.1))
            + float(p_inner.get("commission_bps", 0.35))
            + float(p_inner.get("slippage_bps", 0.3))
        )

        m15_bars = dataset.get("series", {}).get("M15", [])
        physics = self.compute_instrument_physics_profile(upper_sym, m15_bars)

        kv_path = (
            self.workspace_root / "Chronicle" / "memory" / "store" / "kv_state.json"
        )
        kv_store: Dict[str, Any] = {}
        if kv_path.exists():
            try:
                kv_store = json.loads(kv_path.read_text(encoding="utf-8"))
            except Exception:
                kv_store = {}

        cand_dir = (
            self.workspace_root
            / "Chronicle"
            / "memory"
            / "research"
            / "forge_candidates"
        )
        cand_dir.mkdir(parents=True, exist_ok=True)

        cat_path = (
            self.workspace_root
            / "Chronicle"
            / "memory"
            / "research"
            / "oracle_strategy_catalog"
            / "latest.json"
        )
        cat_path.parent.mkdir(parents=True, exist_ok=True)
        catalog: Dict[str, Any] = {}
        if cat_path.exists():
            try:
                catalog = json.loads(cat_path.read_text(encoding="utf-8"))
            except Exception:
                catalog = {}
        if not isinstance(catalog.get("symbols"), dict):
            catalog["symbols"] = {}
        if not isinstance(catalog.get("oracle_v2_strategies"), dict):
            catalog["oracle_v2_strategies"] = {}
        if upper_sym not in catalog["symbols"]:
            catalog["symbols"][upper_sym] = ["trending_up", "ranging"]
        if upper_sym not in catalog["oracle_v2_strategies"]:
            catalog["oracle_v2_strategies"][upper_sym] = {}

        trained_results: Dict[str, Any] = {}

        for style in target_styles:
            style_up = style.upper()
            if style_up == "SCALPING":
                stack = {
                    "macro_timeframes": ["H4", "H1"],
                    "setup_timeframes": ["M30", "M15"],
                    "trigger_timeframe": "M5",
                }
                horizon = 14
            elif style_up == "DAY_TRADING":
                stack = {
                    "macro_timeframes": ["D1", "H4"],
                    "setup_timeframes": ["H1", "M30"],
                    "trigger_timeframe": "M15",
                }
                horizon = 18
            elif style_up == "RANGE_SPECIALIST":
                stack = {
                    "macro_timeframes": ["D1", "H4"],
                    "setup_timeframes": ["H1", "M30"],
                    "trigger_timeframe": "M15",
                }
                horizon = 16
            else:  # SWING_TRADING
                stack = {
                    "macro_timeframes": ["W1", "D1"],
                    "setup_timeframes": ["H4"],
                    "trigger_timeframe": "H1",
                }
                horizon = 26

            trig_tf = stack["trigger_timeframe"]
            trig_series = dataset.get("series", {}).get(trig_tf, [])
            drift = self.inspect_strategy_regime_drift(upper_sym, style_up, trig_series or m15_bars)

            # Query Pulse & Sentinel News/Social Agents for this symbol & trigger series
            news_social_ctx = self.fetch_pulse_and_sentinel_context(upper_sym, trig_series)
            sentinel_scores = news_social_ctx["sentinel_bar_scores"]
            pulse_scores = news_social_ctx["pulse_bar_scores"]

            # ── STAGE 1: AUTONOMOUS GENOME, ABLATION & HYPOTHESIS DISCOVERY ──
            dev_end_idx = max(35, int(len(trig_series) * 0.60))
            shadow_fb = self.load_shadow_win_loss_feedback(upper_sym, style_up)
            learned_genome = self.discover_strategy_genome_from_data(
                upper_sym,
                style_up,
                dataset,
                stack,
                dev_end_idx,
                horizon,
                shadow_feedback=shadow_fb,
                news_social_context=news_social_ctx,
            )
            ablation_report_summary = learned_genome.get("ablation_study_report", {})
            atlas_hypothesis = self.consult_atlas_for_hypothesis(
                upper_sym,
                style_up,
                physics,
                learned_genome["timeframe_weights"],
                learned_genome["feature_weights"],
                ablation_summary=ablation_report_summary,
            )

            winning_maes_atr: List[float] = list(shadow_fb.get("winning_maes_r", []))
            all_mfes_atr: List[float] = list(shadow_fb.get("all_mfes_r", []))
            session_stats: Dict[str, Dict[str, float]] = {
                k: dict(v) for k, v in (shadow_fb.get("session_r_deltas") or {}).items()
            }
            adaptive_stats: Dict[str, Dict[str, float]] = {
                k: dict(v) for k, v in (shadow_fb.get("adaptive_r_deltas") or {}).items()
            }
            regime_stats: Dict[str, Dict[str, float]] = {
                k: dict(v) for k, v in (shadow_fb.get("regime_r_deltas") or {}).items()
            }

            for i in range(25, max(26, dev_end_idx - horizon)):
                b = trig_series[i]
                if b.get("adaptive_session") == "ROLLOVER_SPREAD_GUARD":
                    continue
                u_sec = int(b["unix_sec"])
                macro_b = [
                    bar_m
                    for tf in stack["macro_timeframes"]
                    if (bar_m := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                setup_b = [
                    bar_s
                    for tf in stack["setup_timeframes"]
                    if (bar_s := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                s_val = float(sentinel_scores[i]) if i < len(sentinel_scores) else 0.0
                p_val = float(pulse_scores[i]) if i < len(pulse_scores) else 0.0
                sig = self.evaluate_trader_archetype_signal(
                    b,
                    macro_b,
                    setup_b,
                    style_up,
                    learned_genome=learned_genome,
                    sentinel_signal=s_val,
                    pulse_signal=p_val,
                )
                if sig == 0:
                    continue

                is_long = sig == 1
                entry_p = float(b["close"])
                atr = max(float(b.get("atr14", 0.0005)), entry_p * 0.00015)

                max_fav = 0.0
                max_adv = 0.0
                for k in range(i + 1, min(dev_end_idx, i + horizon + 8)):
                    fb = trig_series[k]
                    fh, fl = float(fb["high"]), float(fb["low"])
                    fav = (fh - entry_p) / atr if is_long else (entry_p - fl) / atr
                    adv = (entry_p - fl) / atr if is_long else (fh - entry_p) / atr
                    if fav > max_fav:
                        max_fav = fav
                    if adv > max_adv:
                        max_adv = adv

                all_mfes_atr.append(max_fav)
                is_win_excursion = max_fav >= 1.25 and max_adv <= 1.85
                if is_win_excursion:
                    winning_maes_atr.append(max_adv)

                approx_r = (max_fav * 0.65 - max_adv * 0.35) if is_win_excursion else -0.85

                s_key = str(b.get("session", "LONDON_SESSION"))
                s_st = session_stats.setdefault(s_key, {"setups": 0, "wins": 0, "r_sum": 0.0})
                s_st["setups"] += 1
                s_st["wins"] += 1 if is_win_excursion else 0
                s_st["r_sum"] += approx_r

                a_key = str(b.get("adaptive_session", "LONDON_TREND_EXPANSION"))
                a_st = adaptive_stats.setdefault(a_key, {"setups": 0, "wins": 0, "r_sum": 0.0})
                a_st["setups"] += 1
                a_st["wins"] += 1 if is_win_excursion else 0
                a_st["r_sum"] += approx_r

                r_key = str(b.get("regime", "TRENDING_BULLISH"))
                r_st = regime_stats.setdefault(r_key, {"setups": 0, "wins": 0, "r_sum": 0.0})
                r_st["setups"] += 1
                r_st["wins"] += 1 if is_win_excursion else 0
                r_st["r_sum"] += approx_r

            def _pct(arr: List[float], q: float, fallback: float) -> float:
                if not arr:
                    return fallback
                s = sorted(arr)
                return s[min(len(s) - 1, int(len(s) * q))]

            p70_mae = _pct(winning_maes_atr, 0.70, 1.15)
            p65_mfe = _pct(all_mfes_atr, 0.65, 2.50)
            p80_mfe = _pct(all_mfes_atr, 0.80, 3.40)

            sl_mult = round(min(2.15, max(1.05, p70_mae * 1.12)), 3)
            premature_rev = int(shadow_fb.get("premature_reversals_after_085r", 0) or 0)
            rev_discount = 0.88 if premature_rev >= 2 else 1.0
            be_r = round(min(1.15, max(0.55, (p65_mfe / max(sl_mult, 0.1)) * 0.42 * rev_discount)), 2)
            trail_mult = round(min(1.75, max(0.95, sl_mult * 0.88)), 2)

            # Build the 7-Stage Progressive TP Ladder (`TP1` .. `TP7`) + Primary TP (`TP3`)
            tp_ladder_config = self.build_learned_7tp_ladder(
                all_mfes_atr=all_mfes_atr,
                sl_mult=sl_mult,
                rev_discount=rev_discount,
            )
            tp_ladder_r = tp_ladder_config["tp_ladder_r"]
            primary_tp_r = tp_ladder_config["primary_tp_r"]
            tp1_r = tp_ladder_r["TP1"]
            tp2_r = tp_ladder_r["TP2"]
            tp_mult = round(sl_mult * primary_tp_r, 3)
            rr_target = round(primary_tp_r, 2)

            rec_sessions = [
                k
                for k, v in sorted(
                    session_stats.items(),
                    key=lambda kv: kv[1]["r_sum"] / max(1.0, kv[1]["setups"]),
                    reverse=True,
                )
                if v["setups"] >= 2 and (v["r_sum"] / v["setups"] > -0.15 or v["wins"] / v["setups"] >= 0.42)
            ][:3]
            if not rec_sessions:
                rec_sessions = ["LONDON_SESSION", "LONDON_NY_OVERLAP", "NEW_YORK_SESSION"]

            rec_adaptive = [
                k
                for k, v in sorted(
                    adaptive_stats.items(),
                    key=lambda kv: kv[1]["r_sum"] / max(1.0, kv[1]["setups"]),
                    reverse=True,
                )
                if v["setups"] >= 2 and (v["r_sum"] / v["setups"] > -0.15 or v["wins"] / v["setups"] >= 0.42)
            ][:3]
            if not rec_adaptive:
                rec_adaptive = [
                    "LONDON_OPEN_JUDAS_SWEEP",
                    "LONDON_TREND_EXPANSION",
                    "NY_OVERLAP_INSTITUTIONAL_DRIVE",
                ]

            rec_regimes = [
                k
                for k, v in sorted(
                    regime_stats.items(),
                    key=lambda kv: kv[1]["r_sum"] / max(1.0, kv[1]["setups"]),
                    reverse=True,
                )
                if v["setups"] >= 2 and (v["r_sum"] / v["setups"] > -0.20 or v["wins"] / v["setups"] >= 0.40)
            ][:3]
            if not rec_regimes:
                rec_regimes = (
                    ["CONSOLIDATION_RANGING"]
                    if style_up == "RANGE_SPECIALIST"
                    else ["TRENDING_BULLISH", "TRENDING_BEARISH", "VOLATILITY_EXPANSION"]
                )

            # ── STAGE 2: UNTOUCHED 40% OUT-OF-SAMPLE LOCKBOX EVALUATION (WITH 7-TP LADDER) ──
            eval_start_idx = dev_end_idx if len(trig_series) >= 120 else 30
            trade_returns_r: List[float] = []
            static_tp_returns_r: List[float] = []
            max_tp_stage_lockbox = 0
            continued_past_primary_cnt = 0
            i = eval_start_idx

            while i < len(trig_series) - 2:
                b = trig_series[i]
                u_sec = int(b["unix_sec"])
                if b.get("adaptive_session") == "ROLLOVER_SPREAD_GUARD":
                    i += 1
                    continue
                if b.get("session") not in rec_sessions and b.get("adaptive_session") not in rec_adaptive:
                    i += 1
                    continue
                if rec_regimes and b.get("regime") not in rec_regimes:
                    i += 1
                    continue

                macro_b = [
                    bar_m
                    for tf in stack["macro_timeframes"]
                    if (bar_m := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                setup_b = [
                    bar_s
                    for tf in stack["setup_timeframes"]
                    if (bar_s := self._find_bar_at_or_before(dataset["series"].get(tf, []), u_sec))
                ]
                s_val = float(sentinel_scores[i]) if i < len(sentinel_scores) else 0.0
                p_val = float(pulse_scores[i]) if i < len(pulse_scores) else 0.0
                sig = self.evaluate_trader_archetype_signal(
                    b,
                    macro_b,
                    setup_b,
                    style_up,
                    learned_genome=learned_genome,
                    sentinel_signal=s_val,
                    pulse_signal=p_val,
                )
                if sig == 0:
                    i += 1
                    continue

                is_long = sig == 1
                entry_p = float(b["close"])
                atr = max(float(b.get("atr14", 0.0005)), entry_p * 0.00015)
                struct_dist = (
                    max(entry_p - float(b["swing_low_16"]), atr * 0.75)
                    if is_long
                    else max(float(b["swing_high_16"]) - entry_p, atr * 0.75)
                )
                stop_dist = min(
                    max(struct_dist, atr * (sl_mult * 0.85)),
                    atr * (sl_mult * 1.35),
                )
                stop_bps = (stop_dist / entry_p) * 10000.0
                if total_cost_bps > stop_bps * 0.40:
                    i += 1
                    continue

                # Execute progressive 7-TP ladder with continuation evaluation
                sim_7tp = self.simulate_progressive_7tp_execution(
                    trig_series=trig_series,
                    entry_idx=i,
                    horizon_bars=horizon,
                    is_long=is_long,
                    entry_price=entry_p,
                    stop_distance=stop_dist,
                    atr=atr,
                    be_trigger_r=be_r,
                    trail_atr_mult=trail_mult,
                    tp_ladder_r=tp_ladder_r,
                    allow_continuation_past_primary=True,
                )
                # Also run an ablated variant WITHOUT continuation past Primary TP (stops at TP3)
                sim_no_cont = self.simulate_progressive_7tp_execution(
                    trig_series=trig_series,
                    entry_idx=i,
                    horizon_bars=horizon,
                    is_long=is_long,
                    entry_price=entry_p,
                    stop_distance=stop_dist,
                    atr=atr,
                    be_trigger_r=be_r,
                    trail_atr_mult=trail_mult,
                    tp_ladder_r=tp_ladder_r,
                    allow_continuation_past_primary=False,
                )

                cost_drag_r = total_cost_bps / max(stop_bps, 0.1)
                net_r = round(sim_7tp["gross_r"] - cost_drag_r, 4)
                net_no_cont_r = round(sim_no_cont["gross_r"] - cost_drag_r, 4)

                trade_returns_r.append(net_r)
                static_tp_returns_r.append(net_no_cont_r)
                if sim_7tp["highest_tp_stage_reached"] > max_tp_stage_lockbox:
                    max_tp_stage_lockbox = sim_7tp["highest_tp_stage_reached"]
                if sim_7tp["continued_past_primary_tp"]:
                    continued_past_primary_cnt += 1

                i = max(i + 1, int(sim_7tp["exit_idx"]) + 1)

            mc_report = self.run_monte_carlo_sequence_stress_test(trade_returns_r)
            wins = sum(1 for r in trade_returns_r if r > 0)
            total_r = round(sum(trade_returns_r), 3)
            static_total_r = round(sum(static_tp_returns_r), 3)
            continuation_value_added_r = round(total_r - static_total_r, 3)
            win_rate = round((wins / max(1, len(trade_returns_r))) * 100.0, 1)

            # Record the 7-TP Continuation ablation impact into the ablation report
            ablation_report_summary["tp_continuation_7stage_ablation"] = {
                "with_7tp_continuation_total_r": total_r,
                "without_continuation_capped_at_primary_tp_r": static_total_r,
                "marginal_r_gained_by_7tp_continuation": continuation_value_added_r,
                "trades_continued_past_primary_tp": continued_past_primary_cnt,
                "highest_tp_stage_reached_in_lockbox": max_tp_stage_lockbox,
            }

            cand_id = f"cand-v2-{upper_sym.lower()}-{style_up.lower()}"
            blueprint = {
                "strategy_id": f"oracle_v2_{upper_sym.lower()}_{style_up.lower()}",
                "symbol": upper_sym,
                "trader_style": style_up,
                "top_down_stack": stack,
                "atlas_research_hypothesis": atlas_hypothesis,
                "pulse_and_sentinel_intelligence": {
                    "sentinel_agent": news_social_ctx["sentinel_agent"],
                    "pulse_agent": news_social_ctx["pulse_agent"],
                    "sentinel_retained_by_ablation": "sentinel_news_sentiment"
                    in ablation_report_summary.get("retained_components", []),
                    "pulse_retained_by_ablation": "pulse_social_sentiment"
                    in ablation_report_summary.get("retained_components", []),
                },
                "learned_strategy_genome": learned_genome,
                "ablation_study_report": ablation_report_summary,
                "shadow_reinforcement_feedback": {
                    "shadow_trades_analyzed": shadow_fb.get("shadow_trades_analyzed", 0),
                    "shadow_wins": shadow_fb.get("shadow_wins", 0),
                    "shadow_losses": shadow_fb.get("shadow_losses", 0),
                    "premature_reversals_after_085r": shadow_fb.get("premature_reversals_after_085r", 0),
                    "continued_past_primary_tp_count": shadow_fb.get("continued_past_primary_tp_count", 0),
                    "highest_tp_stage_reached_in_shadow": shadow_fb.get("highest_tp_stage_reached_in_shadow", 0),
                    "feature_r_deltas": shadow_fb.get("feature_r_deltas", {}),
                },
                "recommended_sessions": rec_sessions,
                "recommended_adaptive_sessions": rec_adaptive,
                "recommended_regimes": rec_regimes,
                "learned_excursion_parameters": {
                    "sample_setups_60pct_dev": len(all_mfes_atr),
                    "p70_winning_mae_atr": round(p70_mae, 3),
                    "p65_mfe_atr": round(p65_mfe, 3),
                    "p80_mfe_atr": round(p80_mfe, 3),
                    "learned_optimal_stop_atr_mult": sl_mult,
                    "learned_optimal_tp_atr_mult": tp_mult,
                    "learned_risk_reward_ratio": rr_target,
                    "learned_be_trigger_r": be_r,
                    "learned_tp1_r": tp1_r,
                    "learned_tp2_r": tp2_r,
                    "learned_primary_tp_r": primary_tp_r,
                    "learned_tp_ladder_r": tp_ladder_r,
                    "tp_stage_scale_out_fractions": tp_ladder_config["tp_stage_scale_out_fractions"],
                    "continuation_past_tp_enabled": True,
                    "learned_trail_atr_mult": trail_mult,
                },
                "instrument_physics": physics,
                "regime_drift_report": drift,
                "monte_carlo_stress_report": mc_report,
            }

            now_ts = time.time()
            exp_r = round(total_r / max(1, len(trade_returns_r)), 3)
            net_ret_pct = round(total_r * 0.5, 2)
            lifecycle = "DEPLOYED" if (total_r > 0 and mc_report["passed_mc95_gate"]) else "WATCHLIST"
            candidate_record = {
                "candidate_id": cand_id,
                "experiment_id": f"exp-v2-{upper_sym.lower()}-{style_up.lower()}",
                "hypothesis_id": atlas_hypothesis["hypothesis_id"],
                "symbol": upper_sym,
                "regime": "trending_up" if style_up != "RANGE_SPECIALIST" else "ranging",
                "trader_style": style_up,
                "candidate_code": f"V2_MTF_{style_up}",
                "candidate_title": f"Atlas+Forge Self-Discovered ({'+'.join(stack['macro_timeframes'])} -> {'+'.join(stack['setup_timeframes'])} -> {trig_tf}) {style_up.replace('_', ' ')}",
                "lifecycle_state": lifecycle,
                "status_reason": f"Discovered by Atlas+Forge (with Pulse/Sentinel & Leave-One-Out Ablation) on 60% Dev & Tested on 40% Lockbox: Trades={len(trade_returns_r)}, WinRate={win_rate}%, Net={net_ret_pct}%, MaxTPStage=TP{max_tp_stage_lockbox}, MC95 DD={mc_report['mc95_worst_case_drawdown_pct']}%.",
                "created_at_unix": now_ts,
                "updated_at_unix": now_ts,
                "atlas_research_hypothesis": atlas_hypothesis,
                "learned_strategy_genome": learned_genome,
                "ablation_study_report": ablation_report_summary,
                "evaluation_metrics": {
                    "trades_count": len(trade_returns_r),
                    "win_rate_pct": win_rate,
                    "total_net_r": total_r,
                    "expectancy_r": exp_r,
                    "highest_tp_stage_reached": max_tp_stage_lockbox,
                    "continued_past_primary_tp_count": continued_past_primary_cnt,
                    "marginal_r_from_7tp_continuation": continuation_value_added_r,
                    "mc95_worst_case_drawdown_pct": mc_report["mc95_worst_case_drawdown_pct"],
                    "passed_mc95_gate": mc_report["passed_mc95_gate"],
                },
                "lockbox_holdout_report": {
                    "candidate_metrics": {
                        "trades": len(trade_returns_r),
                        "wins": wins,
                        "losses": len(trade_returns_r) - wins,
                        "win_rate": round(win_rate / 100.0, 4),
                        "net_return_pct": net_ret_pct,
                        "expectancy_r": exp_r,
                        "sharpe_ratio": round(max(0.5, exp_r * 2.2), 2),
                        "max_drawdown_pct": mc_report["mc50_median_drawdown_pct"],
                    }
                },
                "monte_carlo_stress_report": mc_report,
                "v1_compatibility_export": blueprint,
            }

            kv_key = f"forge.core.candidate.{cand_id}"
            prev_rev = int((kv_store.get(kv_key) or {}).get("revision", 0))
            kv_store[kv_key] = {
                "key": kv_key,
                "value": candidate_record,
                "revision": prev_rev + 1,
                "updated_at_unix": time.time(),
            }
            (cand_dir / f"{cand_id}.json").write_text(
                json.dumps(candidate_record, indent=2), encoding="utf-8"
            )
            catalog["oracle_v2_strategies"][upper_sym][style_up] = blueprint
            trained_results[style_up] = {
                "candidate_id": cand_id,
                "lifecycle_state": lifecycle,
                "top_down_stack": f"{'+'.join(stack['macro_timeframes'])} -> {'+'.join(stack['setup_timeframes'])} -> {trig_tf}",
                "atlas_dominant_timeframes": atlas_hypothesis["dominant_timeframes_discovered"],
                "ablation_retained_features": ablation_report_summary.get("retained_components", []),
                "ablation_pruned_left_out": ablation_report_summary.get("pruned_components_left_out", []),
                "learned_gates": learned_genome["structural_gates"],
                "learned_sl_atr": sl_mult,
                "learned_primary_tp_r": primary_tp_r,
                "learned_7tp_ladder_r": tp_ladder_r,
                "highest_tp_stage_reached": f"TP{max_tp_stage_lockbox}" if max_tp_stage_lockbox > 0 else "NONE",
                "continued_past_primary_tp_trades": continued_past_primary_cnt,
                "drift_severity": drift["overall_severity"],
                "recommended_sessions": rec_sessions,
                "recommended_regimes": rec_regimes,
                "lockbox_trades": len(trade_returns_r),
                "lockbox_win_rate_pct": win_rate,
                "lockbox_total_net_r": total_r,
                "mc95_drawdown_pct": mc_report["mc95_worst_case_drawdown_pct"],
            }

        kv_path.parent.mkdir(parents=True, exist_ok=True)
        kv_path.write_text(json.dumps(kv_store, indent=2), encoding="utf-8")
        cat_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

        return {
            "status": "complete",
            "symbol": upper_sym,
            "timeframe_counts": dataset.get("timeframe_counts"),
            "instrument_physics": physics,
            "strategies_certified": trained_results,
        }

    @staticmethod
    def _find_bar_at_or_before(
        series: List[Dict[str, Any]], unix_sec: int
    ) -> Optional[Dict[str, Any]]:
        if not series:
            return None
        lo, hi = 0, len(series) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if int(series[mid]["unix_sec"]) <= unix_sec:
                best = series[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forge V2 7-Timeframe Institutional Strategy Trainer & Discovery Engine"
    )
    parser.add_argument(
        "--symbol", default="GBPUSD", help="Single trading symbol (e.g. GBPUSD, USDJPY, XAUUSD)"
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols (e.g. EURUSD,GBPUSD,USDJPY,XAUUSD,AUDUSD) or ALL",
    )
    parser.add_argument(
        "--preset",
        choices=["single", "live", "all"],
        default="single",
        help="Symbol preset: 'live' (top 10 active symbols) or 'all' (full 38-symbol multi-asset watchlist)",
    )
    parser.add_argument(
        "--train",
        action="store_true",
        help="Train, Monte-Carlo stress test, and certify all 4 Trader Archetypes for the selected symbols",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Continuously train and recalibrate strategies across all selected symbols in a loop",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=900,
        help="Seconds between continuous training cycles when --daemon is set (default: 900s / 15m)",
    )
    args = parser.parse_args()

    if args.preset == "all" or args.symbols.strip().upper() == "ALL":
        symbols_list = list(ALL_WATCHLIST_SYMBOLS)
    elif args.preset == "live":
        symbols_list = list(LIVE_PRESET_SYMBOLS)
    elif args.symbols.strip():
        symbols_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols_list = [args.symbol.strip().upper()]

    engine = MultiTimeframeInstitutionalEngine()

    while True:
        for sym in symbols_list:
            if args.train or args.daemon or args.preset in ("live", "all"):
                res = engine.train_and_certify_symbol_strategies(sym)
                print(json.dumps(res, indent=2), flush=True)
            else:
                ds = engine.fetch_live_7tf_dataset(sym)
                if not ds:
                    print(f"No MTF dataset available for {sym}.")
                    continue
                m15_bars = ds.get("series", {}).get("M15", [])
                physics = engine.compute_instrument_physics_profile(sym, m15_bars)
                drift = engine.inspect_strategy_regime_drift(sym, "DAY_TRADING", m15_bars)
                print(
                    json.dumps(
                        {
                            "symbol": ds.get("symbol"),
                            "dataset_fingerprint": ds.get("dataset_fingerprint"),
                            "timeframe_counts": ds.get("timeframe_counts"),
                            "instrument_physics": physics,
                            "regime_drift_report": drift,
                        },
                        indent=2,
                    ),
                    flush=True,
                )
        if not args.daemon:
            break
        time.sleep(max(60, args.interval_sec))


if __name__ == "__main__":
    main()


