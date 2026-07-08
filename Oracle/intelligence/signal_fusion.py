"""
Oracle.intelligence.signal_fusion
================================
Multi-signal fusion: combine technical, news, social, and memory into one
evidence-based trading view. (Book II Part II Ch VIII Multi-Agent Conversations;
Book I Part IV Article X Decision Making; Book II Principle III Memory First.)

An institutional desk never trades on price alone. Oracle fuses four independent
evidence streams, each weighted by its own confidence and (for news/social)
credibility/authenticity:

  * TECHNICAL   direction + strength from indicators and regime (Oracle's own).
  * NEWS        Sentinel's credibility-weighted sentiment for the symbol.
  * SOCIAL      Pulse's authenticity-weighted sentiment (+ manipulation guard).
  * MEMORY      Chronicle's recall of how similar setups resolved before.

The fused signal carries a direction, a calibrated confidence, and a full
breakdown so every decision is explainable and auditable. Conflicting streams
LOWER confidence (honest uncertainty), agreement raises it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class SignalFusion:
    # base weights per stream; scaled by each stream's own confidence at runtime
    WEIGHTS = {"technical": 0.45, "news": 0.22, "social": 0.18, "memory": 0.15}

    def __init__(self, sentinel=None, pulse=None, chronicle=None):
        self.sentinel = sentinel
        self.pulse = pulse
        self.chronicle = chronicle

    def _technical_signal(self, technicals: Dict[str, Any]) -> Dict[str, Any]:
        if "error" in technicals:
            return {"direction": 0.0, "confidence": 0.0, "note": technicals["error"]}
        score = 0.0
        reasons = []
        rsi = technicals.get("rsi_14")
        if rsi is not None:
            if rsi < 30:
                score += 0.4; reasons.append(f"RSI {rsi} oversold")
            elif rsi > 70:
                score -= 0.4; reasons.append(f"RSI {rsi} overbought")
        macd = technicals.get("macd") or {}
        if macd.get("histogram", 0) > 0:
            score += 0.3; reasons.append("MACD bullish")
        elif macd.get("histogram", 0) < 0:
            score -= 0.3; reasons.append("MACD bearish")
        sma20, sma50 = technicals.get("sma_20"), technicals.get("sma_50")
        if sma20 and sma50:
            if sma20 > sma50:
                score += 0.3; reasons.append("SMA20>SMA50 uptrend")
            else:
                score -= 0.3; reasons.append("SMA20<SMA50 downtrend")
        regime = (technicals.get("regime") or {}).get("regime", "ranging")
        # confidence lower in ranging / high-vol regimes
        conf = {"trending_up": 0.8, "trending_down": 0.8, "ranging": 0.5,
                "high_volatility": 0.4}.get(regime, 0.5)
        return {"direction": max(-1.0, min(1.0, score)), "confidence": conf,
               "regime": regime, "reasons": reasons}

    def _news_signal(self, symbol: str) -> Dict[str, Any]:
        if self.sentinel is None:
            return {"direction": 0.0, "confidence": 0.0, "note": "sentinel unavailable"}
        try:
            s = self.sentinel.sentiment_for(symbol)
            return {"direction": s.get("sentiment", 0.0),
                   "confidence": s.get("confidence", 0.0),
                   "cross_source": s.get("cross_source", False),
                   "articles": s.get("article_count", 0)}
        except Exception:
            return {"direction": 0.0, "confidence": 0.0, "note": "sentinel error"}

    def _social_signal(self, symbol: str) -> Dict[str, Any]:
        if self.pulse is None:
            return {"direction": 0.0, "confidence": 0.0, "note": "pulse unavailable"}
        try:
            s = self.pulse.sentiment_for(symbol)
            conf = s.get("confidence", 0.0)
            # a manipulation warning slashes social confidence
            if s.get("manipulation_warning"):
                conf *= 0.3
            return {"direction": s.get("sentiment", 0.0), "confidence": conf,
                   "posts": s.get("post_count", 0),
                   "manipulation_warning": s.get("manipulation_warning", False)}
        except Exception:
            return {"direction": 0.0, "confidence": 0.0, "note": "pulse error"}

    def _memory_signal(self, symbol: str, regime: str) -> Dict[str, Any]:
        if self.chronicle is None:
            return {"direction": 0.0, "confidence": 0.0, "note": "chronicle unavailable"}
        try:
            mems = self.chronicle.search(query=f"{symbol} {regime} strategy outcome",
                                        domain="trading", limit=4, requester="oracle")
            if not mems:
                return {"direction": 0.0, "confidence": 0.0, "note": "no prior memory"}
            # infer directional lesson from memory summaries
            score, n = 0.0, 0
            for m in mems:
                text = (m.get("summary", "") if isinstance(m, dict) else str(m)).lower()
                if any(w in text for w in ("profit", "worked", "success", "long", "up")):
                    score += 1; n += 1
                elif any(w in text for w in ("loss", "failed", "short", "down")):
                    score -= 1; n += 1
            direction = score / n if n else 0.0
            return {"direction": round(direction, 3), "confidence": min(len(mems) / 4.0, 0.7),
                   "memories": len(mems)}
        except Exception:
            return {"direction": 0.0, "confidence": 0.0, "note": "chronicle error"}

    def fuse(self, symbol: str, technicals: Dict[str, Any]) -> Dict[str, Any]:
        streams = {
            "technical": self._technical_signal(technicals),
            "news": self._news_signal(symbol),
            "social": self._social_signal(symbol),
            "memory": self._memory_signal(symbol, (technicals.get("regime") or {}).get("regime", "ranging")),
        }
        # confidence-weighted fusion
        num, denom = 0.0, 0.0
        for name, sig in streams.items():
            w = self.WEIGHTS[name] * sig.get("confidence", 0.0)
            num += w * sig.get("direction", 0.0)
            denom += w
        fused_direction = num / denom if denom else 0.0

        # agreement/disagreement adjusts confidence honestly
        directions = [s["direction"] for s in streams.values()
                     if s.get("confidence", 0) > 0.1 and abs(s["direction"]) > 0.05]
        agreement = 0.5
        if len(directions) >= 2:
            pos = sum(1 for d in directions if d > 0); neg = sum(1 for d in directions if d < 0)
            agreement = max(pos, neg) / len(directions)
        base_conf = denom / sum(self.WEIGHTS.values())  # coverage-weighted
        confidence = round(min(base_conf * (0.5 + 0.5 * agreement), 0.95), 3)

        if fused_direction > 0.15:
            call = "buy"
        elif fused_direction < -0.15:
            call = "sell"
        else:
            call = "hold"

        return {"symbol": symbol, "call": call, "direction": round(fused_direction, 3),
               "confidence": confidence, "agreement": round(agreement, 3),
               "streams": streams,
               "manipulation_warning": streams["social"].get("manipulation_warning", False)}
