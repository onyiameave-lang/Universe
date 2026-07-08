"""
Oracle.intelligence.technicals
=============================
Real technical analysis and market-regime detection. (Book I Part IV Article X
Decision Making with evidence; institutional quant practice.)

Genuine indicator math (no libraries required): SMA/EMA, RSI, MACD, Bollinger
Bands, ATR (volatility), and returns statistics. Plus a REGIME classifier
(trending-up / trending-down / ranging / high-volatility) because the right
strategy depends on the regime, and an institutional desk always knows the regime.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: List[float], period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0)); losses.append(max(-change, 0))
    avg_gain = sum(gains) / period; avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Dict[str, float]]:
    if len(values) < slow + signal:
        return None
    ef, es = ema_series(values, fast), ema_series(values, slow)
    macd_line = [f - s for f, s in zip(ef[-len(es):], es)]
    signal_line = ema_series(macd_line, signal)
    return {"macd": round(macd_line[-1], 5), "signal": round(signal_line[-1], 5),
            "histogram": round(macd_line[-1] - signal_line[-1], 5)}


def bollinger(values: List[float], period: int = 20, num_std: float = 2.0) -> Optional[Dict[str, float]]:
    if len(values) < period:
        return None
    window = values[-period:]
    mid = sum(window) / period
    var = sum((v - mid) ** 2 for v in window) / period
    sd = math.sqrt(var)
    last = values[-1]
    width = (2 * num_std * sd) / mid if mid else 0
    pct_b = (last - (mid - num_std * sd)) / (2 * num_std * sd) if sd else 0.5
    return {"mid": round(mid, 5), "upper": round(mid + num_std * sd, 5),
            "lower": round(mid - num_std * sd, 5), "width": round(width, 5),
            "percent_b": round(pct_b, 3)}


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return round(sum(trs) / period, 5)


def returns_stats(closes: List[float]) -> Dict[str, float]:
    if len(closes) < 2:
        return {"mean": 0.0, "vol": 0.0, "sharpe_proxy": 0.0}
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    if not rets:
        return {"mean": 0.0, "vol": 0.0, "sharpe_proxy": 0.0}
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    vol = math.sqrt(var)
    return {"mean": round(mean, 6), "vol": round(vol, 6),
            "sharpe_proxy": round(mean / vol, 4) if vol else 0.0}


def analyze(series) -> Dict[str, Any]:
    """Full technical snapshot for a price series."""
    closes, highs, lows = series.closes, series.highs, series.lows
    if len(closes) < 30:
        return {"error": "insufficient history for technicals", "bars": len(closes)}
    ind = {
        "last": closes[-1], "sma_20": sma(closes, 20), "sma_50": sma(closes, 50),
        "ema_12": ema(closes, 12), "ema_26": ema(closes, 26), "rsi_14": rsi(closes, 14),
        "macd": macd(closes), "bollinger": bollinger(closes), "atr_14": atr(highs, lows, closes, 14),
        "returns": returns_stats(closes),
    }
    ind["regime"] = detect_regime(closes, ind)
    return ind


def detect_regime(closes: List[float], ind: Dict[str, Any]) -> Dict[str, Any]:
    """Classify the market regime from real indicators."""
    sma20, sma50 = ind.get("sma_20"), ind.get("sma_50")
    vol = ind.get("returns", {}).get("vol", 0)
    bb = ind.get("bollinger") or {}
    width = bb.get("width", 0)

    # trend via SMA relationship + slope
    slope = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 and closes[-20] else 0
    high_vol = vol > 0.02 or width > 0.08

    if high_vol:
        regime = "high_volatility"
    elif sma20 and sma50 and sma20 > sma50 * 1.002 and slope > 0.01:
        regime = "trending_up"
    elif sma20 and sma50 and sma20 < sma50 * 0.998 and slope < -0.01:
        regime = "trending_down"
    else:
        regime = "ranging"
    return {"regime": regime, "slope_20": round(slope, 4), "volatility": round(vol, 5),
            "high_volatility": high_vol}
