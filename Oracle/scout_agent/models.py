"""
Oracle.scout_agent.models
=========================
Data structures and domain classifiers for OracleScoutSubAgent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Evidence dimensions evaluated by OracleScoutSubAgent.
# Initialized via maximum-entropy symmetry (1 / len(SCOUT_DIMENSIONS)) so no
# arbitrary hand-picked weights exist.
SCOUT_DIMENSIONS: Tuple[str, ...] = (
    "technical_blueprint",
    "sentinel_news",
    "pulse_social",
    "chronicle_memory",
    "htf_structure",
    "spread_efficiency",
)


def classify_asset_class(symbol: str) -> str:
    """Classify canonical symbol into its institutional asset class."""
    s = str(symbol or "").upper().strip()
    if any(k in s for k in ("XAU", "XAG", "XPT", "GOLD", "SILVER")):
        return "metals"
    if any(k in s for k in ("OIL", "WTI", "BRENT", "NATGAS", "XTI", "XBR", "XNG")):
        return "energy"
    if any(k in s for k in ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LTC")):
        return "crypto"
    if any(k in s for k in ("US30", "US500", "NAS100", "GER40", "UK100", "JPN225", "AUS200", "SPX", "NDX")):
        return "indices"
    return "fx"


def current_trading_session(utc_now: Optional[datetime] = None) -> str:
    """Identify the active institutional liquidity session from UTC hour."""
    now = utc_now or datetime.now(timezone.utc)
    hour = now.hour
    if 12 <= hour < 16:
        return "london_ny_overlap"
    if 7 <= hour < 16:
        return "london"
    if 16 <= hour < 21:
        return "new_york"
    return "asian"


@dataclass
class ScoutCandidateEvaluation:
    symbol: str
    direction: str  # "long" | "short" | "hold"
    call: str       # "buy" | "sell" | "hold"
    raw_confidence: float
    effective_confidence: float
    learned_conviction_floor: float
    passes_conviction_floor: bool
    scout_score: float
    expected_net_r: float
    regime: str
    session: str
    asset_class: str
    last_price: float
    atr: float
    hypothetical_stop: float
    hypothetical_target: float
    reward_risk_ratio: float
    spread_to_risk_ratio: float
    strategy_id: Optional[str]
    using_evolved_champion: bool
    dimension_scores: Dict[str, float]
    learned_dimension_weights: Dict[str, float]
    streams: Dict[str, Any]
    news_assessment: Dict[str, Any]
    social_assessment: Dict[str, Any]
    why_trade_reasons: List[str] = field(default_factory=list)
    why_not_reasons: List[str] = field(default_factory=list)
    tournament_rank: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "call": self.call,
            "raw_confidence": self.raw_confidence,
            "effective_confidence": self.effective_confidence,
            "learned_conviction_floor": self.learned_conviction_floor,
            "passes_conviction_floor": self.passes_conviction_floor,
            "scout_score": self.scout_score,
            "expected_net_r": self.expected_net_r,
            "regime": self.regime,
            "session": self.session,
            "asset_class": self.asset_class,
            "last_price": self.last_price,
            "atr": self.atr,
            "hypothetical_stop": self.hypothetical_stop,
            "hypothetical_target": self.hypothetical_target,
            "reward_risk_ratio": self.reward_risk_ratio,
            "spread_to_risk_ratio": self.spread_to_risk_ratio,
            "strategy_id": self.strategy_id,
            "using_evolved_champion": self.using_evolved_champion,
            "dimension_scores": dict(self.dimension_scores),
            "learned_dimension_weights": dict(self.learned_dimension_weights),
            "news_assessment": dict(self.news_assessment),
            "social_assessment": dict(self.social_assessment),
            "why_trade_reasons": list(self.why_trade_reasons),
            "why_not_reasons": list(self.why_not_reasons),
            "tournament_rank": self.tournament_rank,
        }
