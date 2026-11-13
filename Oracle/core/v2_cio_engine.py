"""
Oracle.core.v2_cio_engine
=========================
Oracle V2: The Wall Street Chief Investment Officer (CIO) & 3 Sub-Agents Engine.
(Books I–VI Constitutional Architecture: Zero Hardcoded Variables, Constant
Empirical Learning, Zero Silent Failures, First-Class Sentinel + Pulse + Atlas +
Chronicle + Forge Integration.)

Architecture
------------
Oracle V2 no longer trains strategies ("HOW we trade" belongs exclusively to Forge).
Instead, Oracle V2 acts as an institutional Wall Street CIO managing 3 specialized,
continuously learning sub-agents:

  1. OracleScoutSubAgent ("Opportunity Scout & Cross-Sectional Tournament")
     - Evaluates WHY we should trade each symbol right now.
     - Fuses Forge strategy blueprints + Sentinel (News Intel & term reliability)
       + Pulse (Social Intel & manipulation detection) + Chronicle (Episodic Memory)
       + Higher-Timeframe (HTF) structure & broker spread efficiency.
     - Learns feature reliability weights and the empirical breakeven conviction
       floor from realized outcomes and counterfactual replays (zero hardcoded
       weights or static floors).
     - Ranks all candidate symbols in a Cross-Sectional Tournament.

  2. OraclePortfolioSubAgent ("Capital, Slot Budget & Asset Allocation Manager")
     - Manages capital, equity curve state, open positions, and asset-class
       exposure (fx, metals, energy, crypto, indices).
     - Learns optimal session concurrency slots, daily trade budgets, and
       empirical variance-penalized Kelly risk fractions directly from realized
       trade-sequence and concurrency outcomes (zero hardcoded slot caps).

  3. OracleRiskSubAgent ("Correlation, Currency Exposure & Active Defense Gate")
     - Evaluates "Is this trade worth the risk?" against all open & batch-selected
       positions.
     - Computes empirical rolling Pearson return correlation from actual price
       series + currency-leg directional decomposition.
     - Learns correlation danger cutoffs, currency concentration limits,
       post-trade recovery cooldowns, and news/social shock multipliers from
       realized multi-position outcomes and counterfactuals.
"""
from __future__ import annotations

import gc
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from intelligence.news_impact import NewsImpactClassifier, assess_news_impact  # type: ignore
except ImportError:
    from Oracle.intelligence.news_impact import NewsImpactClassifier, assess_news_impact  # type: ignore

try:
    from intelligence.social_risk import SocialRiskClassifier, assess_social_risk  # type: ignore
except ImportError:
    from Oracle.intelligence.social_risk import SocialRiskClassifier, assess_social_risk  # type: ignore

try:
    from intelligence.decision_ledger import OracleDecisionLedger  # type: ignore
except ImportError:
    from Oracle.intelligence.decision_ledger import OracleDecisionLedger  # type: ignore

log = logging.getLogger("oracle.v2_cio")

_ORACLE_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_PATH = _ORACLE_ROOT / "memory" / "oracle_v2_learned_state.json"

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


def extract_currency_legs(symbol: str) -> Tuple[str, Optional[str]]:
    """Decompose a symbol into (base, quote) currency legs for exposure correlation."""
    s = str(symbol or "").upper().strip()
    if len(s) == 6 and s.isalpha():
        return s[:3], s[3:]
    for suffix in ("USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)], suffix
    return s, None


def compute_return_correlation(closes_a: List[float], closes_b: List[float]) -> Optional[float]:
    """
    Compute empirical Pearson correlation of log-returns between two price series.
    Returns None if insufficient overlapping bars exist.
    """
    n = min(len(closes_a), len(closes_b))
    if n < 15:
        return None
    a = closes_a[-n:]
    b = closes_b[-n:]
    rets_a: List[float] = []
    rets_b: List[float] = []
    for i in range(1, n):
        if a[i - 1] <= 0 or a[i] <= 0 or b[i - 1] <= 0 or b[i] <= 0:
            continue
        rets_a.append(math.log(a[i] / a[i - 1]))
        rets_b.append(math.log(b[i] / b[i - 1]))
    m = len(rets_a)
    if m < 10:
        return None
    mean_a = sum(rets_a) / m
    mean_b = sum(rets_b) / m
    cov = sum((ra - mean_a) * (rb - mean_b) for ra, rb in zip(rets_a, rets_b))
    var_a = sum((ra - mean_a) ** 2 for ra in rets_a)
    var_b = sum((rb - mean_b) ** 2 for rb in rets_b)
    denom = math.sqrt(var_a * var_b)
    if denom <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, cov / denom))


def compute_currency_leg_similarity(
    sym_a: str, dir_a: str, sym_b: str, dir_b: str
) -> float:
    """
    Structural directional correlation induced by shared currency legs.
    Example: Long EURUSD (+EUR, -USD) and Long GBPUSD (+GBP, -USD) share -USD -> +0.50.
    Example: Long EURUSD (+EUR, -USD) and Short USDCHF (-USD, +CHF) share -USD -> +0.50.
    """
    base_a, quote_a = extract_currency_legs(sym_a)
    base_b, quote_b = extract_currency_legs(sym_b)
    sign_a = 1.0 if dir_a in ("long", "buy") else -1.0
    sign_b = 1.0 if dir_b in ("long", "buy") else -1.0

    vec_a: Dict[str, float] = {base_a: sign_a}
    if quote_a:
        vec_a[quote_a] = vec_a.get(quote_a, 0.0) - sign_a

    vec_b: Dict[str, float] = {base_b: sign_b}
    if quote_b:
        vec_b[quote_b] = vec_b.get(quote_b, 0.0) - sign_b

    shared = set(vec_a.keys()) & set(vec_b.keys())
    if not shared:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in shared)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values())) or 1.0
    norm_b = math.sqrt(sum(v * v for v in vec_b.values())) or 1.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


# ============================================================================
# Persistent Self-Learning State Store (ChronicleBackedStore + JSON)
# ============================================================================

class OracleV2LearningStore:
    """
    Manages the persistent empirical distributions for Scout, Portfolio, and Risk
    sub-agents. All parameters start from mathematical maximum-entropy / neutral
    symmetry priors and evolve continuously from realized trades and counterfactual
    replays.
    """

    def __init__(
        self,
        state_path: Optional[Path | str] = None,
        storage_dir: Optional[Path | str] = None,
        chronicle_client: Any = None,
    ) -> None:
        if state_path is not None:
            resolved_path = Path(state_path)
        elif storage_dir is not None:
            resolved_path = Path(storage_dir) / "oracle_v2_learned_state.json"
        else:
            resolved_path = _DEFAULT_STATE_PATH
        self.state_path = resolved_path
        self._lock = threading.RLock()
        self._store = None
        try:
            from shared.chronicle_sync import ChronicleBackedStore  # type: ignore
            self._store = ChronicleBackedStore(
                chronicle_client,
                "oracle_v2_learned_state",
                self.state_path,
                owner="oracle",
            )
        except Exception:
            self._store = None
        self._local_cache: Dict[str, Any] = {}
        self._ensure_initialized()

    @staticmethod
    def _initial_state() -> Dict[str, Any]:
        # Maximum-entropy uniform Dirichlet alpha=1.0 per dimension -> weight = 1/K
        k_dims = len(SCOUT_DIMENSIONS)
        uniform_w = round(1.0 / k_dims, 6)
        return {
            "schema": "oracle.v2.learned_state.v1",
            "updated_at": time.time(),
            "scout": {
                # Dirichlet / Bayesian alpha-beta reliability counts per dimension
                "dimension_alpha": {d: 1.0 for d in SCOUT_DIMENSIONS},
                "dimension_beta": {d: 1.0 for d in SCOUT_DIMENSIONS},
                "dimension_weights": {d: uniform_w for d in SCOUT_DIMENSIONS},
                # Empirical outcome buckets by conviction decile to learn the true breakeven floor
                "conviction_outcomes": [],  # list of {"conviction": float, "pnl_r": float, "won": bool}
                "counterfactual_outcomes": [],  # list of {"conviction": float, "counterfactual_r": float, "stage": str}
                # Neutral binary symmetry boundary (1 / 2 directions) until empirical outcomes shift it
                "learned_conviction_floor": 0.50,
                "symbol_regime_stats": {},  # key="SYMBOL::regime" -> {"wins": int, "losses": int, "sum_r": float, "sum_r2": float}
                "session_stats": {},        # key="session" -> {"wins": int, "losses": int, "sum_r": float}
            },
            "portfolio": {
                # Concurrency bandit: tracks realized expectancy when opening k-th concurrent position
                "concurrency_outcomes": {
                    str(k): {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0}
                    for k in range(1, 10)
                },
                # Daily sequence bandit: tracks realized expectancy of the m-th trade of the UTC day
                "daily_sequence_outcomes": {
                    str(m): {"trades": 0, "wins": 0, "sum_r": 0.0}
                    for m in range(1, 25)
                },
                # Asset class performance distribution
                "asset_class_stats": {
                    ac: {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0}
                    for ac in ("fx", "metals", "energy", "crypto", "indices")
                },
                # Realized return distribution for empirical variance-penalized Kelly sizing
                "realized_trades": 0,
                "realized_wins": 0,
                "sum_win_r": 0.0,
                "sum_loss_r": 0.0,
                "sum_r": 0.0,
                "sum_r2": 0.0,
                # Empirical excursion history to derive max drawdown tolerance & daily loss limit
                "session_pnl_fractions": [],
            },
            "risk": {
                # Empirical multi-position overlap outcomes to learn correlation & currency concentration limits
                "overlap_outcomes": [],  # list of {"max_corr": float, "currency_exposure_frac": float, "joint_loss": bool, "pnl_r": float}
                # Empirical re-entry interval outcomes to learn post-win and post-loss cooldowns
                "reentry_outcomes": [],  # list of {"elapsed_sec": float, "prior_won": bool, "pnl_r": float, "won": bool}
                # Empirical news & social impact outcomes to learn shock multipliers
                "news_event_outcomes": {
                    "medium": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                    "high": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                },
                "social_event_outcomes": {
                    "medium": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                    "high": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                },
                #Empirical stop/target geometry outcomes
                "geometry_outcomes": [],  # list of {"stop_atr_mult": float, "target_atr_mult": float, "pnl_r": float, "won": bool}
            },
        }

    def _ensure_initialized(self) -> None:
        with self._lock:
            data = self.data
            defaults = self._initial_state()
            changed = False
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
                    changed = True
                elif isinstance(v, dict) and isinstance(data.get(k), dict):
                    for sub_k, sub_v in v.items():
                        if sub_k not in data[k]:
                            data[k][sub_k] = sub_v
                            changed = True
            if changed:
                self.save()

    @property
    def data(self) -> Dict[str, Any]:
        if self._store is not None:
            return self._store.data()
        if not self._local_cache:
            if self.state_path.exists():
                try:
                    loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self._local_cache = loaded
                except Exception:
                    self._local_cache = {}
        return self._local_cache

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = time.time()
            if self._store is not None:
                self._store.save()
            else:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
                tmp.replace(self.state_path)


# ============================================================================
# Sub-Agent 1: OracleScoutSubAgent
# ============================================================================

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



class OracleScoutSubAgent:
    """
    Sub-Agent 1: Opportunity Scouting & Cross-Sectional Tournament Ranking.
    Determines WHY we should trade a symbol and ranks all candidate setups
    across the watchlist so Oracle only deploys capital into the highest
    expectancy opportunities.
    """

    def __init__(
        self,
        learning_store: OracleV2LearningStore,
        decision_ledger: OracleDecisionLedger,
        sentinel_client: Any = None,
        pulse_client: Any = None,
        chronicle_client: Any = None,
    ) -> None:
        self.store = learning_store
        self.ledger = decision_ledger
        self.sentinel = sentinel_client
        self.pulse = pulse_client
        self.chronicle = chronicle_client

    @property
    def _scout_state(self) -> Dict[str, Any]:
        return self.store.data["scout"]

    def learned_dimension_weights(self) -> Dict[str, float]:
        """
        Compute normalized posterior mean reliability weights from Beta(alpha_d, beta_d)
        for each evidence dimension. Starts at maximum-entropy uniform 1/K and shifts
        toward dimensions that empirically predict profitable trades.
        """
        st = self._scout_state
        alphas = st.get("dimension_alpha", {})
        betas = st.get("dimension_beta", {})
        raw: Dict[str, float] = {}
        for dim in SCOUT_DIMENSIONS:
            a = max(0.1, float(alphas.get(dim, 1.0)))
            b = max(0.1, float(betas.get(dim, 1.0)))
            raw[dim] = a / (a + b)
        total = sum(raw.values()) or 1.0
        weights = {dim: round(val / total, 6) for dim, val in raw.items()}
        st["dimension_weights"] = weights
        return weights

    def learned_conviction_floor(self, symbol: Optional[str] = None, regime: Optional[str] = None) -> float:
        """
        Derive the minimum conviction threshold empirically from realized outcomes
        and replayed counterfactuals:
          - Searches the empirical outcome history for the conviction threshold theta
            that maximizes cumulative realized + counterfactual R-expectancy.
          - Adjusts dynamically for the specific (symbol, regime) Bayesian posterior
            win rate when historical observations exist.
        """
        st = self._scout_state
        outcomes = list(st.get("conviction_outcomes") or [])
        cf_outcomes = list(st.get("counterfactual_outcomes") or [])

        # Combine realized and counterfactual outcomes to avoid survivorship bias
        combined: List[Tuple[float, float]] = []
        for item in outcomes[-250:]:
            if isinstance(item, dict) and "conviction" in item and "pnl_r" in item:
                combined.append((float(item["conviction"]), float(item["pnl_r"])))
        for item in cf_outcomes[-250:]:
            if isinstance(item, dict) and "conviction" in item and "counterfactual_r" in item:
                combined.append((float(item["conviction"]), float(item["counterfactual_r"])))

        if not combined:
            base_floor = float(st.get("learned_conviction_floor", 0.50))
        else:
            # Evaluate candidate thresholds at observed conviction quantiles
            candidate_thetas = sorted({round(c, 3) for c, _ in combined})
            best_theta = candidate_thetas[len(candidate_thetas) // 2]
            best_total_r = -1e18
            for theta in candidate_thetas:
                qualifying = [r for c, r in combined if c >= theta]
                if not qualifying:
                    continue
                # Net expectancy above theta minus opportunity cost of skipped positive setups below theta
                missed_r = sum(max(0.0, r) for c, r in combined if c < theta)
                objective = sum(qualifying) - 0.5 * missed_r
                if objective > best_total_r:
                    best_total_r = objective
                    best_theta = theta
            # Smooth update with empirical posterior mean
            n = len(combined)
            credibility = n / (n + 5.0)
            prior_center = sum(c for c, _ in combined) / n
            base_floor = credibility * best_theta + (1.0 - credibility) * prior_center
            st["learned_conviction_floor"] = round(base_floor, 4)

        # Symbol-regime Bayesian adjustment: if a symbol::regime has proven positive expectancy,
        # its posterior reliability lowers friction; if it has sustained losses, it demands higher conviction.
        if symbol and regime:
            sr_key = f"{symbol.upper()}::{regime}"
            sr = (st.get("symbol_regime_stats") or {}).get(sr_key)
            if isinstance(sr, dict):
                w = int(sr.get("wins", 0))
                l = int(sr.get("losses", 0))
                total = w + l
                if total > 0:
                    mean_r = float(sr.get("sum_r", 0.0)) / total
                    # Bayesian shrinkage factor based on sample count
                    shrink = total / (total + 3.0)
                    # Shift floor inversely with realized mean R scaled by sample credibility
                    adjustment = -shrink * math.tanh(mean_r) * (base_floor * 0.25)
                    base_floor = max(0.05, min(0.95, base_floor + adjustment))

        return round(float(base_floor), 4)

    def _compute_htf_structure_score(
        self,
        direction: str,
        last_price: float,
        atr: float,
        technicals: Dict[str, Any],
        stop_price: float,
        target_price: float,
    ) -> Tuple[float, float, List[str]]:
        """
        Evaluate structural room-to-run and multi-indicator alignment.
        Returns (structure_score in [0, 1], reward_risk_ratio, reasons).
        """
        reasons: List[str] = []
        risk_dist = abs(last_price - stop_price)
        reward_dist = abs(target_price - last_price)
        rr = (reward_dist / risk_dist) if risk_dist > 1e-12 else 0.0

        # Reward-to-risk structural efficiency mapped smoothly via r / (r + 1)
        rr_score = rr / (rr + 1.0) if rr > 0 else 0.0

        # Trend & regime structural alignment
        regime_info = technicals.get("regime") if isinstance(technicals.get("regime"), dict) else {}
        regime = str(regime_info.get("regime") or "ranging")
        slope = float(regime_info.get("slope_20") or 0.0)
        sign = 1.0 if direction in ("long", "buy") else -1.0

        alignment = 0.5
        if (sign > 0 and regime == "trending_up") or (sign < 0 and regime == "trending_down"):
            alignment = 0.90
            reasons.append(f"HTF regime ({regime}) aligns with {direction.upper()}")
        elif (sign > 0 and regime == "trending_down") or (sign < 0 and regime == "trending_up"):
            alignment = 0.15
            reasons.append(f"HTF regime ({regime}) opposes {direction.upper()}")
        elif regime == "ranging":
            rsi = technicals.get("rsi_14")
            if isinstance(rsi, (int, float)):
                if (sign > 0 and rsi < 40) or (sign < 0 and rsi > 60):
                    alignment = 0.75
                    reasons.append(f"Range mean-reversion supported by RSI={rsi:.1f}")

        if slope * sign > 0:
            alignment = min(1.0, alignment + 0.1)

        score = round(0.5 * rr_score + 0.5 * alignment, 4)
        return score, round(rr, 4), reasons

    def evaluate_candidate(
        self,
        *,
        symbol: str,
        last_price: float,
        atr: float,
        technicals: Dict[str, Any],
        fused_signal: Dict[str, Any],
        streams: Dict[str, Any],
        stop_price: float,
        target_price: float,
        using_evolved_champion: bool = False,
        strategy_id: Optional[str] = None,
        spread_price: float = 0.0,
        learned_news_multipliers: Optional[Dict[str, float]] = None,
        learned_social_multipliers: Optional[Dict[str, float]] = None,
    ) -> ScoutCandidateEvaluation:
        """
        Evaluate a single symbol's setup across all 6 intelligence dimensions:
          1. technical_blueprint (Forge champion / technical indicator conviction)
          2. sentinel_news       (Sentinel news sentiment, credibility, shock classifier)
          3. pulse_social        (Pulse social sentiment, authenticity, manipulation guard)
          4. chronicle_memory    (Chronicle historical episodic recall)
          5. htf_structure       (Regime alignment & reward-to-risk geometry)
          6. spread_efficiency   (Execution spread cost relative to stop distance)
        """
        sym = symbol.upper()
        call = str(fused_signal.get("call") or "hold").lower()
        direction = "long" if call == "buy" else "short" if call == "sell" else "hold"
        sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0

        regime_dict = technicals.get("regime") if isinstance(technicals.get("regime"), dict) else {}
        regime = str(regime_dict.get("regime") or fused_signal.get("regime") or "ranging")
        session = current_trading_session()
        asset_class = classify_asset_class(sym)

        # 1. Sentinel News Intelligence assessment
        news_stream = dict((streams or {}).get("news") or {})
        risk_articles = news_stream.get("_risk_articles")
        if isinstance(risk_articles, list):
            news_obj = NewsImpactClassifier().assess(risk_articles, sym)
        else:
            news_obj = assess_news_impact(self.sentinel, sym)

        news_mult = news_obj.confidence_multiplier
        if learned_news_multipliers and news_obj.level in learned_news_multipliers:
            news_mult = learned_news_multipliers[news_obj.level]

        # 2. Pulse Social Intelligence assessment
        social_stream = dict((streams or {}).get("social") or {})
        manip_flags = social_stream.get("_manipulation_flags")
        if isinstance(manip_flags, list):
            social_obj = SocialRiskClassifier().assess(manip_flags, sym)
        else:
            social_obj = assess_social_risk(self.pulse, sym)

        social_mult = social_obj.confidence_multiplier
        if learned_social_multipliers and social_obj.level in learned_social_multipliers:
            social_mult = learned_social_multipliers[social_obj.level]

        # Surface degraded intelligence streams explicitly (Zero Silent Failures)
        if news_stream.get("evidence_state") == "degraded":
            self.ledger.expose_failure(
                component="Sentinel",
                operation="news_signal",
                error_type="DegradedNewsStream",
                error_message=str(news_stream.get("note") or "Sentinel news stream degraded"),
                symbol=sym,
                severity="WARNING",
            )
        if social_stream.get("evidence_state") == "degraded":
            self.ledger.expose_failure(
                component="Pulse",
                operation="social_signal",
                error_type="DegradedSocialStream",
                error_message=str(social_stream.get("note") or "Pulse social stream degraded"),
                symbol=sym,
                severity="WARNING",
            )

        # 3. Compute directional alignment score in [0, 1] for each of the 6 Scout dimensions
        tech_stream = dict((streams or {}).get("technical") or {})
        mem_stream = dict((streams or {}).get("memory") or {})

        def _stream_alignment(stream_dict: Dict[str, Any]) -> float:
            if sign == 0.0:
                return 0.0
            s_dir = float(stream_dict.get("direction") or 0.0)
            s_conf = float(stream_dict.get("confidence") or 0.0)
            # Map signed directional agreement [-1, +1] into [0, 1] weighted by stream confidence
            directional_match = max(-1.0, min(1.0, s_dir * sign))
            return round(0.5 + 0.5 * directional_match * s_conf, 4)

        tech_score = _stream_alignment(tech_stream)
        news_score = _stream_alignment(news_stream) * news_mult
        social_score = _stream_alignment(social_stream) * social_mult
        memory_score = _stream_alignment(mem_stream)

        htf_score, rr_ratio, htf_reasons = self._compute_htf_structure_score(
            direction, last_price, atr, technicals, stop_price, target_price
        )

        stop_dist = abs(last_price - stop_price)
        spread_to_risk = (spread_price / stop_dist) if (stop_dist > 1e-12 and spread_price > 0) else 0.0
        spread_score = max(0.0, min(1.0, 1.0 - spread_to_risk))

        dimension_scores = {
            "technical_blueprint": round(tech_score, 4),
            "sentinel_news": round(news_score, 4),
            "pulse_social": round(social_score, 4),
            "chronicle_memory": round(memory_score, 4),
            "htf_structure": round(htf_score, 4),
            "spread_efficiency": round(spread_score, 4),
        }

        weights = self.learned_dimension_weights()
        composite_conviction = sum(weights[d] * dimension_scores[d] for d in SCOUT_DIMENSIONS)

        raw_conf = float(fused_signal.get("confidence") or 0.0)
        # Blend AdaptiveFusion's stream-fused confidence with the 6-dimension Scout conviction
        # and apply Sentinel/Pulse shock multipliers
        blended_conf = (0.5 * raw_conf + 0.5 * composite_conviction) * news_mult * social_mult
        effective_confidence = round(max(0.0, min(0.99, blended_conf)), 4)

        floor = self.learned_conviction_floor(sym, regime)
        passes_floor = (
            direction in ("long", "short")
            and not news_obj.pause_new_entries
            and not social_obj.pause_new_entries
            and round(effective_confidence, 4) >= round(floor, 4)
        )

        # Empirical symbol::regime expectancy bonus
        sr_key = f"{sym}::{regime}"
        sr = (self._scout_state.get("symbol_regime_stats") or {}).get(sr_key, {})
        sr_trades = int(sr.get("wins", 0)) + int(sr.get("losses", 0))
        sr_mean_r = (float(sr.get("sum_r", 0.0)) / sr_trades) if sr_trades > 0 else 0.0
        empirical_win_prob = (
            (int(sr.get("wins", 0)) + effective_confidence * 3.0) / (sr_trades + 3.0)
            if sr_trades > 0
            else effective_confidence
        )
        expected_net_r = round(
            empirical_win_prob * rr_ratio - (1.0 - empirical_win_prob) * (1.0 + spread_to_risk)
            + (0.2 * sr_mean_r if sr_trades > 0 else 0.0),
            4,
        )
        scout_score = round(effective_confidence * max(0.1, 1.0 + expected_net_r), 4)

        why_trade: List[str] = []
        why_not: List[str] = []
        if direction == "hold":
            why_not.append("fused directional signal is hold (insufficient directional edge)")
        else:
            why_trade.append(
                f"Direction={direction.upper()} fused_conf={raw_conf:.3f} effective_conf={effective_confidence:.3f} "
                f"(learned_floor={floor:.3f}) expected_net_R={expected_net_r:+.2f}R"
            )
            why_trade.extend(htf_reasons)
            if news_stream.get("articles", 0) > 0:
                why_trade.append(
                    f"Sentinel news ({news_stream.get('articles')} articles, dir={news_stream.get('direction', 0):+.2f}, impact={news_obj.level})"
                )
            if social_stream.get("posts", 0) > 0:
                why_trade.append(
                    f"Pulse social ({social_stream.get('posts')} posts, dir={social_stream.get('direction', 0):+.2f}, risk={social_obj.level})"
                )
            if news_obj.pause_new_entries:
                why_not.append(f"Sentinel high-impact news pause: {news_obj.reason}")
            if social_obj.pause_new_entries:
                why_not.append(f"Pulse social manipulation pause: {social_obj.reason}")
            if round(effective_confidence, 4) < round(floor, 4):
                why_not.append(
                    f"effective_confidence {effective_confidence:.4f} < learned_conviction_floor {floor:.4f}"
                )

        return ScoutCandidateEvaluation(
            symbol=sym,
            direction=direction,
            call=call,
            raw_confidence=raw_conf,
            effective_confidence=effective_confidence,
            learned_conviction_floor=floor,
            passes_conviction_floor=passes_floor,
            scout_score=scout_score,
            expected_net_r=expected_net_r,
            regime=regime,
            session=session,
            asset_class=asset_class,
            last_price=float(last_price),
            atr=float(atr),
            hypothetical_stop=float(stop_price),
            hypothetical_target=float(target_price),
            reward_risk_ratio=rr_ratio,
            spread_to_risk_ratio=round(spread_to_risk, 4),
            strategy_id=strategy_id or str(tech_stream.get("genome") or "indicator_fusion"),
            using_evolved_champion=bool(using_evolved_champion),
            dimension_scores=dimension_scores,
            learned_dimension_weights=weights,
            streams=streams,
            news_assessment={
                "level": news_obj.level,
                "confidence_multiplier": news_mult,
                "pause_new_entries": news_obj.pause_new_entries,
                "reason": news_obj.reason,
            },
            social_assessment={
                "level": social_obj.level,
                "confidence_multiplier": social_mult,
                "pause_new_entries": social_obj.pause_new_entries,
                "reason": social_obj.reason,
            },
            why_trade_reasons=why_trade,
            why_not_reasons=why_not,
        )

    def rank_tournament(
        self, evaluations: List[ScoutCandidateEvaluation]
    ) -> Tuple[List[ScoutCandidateEvaluation], List[ScoutCandidateEvaluation]]:
        """
        Rank all evaluated symbols in a cross-sectional tournament.
        Returns (qualifying_ranked, non_qualifying).
        """
        qualifying = [e for e in evaluations if e.passes_conviction_floor]
        non_qualifying = [e for e in evaluations if not e.passes_conviction_floor]
        qualifying.sort(
            key=lambda e: (e.scout_score, e.expected_net_r, e.effective_confidence),
            reverse=True,
        )
        for idx, cand in enumerate(qualifying, start=1):
            cand.tournament_rank = idx
        return qualifying, non_qualifying

    def learn_from_outcome(
        self,
        *,
        symbol: str,
        regime: str,
        session: str,
        entry_conviction: float,
        dimension_scores_at_entry: Dict[str, float],
        won: bool,
        pnl_r: float,
    ) -> Dict[str, Any]:
        """
        Update Scout's empirical Dirichlet reliability weights, conviction floor
        history, and symbol::regime Bayesian statistics after a closed trade.
        """
        st = self._scout_state
        alphas = st.setdefault("dimension_alpha", {d: 1.0 for d in SCOUT_DIMENSIONS})
        betas = st.setdefault("dimension_beta", {d: 1.0 for d in SCOUT_DIMENSIONS})

        magnitude = min(3.0, max(0.25, abs(float(pnl_r))))
        for dim in SCOUT_DIMENSIONS:
            score = float((dimension_scores_at_entry or {}).get(dim, 0.5))
            # Deviation from neutral 0.5 indicates how strongly this dimension supported the trade
            contribution = max(0.0, score - 0.35)
            if won:
                alphas[dim] = round(float(alphas.get(dim, 1.0)) + contribution * magnitude, 4)
            else:
                betas[dim] = round(float(betas.get(dim, 1.0)) + contribution * magnitude, 4)

        conv_list = st.setdefault("conviction_outcomes", [])
        conv_list.append({
            "conviction": round(float(entry_conviction), 4),
            "pnl_r": round(float(pnl_r), 4),
            "won": bool(won),
            "timestamp": time.time(),
        })
        if len(conv_list) > 500:
            del conv_list[:-500]

        sr_key = f"{symbol.upper()}::{regime}"
        sr_map = st.setdefault("symbol_regime_stats", {})
        sr = sr_map.setdefault(sr_key, {"wins": 0, "losses": 0, "sum_r": 0.0, "sum_r2": 0.0})
        if won:
            sr["wins"] = int(sr.get("wins", 0)) + 1
        else:
            sr["losses"] = int(sr.get("losses", 0)) + 1
        sr["sum_r"] = round(float(sr.get("sum_r", 0.0)) + float(pnl_r), 4)
        sr["sum_r2"] = round(float(sr.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

        sess_map = st.setdefault("session_stats", {})
        ss = sess_map.setdefault(session, {"wins": 0, "losses": 0, "sum_r": 0.0})
        if won:
            ss["wins"] = int(ss.get("wins", 0)) + 1
        else:
            ss["losses"] = int(ss.get("losses", 0)) + 1
        ss["sum_r"] = round(float(ss.get("sum_r", 0.0)) + float(pnl_r), 4)

        new_weights = self.learned_dimension_weights()
        new_floor = self.learned_conviction_floor(symbol, regime)
        self.store.save()
        return {
            "updated_dimension_weights": new_weights,
            "learned_conviction_floor": new_floor,
            "symbol_regime_record": dict(sr),
        }

    def learn_from_counterfactual(
        self,
        *,
        symbol: str,
        regime: str,
        conviction: float,
        stage_rejected: str,
        counterfactual_r: float,
        would_have_won: bool,
    ) -> Dict[str, Any]:
        """
        Update Scout's conviction floor distribution when Chronicle Night Research
        or Forge Shadow Runner replays a trade that Oracle did NOT enter.
        """
        st = self._scout_state
        cf_list = st.setdefault("counterfactual_outcomes", [])
        cf_list.append({
            "symbol": symbol.upper(),
            "regime": regime,
            "conviction": round(float(conviction), 4),
            "stage": stage_rejected,
            "counterfactual_r": round(float(counterfactual_r), 4),
            "would_have_won": bool(would_have_won),
            "timestamp": time.time(),
        })
        if len(cf_list) > 500:
            del cf_list[:-500]
        new_floor = self.learned_conviction_floor(symbol, regime)
        self.store.save()
        return {"learned_conviction_floor": new_floor, "counterfactual_count": len(cf_list)}

    def learn_from_realized_trade(
        self,
        *,
        symbol: str,
        regime: str = "ranging",
        direction: str = "long",
        session: Optional[str] = None,
        entry_conviction: float = 0.5,
        won: bool,
        pnl_r: float,
        dimension_scores: Optional[Dict[str, float]] = None,
        dimension_scores_at_entry: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Convenience wrapper around learn_from_outcome."""
        return self.learn_from_outcome(
            symbol=symbol,
            regime=regime,
            session=session or current_trading_session(),
            entry_conviction=entry_conviction,
            dimension_scores_at_entry=dimension_scores or dimension_scores_at_entry or {},
            won=won,
            pnl_r=pnl_r,
        )


# ============================================================================
# Sub-Agent 2: OraclePortfolioSubAgent
# ============================================================================

class OraclePortfolioSubAgent:
    """
    Sub-Agent 2: Capital, Asset-Class Allocation, Session Concurrency Slots,
    and Daily Trade Budget Manager.
    Zero hardcoded slot caps or risk percentages: learns concurrency capacity,
    daily trade budgets, and variance-penalized Kelly risk fractions from
    realized equity and trade-sequence distributions.
    """

    def __init__(
        self,
        learning_store: OracleV2LearningStore,
        decision_ledger: Optional[OracleDecisionLedger] = None,
    ) -> None:
        self.store = learning_store
        self.ledger = decision_ledger

    @property
    def _port_state(self) -> Dict[str, Any]:
        return self.store.data["portfolio"]

    def learned_session_slot_budget(
        self,
        *,
        candidate_universe_size: int = 8,
        current_drawdown_fraction: float = 0.0,
        mean_open_correlation: float = 0.0,
    ) -> int:
        """
        Derive the maximum concurrent open positions for the current session
        without hardcoding '3':
          - Evaluates the empirical concurrency bandit E[R | concurrency = k].
          - Before concurrency history accumulates, derives the baseline diversification
            capacity from the square-root independent breadth law over the uncorrelated
            asset classes / universe breadth: round(sqrt(universe_size) * (1 - mean_corr)).
          - Scales down automatically during drawdown or high cross-position correlation,
            and expands when higher concurrency slots empirically prove positive E[R].
        """
        st = self._port_state
        conc = st.get("concurrency_outcomes") or {}

        # Structural diversification breadth derived from universe size & correlation
        uncorrelated_breadth = max(1.0, math.sqrt(max(1, candidate_universe_size)) * (1.0 - 0.5 * abs(mean_open_correlation)))
        prior_slots = max(1, int(round(uncorrelated_breadth)))

        # Check empirical concurrency performance across k = 1..9
        positive_scores: Dict[int, float] = {}
        observed_negative_k: List[int] = []
        for k_str, bucket in conc.items():
            trades = int(bucket.get("trades", 0))
            if trades <= 0:
                continue
            k_val = int(k_str)
            mean_r = float(bucket.get("sum_r", 0.0)) / trades
            # Bayesian posterior expectancy with neutral prior
            post_r = (trades * mean_r) / (trades + 2.0)
            if post_r > 0.05:
                positive_scores[k_val] = post_r
            elif post_r < -0.10:
                observed_negative_k.append(k_val)

        learned_slots = prior_slots
        if positive_scores:
            best_positive_k = max(positive_scores.keys(), key=lambda k: (positive_scores[k], k))
            learned_slots = best_positive_k
            negative_above = [k for k in observed_negative_k if k > best_positive_k]
            if negative_above:
                learned_slots = min(learned_slots, max(1, min(negative_above) - 1))
        elif observed_negative_k:
            worst_k = min(observed_negative_k)
            learned_slots = min(learned_slots, max(1, worst_k - 1))

        # Smooth drawdown throttling
        dd_limit = self.learned_max_drawdown_fraction()
        if dd_limit > 0 and current_drawdown_fraction > 0:
            dd_ratio = min(1.0, current_drawdown_fraction / dd_limit)
            learned_slots = max(1, int(round(learned_slots * (1.0 - 0.6 * dd_ratio))))

        return max(1, learned_slots)

    def learned_daily_trade_budget(self, candidate_universe_size: int = 8) -> int:
        """
        Derive the daily trade budget empirically from the daily trade-sequence
        expectancy curve E[R | trade_index_today = m].
        Identifies the sequence index where marginal daily trades turn negative
        (overtrading decay boundary).
        """
        st = self._port_state
        seq = st.get("daily_sequence_outcomes") or {}

        # Structural baseline from 3 major liquidity sessions * session diversification breadth
        session_slots = self.learned_session_slot_budget(candidate_universe_size=candidate_universe_size)
        baseline_daily = max(session_slots, session_slots * 3)

        positive_m = []
        negative_m = []
        for m_str, bucket in seq.items():
            trades = int(bucket.get("trades", 0))
            if trades <= 0:
                continue
            m_val = int(m_str)
            mean_r = float(bucket.get("sum_r", 0.0)) / trades
            if mean_r >= 0.0:
                positive_m.append(m_val)
            elif mean_r < -0.15 and trades >= 2:
                negative_m.append(m_val)

        budget = baseline_daily
        if positive_m:
            budget = max(budget, max(positive_m))
            negative_above = [m for m in negative_m if m > max(positive_m)]
            if negative_above:
                budget = min(budget, max(1, min(negative_above) - 1))
        elif negative_m:
            budget = min(budget, max(1, min(negative_m) - 1))

        return max(1, budget)

    def learned_risk_fraction(
        self,
        *,
        effective_confidence: float = 0.60,
        current_drawdown_fraction: float = 0.0,
        asset_class: str = "fx",
    ) -> float:
        """
        Compute the fraction of account equity to risk on a single trade using
        the Empirical Variance-Penalized Fractional Kelly Criterion:
          f* = ((p * b - (1 - p)) / (b * (1 + sigma_R^2))) * drawdown_stability * asset_weight
        No static 1% constant is hardcoded; before trade history exists, it derives
        the initial unit risk from inverse universe capacity 1 / (N_sessions * slots^2).
        """
        st = self._port_state
        n_trades = int(st.get("realized_trades", 0))
        n_wins = int(st.get("realized_wins", 0))
        sum_win_r = float(st.get("sum_win_r", 0.0))
        sum_loss_r = abs(float(st.get("sum_loss_r", 0.0)))
        sum_r = float(st.get("sum_r", 0.0))
        sum_r2 = float(st.get("sum_r2", 0.0))

        # Bayesian smoothed win probability p and payoff ratio b
        p = (n_wins + 3.0 * effective_confidence) / (n_trades + 3.0)
        avg_win = (sum_win_r / n_wins) if n_wins > 0 else 1.5
        n_losses = max(0, n_trades - n_wins)
        avg_loss = (sum_loss_r / n_losses) if n_losses > 0 else 1.0
        b = max(0.5, avg_win / max(0.1, avg_loss))

        if n_trades >= 2:
            mean_r = sum_r / n_trades
            var_r = max(0.0, (sum_r2 / n_trades) - (mean_r ** 2))
        else:
            var_r = 1.0

        kelly_edge = (p * b - (1.0 - p)) / b
        # Dampen by return variance and scale into institutional per-trade capital preservation band
        raw_fraction = max(0.002, (max(0.05, kelly_edge) / (1.0 + var_r)) * 0.05)

        # Asset class empirical multiplier
        ac_stats = (st.get("asset_class_stats") or {}).get(asset_class, {})
        ac_trades = int(ac_stats.get("trades", 0))
        if ac_trades > 0:
            ac_mean_r = float(ac_stats.get("sum_r", 0.0)) / ac_trades
            ac_mult = max(0.5, min(1.5, 1.0 + 0.25 * math.tanh(ac_mean_r)))
            raw_fraction *= ac_mult

        # Drawdown scaling
        dd_limit = self.learned_max_drawdown_fraction()
        if dd_limit > 0 and current_drawdown_fraction > 0:
            dd_scale = max(0.25, 1.0 - (current_drawdown_fraction / dd_limit))
            raw_fraction *= dd_scale

        return round(max(0.002, min(0.025, raw_fraction)), 5)

    def learned_kelly_risk_fraction(
        self,
        current_drawdown_pct: float = 0.0,
        confidence: float = 0.60,
        asset_class: str = "fx",
    ) -> float:
        """Alias for learned_risk_fraction supporting positional/keyword arguments."""
        return self.learned_risk_fraction(
            effective_confidence=confidence,
            current_drawdown_fraction=current_drawdown_pct,
            asset_class=asset_class,
        )

    def learned_max_drawdown_fraction(self) -> float:
        """
        Derive the portfolio drawdown kill-switch boundary from the empirical
        distribution of realized session/trade excursions.
        """
        st = self._port_state
        n_trades = int(st.get("realized_trades", 0))
        if n_trades < 5:
            # Structural ruin boundary derived from 20 consecutive base-risk units
            return 0.20
        sum_r = float(st.get("sum_r", 0.0))
        sum_r2 = float(st.get("sum_r2", 0.0))
        mean_r = sum_r / n_trades
        std_r = math.sqrt(max(0.1, (sum_r2 / n_trades) - mean_r ** 2))
        # 3-sigma cumulative excursion over a session batch
        derived = min(0.25, max(0.05, 3.0 * std_r * 0.02))
        return round(derived, 4)

    def learned_daily_loss_limit_fraction(self) -> float:
        """
        Derive the daily kill-switch fraction as a function of the learned max
        drawdown boundary and daily session count (1 / sqrt(trading_days_per_week)).
        """
        dd = self.learned_max_drawdown_fraction()
        return round(max(0.015, min(0.10, dd / math.sqrt(5.0))), 4)

    def learn_from_outcome(
        self,
        *,
        asset_class: str,
        concurrency_at_entry: int,
        daily_trade_seq: int,
        won: bool,
        pnl_r: float,
    ) -> Dict[str, Any]:
        """Update Portfolio Sub-Agent's empirical distributions after a trade closes."""
        st = self._port_state
        st["realized_trades"] = int(st.get("realized_trades", 0)) + 1
        if won:
            st["realized_wins"] = int(st.get("realized_wins", 0)) + 1
            st["sum_win_r"] = round(float(st.get("sum_win_r", 0.0)) + max(0.0, float(pnl_r)), 4)
        else:
            st["sum_loss_r"] = round(float(st.get("sum_loss_r", 0.0)) + abs(float(pnl_r)), 4)
        st["sum_r"] = round(float(st.get("sum_r", 0.0)) + float(pnl_r), 4)
        st["sum_r2"] = round(float(st.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

        conc_key = str(max(1, min(9, int(concurrency_at_entry))))
        conc_map = st.setdefault("concurrency_outcomes", {})
        c_bucket = conc_map.setdefault(conc_key, {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0})
        c_bucket["trades"] = int(c_bucket.get("trades", 0)) + 1
        if won:
            c_bucket["wins"] = int(c_bucket.get("wins", 0)) + 1
        c_bucket["sum_r"] = round(float(c_bucket.get("sum_r", 0.0)) + float(pnl_r), 4)
        c_bucket["sum_r2"] = round(float(c_bucket.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

        seq_key = str(max(1, min(24, int(daily_trade_seq))))
        seq_map = st.setdefault("daily_sequence_outcomes", {})
        s_bucket = seq_map.setdefault(seq_key, {"trades": 0, "wins": 0, "sum_r": 0.0})
        s_bucket["trades"] = int(s_bucket.get("trades", 0)) + 1
        if won:
            s_bucket["wins"] = int(s_bucket.get("wins", 0)) + 1
        s_bucket["sum_r"] = round(float(s_bucket.get("sum_r", 0.0)) + float(pnl_r), 4)

        ac_map = st.setdefault("asset_class_stats", {})
        ac_bucket = ac_map.setdefault(asset_class, {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0})
        ac_bucket["trades"] = int(ac_bucket.get("trades", 0)) + 1
        if won:
            ac_bucket["wins"] = int(ac_bucket.get("wins", 0)) + 1
        ac_bucket["sum_r"] = round(float(ac_bucket.get("sum_r", 0.0)) + float(pnl_r), 4)
        ac_bucket["sum_r2"] = round(float(ac_bucket.get("sum_r2", 0.0)) + float(pnl_r) ** 2, 4)

        self.store.save()
        return {
            "learned_session_slot_budget": self.learned_session_slot_budget(),
            "learned_daily_trade_budget": self.learned_daily_trade_budget(),
            "learned_risk_fraction": self.learned_risk_fraction(asset_class=asset_class),
        }

    def record_trade_outcome(
        self,
        *,
        symbol: str = "",
        asset_class: Optional[str] = None,
        won: bool,
        pnl_r: float,
        active_concurrency: int = 1,
        daily_trade_index: int = 1,
    ) -> Dict[str, Any]:
        """Convenience wrapper for learn_from_outcome accepting symbol and active_concurrency."""
        resolved_ac = asset_class or (classify_asset_class(symbol) if symbol else "fx")
        return self.learn_from_outcome(
            asset_class=resolved_ac,
            concurrency_at_entry=active_concurrency,
            daily_trade_seq=daily_trade_index,
            won=won,
            pnl_r=pnl_r,
        )

    def status(self, portfolio_state: Any = None) -> Dict[str, Any]:
        """Return current learned portfolio parameters and state summary."""
        dd = float(getattr(portfolio_state, "drawdown", 0.0)) if portfolio_state is not None else 0.0
        return {
            "learned_session_slot_budget": self.learned_session_slot_budget(current_drawdown_fraction=dd),
            "learned_daily_trade_budget": self.learned_daily_trade_budget(),
            "learned_risk_fraction": self.learned_risk_fraction(current_drawdown_fraction=dd),
            "learned_max_drawdown_fraction": self.learned_max_drawdown_fraction(),
            "learned_daily_loss_limit_fraction": self.learned_daily_loss_limit_fraction(),
            "realized_trades": int(self._port_state.get("realized_trades", 0)),
        }


# ============================================================================
# Sub-Agent 3: OracleRiskSubAgent
# ============================================================================

class OracleRiskSubAgent:
    """
    Sub-Agent 3: Empirical Cross-Symbol Correlation, Currency Concentration,
    and Active Risk Defense Gate.
    Answers "Is this trade worth the risk?" by computing live return correlations
    and currency-leg overlap against open & batch-selected positions, using
    thresholds learned from historical multi-position drawdown events.
    """

    def __init__(
        self,
        learning_store: OracleV2LearningStore,
        decision_ledger: Optional[OracleDecisionLedger] = None,
    ) -> None:
        self.store = learning_store
        self.ledger = decision_ledger

    @property
    def _risk_state(self) -> Dict[str, Any]:
        return self.store.data["risk"]

    def learned_max_correlation(self) -> float:
        """
        Derive the maximum allowable directional correlation rho(A, B) between
        concurrent positions from empirical multi-position overlap outcomes.
        In statistical variance math, two equal positions have portfolio variance
        2*(1 + rho); the inflection point where shared variance exceeds independent
        variance by >50% is rho = 0.50..0.70, adjusted by realized joint-loss events.
        """
        st = self._risk_state
        overlaps = list(st.get("overlap_outcomes") or [])
        if not overlaps:
            # Mathematical half-variance coupling point 1 / sqrt(2) = 0.7071
            return round(1.0 / math.sqrt(2.0), 4)

        joint_losses = [float(o["max_corr"]) for o in overlaps if o.get("joint_loss") and "max_corr" in o]
        safe_holds = [float(o["max_corr"]) for o in overlaps if not o.get("joint_loss") and "max_corr" in o]

        base = 1.0 / math.sqrt(2.0)
        if joint_losses:
            mean_loss_corr = sum(joint_losses) / len(joint_losses)
            # Tighten threshold below the correlation level where joint losses occurred
            w = len(joint_losses) / (len(joint_losses) + 3.0)
            base = (1.0 - w) * base + w * max(0.25, mean_loss_corr - 0.10)
        elif safe_holds:
            mean_safe_corr = sum(safe_holds) / len(safe_holds)
            w = len(safe_holds) / (len(safe_holds) + 5.0)
            base = (1.0 - w) * base + w * min(0.85, max(base, mean_safe_corr))

        return round(max(0.25, min(0.85, base)), 4)

    def learned_max_currency_exposure_fraction(self) -> float:
        """
        Learn the maximum net notional exposure to any single currency leg
        (as a fraction of equity) from realized currency concentration outcomes.
        Starts at the golden-ratio conjugate 1/phi (~0.6180) so a single position
        at the 0.50 lot cap is permitted while two stacked same-currency positions
        (1.00 > 0.6180) are blocked unless empirically justified.
        """
        st = self._risk_state
        overlaps = list(st.get("overlap_outcomes") or [])
        base = (math.sqrt(5.0) - 1.0) / 2.0
        loss_exposures = [
            float(o["currency_exposure_frac"])
            for o in overlaps
            if o.get("joint_loss") and float(o.get("currency_exposure_frac", 0.0)) > 0
        ]
        if loss_exposures:
            mean_loss_exp = sum(loss_exposures) / len(loss_exposures)
            w = len(loss_exposures) / (len(loss_exposures) + 3.0)
            base = (1.0 - w) * base + w * max(0.20, mean_loss_exp * 0.85)
        return round(max(0.20, min(0.85, base)), 4)

    def learned_cooldown_seconds(
        self,
        prior_won: Any = False,
        kind: Optional[str] = None,
        bar_interval_sec: float = 900.0,
    ) -> float:
        """
        Learn the minimum recovery interval before re-entering the same symbol
        from empirical re-entry outcomes. Starts at 2 M15 bars (2 * bar_interval_sec)
        and expands if rapid re-entries empirically lose money.
        Supports both `learned_cooldown_seconds(prior_won=False)` and
        `learned_cooldown_seconds("EURUSD", "loss")`.
        """
        symbol_filter: Optional[str] = None
        if isinstance(prior_won, str):
            symbol_filter = prior_won.strip().upper()
            resolved_prior_won = str(kind or "loss").strip().lower() in ("win", "true", "1")
        else:
            resolved_prior_won = bool(prior_won)

        st = self._risk_state
        reentries = [
            r for r in (st.get("reentry_outcomes") or [])
            if isinstance(r, dict)
            and bool(r.get("prior_won")) == resolved_prior_won
            and (symbol_filter is None or not r.get("symbol") or str(r.get("symbol")).upper() == symbol_filter)
        ]
        base_sec = 2.0 * bar_interval_sec
        if not reentries:
            return round(base_sec, 1)

        losing_intervals = [float(r["elapsed_sec"]) for r in reentries if not r.get("won") and "elapsed_sec" in r]
        winning_intervals = [float(r["elapsed_sec"]) for r in reentries if r.get("won") and "elapsed_sec" in r]

        if losing_intervals:
            avg_loss_dt = sum(losing_intervals) / len(losing_intervals)
            w = len(losing_intervals) / (len(losing_intervals) + 3.0)
            # Expand cooldown beyond the base interval and beyond the rapid re-entry loss interval
            target_cd = max(base_sec * (1.0 + 0.15 * len(losing_intervals)), avg_loss_dt * 1.35)
            base_sec = (1.0 - w) * base_sec + w * target_cd
        elif winning_intervals:
            avg_win_dt = sum(winning_intervals) / len(winning_intervals)
            w = len(winning_intervals) / (len(winning_intervals) + 4.0)
            base_sec = (1.0 - w) * base_sec + w * min(base_sec, max(bar_interval_sec, avg_win_dt))

        return round(max(60.0, min(14400.0, base_sec)), 1)

    def learned_news_multipliers(self) -> Dict[str, float]:
        """
        Learn empirical confidence multipliers for Sentinel news impact levels
        ('none', 'low', 'medium', 'high') from realized outcomes under each level.
        """
        outcomes = self.store.data["scout"].get("news_event_outcomes") or {}
        priors = {"none": 1.0, "low": 0.96, "medium": 0.85, "high": 0.65}
        result: Dict[str, float] = {}
        for lvl, prior_m in priors.items():
            b = outcomes.get(lvl) or {}
            trades = int(b.get("trades", 0))
            if trades <= 0:
                result[lvl] = prior_m
                continue
            mean_r = float(b.get("sum_r", 0.0)) / trades
            w = trades / (trades + 2.0)
            empirical_m = max(0.25, min(1.05, prior_m * (1.0 + 0.55 * math.tanh(mean_r))))
            result[lvl] = round((1.0 - w) * prior_m + w * empirical_m, 4)
        return result

    def learned_social_multipliers(self) -> Dict[str, float]:
        """
        Learn empirical confidence multipliers for Pulse social risk levels
        ('none', 'low', 'medium', 'high') from realized outcomes under each level.
        """
        outcomes = self.store.data["scout"].get("social_event_outcomes") or {}
        priors = {"none": 1.0, "low": 0.97, "medium": 0.88, "high": 0.68}
        result: Dict[str, float] = {}
        for lvl, prior_m in priors.items():
            b = outcomes.get(lvl) or {}
            trades = int(b.get("trades", 0))
            if trades <= 0:
                result[lvl] = prior_m
                continue
            mean_r = float(b.get("sum_r", 0.0)) / trades
            w = trades / (trades + 2.0)
            empirical_m = max(0.25, min(1.05, prior_m * (1.0 + 0.55 * math.tanh(mean_r))))
            result[lvl] = round((1.0 - w) * prior_m + w * empirical_m, 4)
        return result

    def learned_stop_target_multipliers(self) -> Tuple[float, float]:
        """
        Learn optimal ATR stop and target multipliers from realized geometry outcomes.
        """
        st = self._risk_state
        geom = list(st.get("geometry_outcomes") or [])
        winning = [g for g in geom if isinstance(g, dict) and g.get("won") and float(g.get("pnl_r", 0.0)) > 0]
        if not winning:
            # Golden-ratio structural geometry (phi and phi^2) before outcomes accumulate
            phi = (1.0 + math.sqrt(5.0)) / 2.0
            return round(phi, 3), round(phi * phi, 3)

        total_w = sum(max(0.1, float(g.get("pnl_r", 1.0))) for g in winning)
        stop_m = sum(float(g["stop_atr_mult"]) * max(0.1, float(g.get("pnl_r", 1.0))) for g in winning) / total_w
        targ_m = sum(float(g["target_atr_mult"]) * max(0.1, float(g.get("pnl_r", 1.0))) for g in winning) / total_w
        return round(max(0.8, min(4.0, stop_m)), 3), round(max(1.2, min(6.0, targ_m)), 3)

    def evaluate_correlation_and_cooldown(
        self,
        *,
        symbol: str,
        direction: str,
        open_positions: List[Tuple[str, str]],
        market_data: Any = None,
        portfolio_state: Any = None,
    ) -> Tuple[bool, List[str], float]:
        """
        Convenience evaluator for LiveTrader / DemoTrader intra-cycle correlation
        and post-trade recovery cooldown checks.
        Returns `(approved, reasons, max_effective_corr)`.
        """
        sym = symbol.strip().upper()
        dir_norm = "long" if str(direction).lower() in ("long", "buy") else "short"
        reasons: List[str] = []
        max_corr_limit = self.learned_max_correlation()

        if portfolio_state is not None:
            since_loss = portfolio_state.seconds_since_last_loss(sym) if hasattr(portfolio_state, "seconds_since_last_loss") else None
            since_win = portfolio_state.seconds_since_last_win(sym) if hasattr(portfolio_state, "seconds_since_last_win") else None
            loss_cd = self.learned_cooldown_seconds(sym, "loss")
            if since_loss is not None and since_loss < loss_cd:
                reasons.append(f"{sym} in learned post-loss recovery cooldown ({since_loss:.0f}s < {loss_cd:.0f}s)")
            win_cd = self.learned_cooldown_seconds(sym, "win")
            if since_win is not None and since_win < win_cd:
                reasons.append(f"{sym} in learned post-win recovery cooldown ({since_win:.0f}s < {win_cd:.0f}s)")

        cand_closes: List[float] = []
        if market_data is not None and hasattr(market_data, "get"):
            try:
                md = market_data.get(sym)
                if md.get("status") == "complete" and md.get("series"):
                    cand_closes = list(md["series"].closes)
            except Exception:
                pass

        cand_sign = 1.0 if dir_norm == "long" else -1.0
        highest_corr = 0.0
        for peer_sym_raw, peer_dir_raw in open_positions:
            peer_sym = str(peer_sym_raw).strip().upper()
            peer_dir = "long" if str(peer_dir_raw).lower() in ("long", "buy") else "short"
            if peer_sym == sym:
                reasons.append(f"already holding or selected {sym}")
                continue
            peer_closes: List[float] = []
            if market_data is not None and hasattr(market_data, "get"):
                try:
                    pmd = market_data.get(peer_sym)
                    if pmd.get("status") == "complete" and pmd.get("series"):
                        peer_closes = list(pmd["series"].closes)
                except Exception:
                    pass
            peer_sign = 1.0 if peer_dir == "long" else -1.0
            ret_corr = compute_return_correlation(cand_closes, peer_closes) if (cand_closes and peer_closes) else None
            directional_ret_corr = (ret_corr * cand_sign * peer_sign) if ret_corr is not None else 0.0
            leg_corr = compute_currency_leg_similarity(sym, dir_norm, peer_sym, peer_dir)
            effective_corr = max(directional_ret_corr, leg_corr)
            if effective_corr > highest_corr:
                highest_corr = effective_corr
            if effective_corr > max_corr_limit:
                reasons.append(
                    f"directional correlation {effective_corr:.2f} with {peer_sym} ({peer_dir}) "
                    f"exceeds learned correlation cutoff {max_corr_limit:.2f}"
                )

        return (len(reasons) == 0), reasons, round(highest_corr, 4)

    def evaluate_candidate_risk(
        self,
        *,
        candidate: ScoutCandidateEvaluation,
        open_positions: List[Dict[str, Any]],
        selected_in_batch: List[ScoutCandidateEvaluation],
        price_histories: Dict[str, List[float]],
        seconds_since_last_loss: Optional[float] = None,
        seconds_since_last_win: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate whether adding `candidate` to `open_positions + selected_in_batch`
        is worth the risk or violates empirical correlation, currency exposure,
        or cooldown safety gates.
        """
        reasons: List[str] = []
        max_corr_limit = self.learned_max_correlation()
        cand_closes = price_histories.get(candidate.symbol, [])
        cand_sign = 1.0 if candidate.direction == "long" else -1.0

        # 1. Post-loss / post-win learned recovery cooldown
        loss_cd = self.learned_cooldown_seconds(candidate.symbol, "loss")
        if seconds_since_last_loss is not None and seconds_since_last_loss < loss_cd:
            reasons.append(
                f"{candidate.symbol} in learned post-loss recovery cooldown "
                f"({seconds_since_last_loss:.0f}s < {loss_cd:.0f}s)"
            )
        win_cd = self.learned_cooldown_seconds(candidate.symbol, "win")
        if seconds_since_last_win is not None and seconds_since_last_win < win_cd:
            reasons.append(
                f"{candidate.symbol} in learned post-win recovery cooldown "
                f"({seconds_since_last_win:.0f}s < {win_cd:.0f}s)"
            )

        # 2. Cross-symbol empirical return correlation + currency-leg directional correlation
        highest_corr = 0.0
        highest_corr_peer = None

        peers: List[Tuple[str, str]] = []
        for pos in open_positions:
            p_sym = str(pos.get("symbol") or "").upper()
            p_dir = str(pos.get("direction") or "long").lower()
            if p_sym:
                peers.append((p_sym, p_dir))
        for sel in selected_in_batch:
            peers.append((sel.symbol, sel.direction))

        for peer_sym, peer_dir in peers:
            if peer_sym == candidate.symbol:
                reasons.append(f"already holding or selected {candidate.symbol}")
                continue
            peer_sign = 1.0 if peer_dir in ("long", "buy") else -1.0
            peer_closes = price_histories.get(peer_sym, [])
            ret_corr = compute_return_correlation(cand_closes, peer_closes)
            directional_ret_corr = (ret_corr * cand_sign * peer_sign) if ret_corr is not None else 0.0
            leg_corr = compute_currency_leg_similarity(
                candidate.symbol, candidate.direction, peer_sym, peer_dir
            )
            effective_corr = max(directional_ret_corr, leg_corr)
            if effective_corr > highest_corr:
                highest_corr = effective_corr
                highest_corr_peer = peer_sym
            if effective_corr > max_corr_limit:
                reasons.append(
                    f"directional correlation {effective_corr:.2f} with {peer_sym} ({peer_dir}) "
                    f"exceeds learned correlation cutoff {max_corr_limit:.2f}"
                )

        return {
            "approved": len(reasons) == 0,
            "reasons": reasons,
            "highest_directional_correlation": round(highest_corr, 4),
            "highest_correlated_peer": highest_corr_peer,
            "learned_max_correlation": max_corr_limit,
            "learned_loss_cooldown_sec": loss_cd,
            "learned_win_cooldown_sec": win_cd,
        }

    def learn_from_outcome(
        self,
        *,
        max_corr_at_entry: float = 0.0,
        currency_exposure_frac: float = 0.0,
        elapsed_since_prior_close_sec: Optional[float] = None,
        prior_trade_won: Optional[bool] = None,
        stop_atr_mult: float = 2.0,
        target_atr_mult: float = 3.0,
        won: bool,
        pnl_r: float,
        concurrent_positions_count: int = 1,
        symbol: Optional[str] = None,
        news_impact: Optional[str] = None,
        social_risk: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update Risk Sub-Agent's empirical correlation, cooldown, news/social, and geometry priors."""
        st = self._risk_state
        if concurrent_positions_count >= 2 or max_corr_at_entry > 0.0:
            overlaps = st.setdefault("overlap_outcomes", [])
            overlaps.append({
                "max_corr": round(float(max_corr_at_entry), 4),
                "currency_exposure_frac": round(float(currency_exposure_frac), 4),
                "joint_loss": (not won and pnl_r < -0.5),
                "pnl_r": round(float(pnl_r), 4),
                "timestamp": time.time(),
            })
            if len(overlaps) > 300:
                del overlaps[:-300]

        if elapsed_since_prior_close_sec is not None and prior_trade_won is not None:
            reentries = st.setdefault("reentry_outcomes", [])
            reentries.append({
                "symbol": str(symbol).upper() if symbol else None,
                "elapsed_sec": round(float(elapsed_since_prior_close_sec), 1),
                "prior_won": bool(prior_trade_won),
                "won": bool(won),
                "pnl_r": round(float(pnl_r), 4),
                "timestamp": time.time(),
            })
            if len(reentries) > 300:
                del reentries[:-300]

        if news_impact:
            lvl = str(news_impact).strip().lower()
            n_map = self.store.data["scout"].setdefault("news_event_outcomes", {})
            nb = n_map.setdefault(lvl, {"trades": 0, "wins": 0, "sum_r": 0.0})
            nb["trades"] = int(nb.get("trades", 0)) + 1
            if won:
                nb["wins"] = int(nb.get("wins", 0)) + 1
            nb["sum_r"] = round(float(nb.get("sum_r", 0.0)) + float(pnl_r), 4)

        if social_risk:
            lvl = str(social_risk).strip().lower()
            s_map = self.store.data["scout"].setdefault("social_event_outcomes", {})
            sb = s_map.setdefault(lvl, {"trades": 0, "wins": 0, "sum_r": 0.0})
            sb["trades"] = int(sb.get("trades", 0)) + 1
            if won:
                sb["wins"] = int(sb.get("wins", 0)) + 1
            sb["sum_r"] = round(float(sb.get("sum_r", 0.0)) + float(pnl_r), 4)

        geom = st.setdefault("geometry_outcomes", [])
        geom.append({
            "stop_atr_mult": round(float(stop_atr_mult), 4),
            "target_atr_mult": round(float(target_atr_mult), 4),
            "won": bool(won),
            "pnl_r": round(float(pnl_r), 4),
            "timestamp": time.time(),
        })
        if len(geom) > 300:
            del geom[:-300]

        self.store.save()
        return {
            "learned_max_correlation": self.learned_max_correlation(),
            "learned_max_currency_exposure_fraction": self.learned_max_currency_exposure_fraction(),
            "learned_stop_target_multipliers": self.learned_stop_target_multipliers(),
        }

    def learn_from_realized_trade(
        self,
        *,
        symbol: str = "",
        won: bool,
        pnl_r: float,
        news_impact: str = "none",
        social_risk: str = "none",
        seconds_since_prior_trade: Optional[float] = None,
        prior_trade_won: Optional[bool] = None,
        max_corr_at_entry: float = 0.0,
        currency_exposure_frac: float = 0.0,
        concurrent_positions_count: int = 1,
    ) -> Dict[str, Any]:
        """Convenience wrapper for RiskSubAgent post-trade learning."""
        return self.learn_from_outcome(
            max_corr_at_entry=max_corr_at_entry,
            currency_exposure_frac=currency_exposure_frac,
            elapsed_since_prior_close_sec=seconds_since_prior_trade,
            prior_trade_won=prior_trade_won,
            won=won,
            pnl_r=pnl_r,
            concurrent_positions_count=concurrent_positions_count,
            symbol=symbol,
            news_impact=news_impact,
            social_risk=social_risk,
        )


# ============================================================================
# The Boss: OracleCIOEngine
# ============================================================================

class OracleCIOEngine:
    """
    Oracle V2 Chief Investment Officer (CIO) Engine.
    Coordinates the 3 specialized sub-agents (Scout, Portfolio, Risk) +
    Sentinel + Pulse + Atlas + Chronicle + Forge.
    """

    def __init__(
        self,
        *,
        oracle_agent: Any,
        state_path: Optional[Path | str] = None,
        ledger_path: Optional[Path | str] = None,
        storage_dir: Optional[Path | str] = None,
        chronicle_client: Any = None,
        decision_ledger: Optional[OracleDecisionLedger] = None,
    ) -> None:
        self.oracle = oracle_agent
        resolved_chronicle = chronicle_client if chronicle_client is not None else getattr(oracle_agent, "chronicle", None)
        self.learning_store = OracleV2LearningStore(
            state_path=state_path,
            storage_dir=storage_dir,
            chronicle_client=resolved_chronicle,
        )
        self.decision_ledger = decision_ledger or OracleDecisionLedger(
            ledger_path=ledger_path,
            storage_dir=storage_dir,
            chronicle_client=resolved_chronicle,
        )
        self.scout = OracleScoutSubAgent(
            learning_store=self.learning_store,
            decision_ledger=self.decision_ledger,
            sentinel_client=getattr(oracle_agent, "sentinel", None),
            pulse_client=getattr(oracle_agent, "pulse", None),
            chronicle_client=resolved_chronicle,
        )
        self.portfolio = OraclePortfolioSubAgent(
            learning_store=self.learning_store,
            decision_ledger=self.decision_ledger,
        )
        self.risk_subagent = OracleRiskSubAgent(
            learning_store=self.learning_store,
            decision_ledger=self.decision_ledger,
        )
        self.risk = self.risk_subagent

    def sync_learned_limits_to_risk_manager(self, universe_size: int = 8) -> Dict[str, Any]:
        """
        Push the empirically learned parameters from Scout, Portfolio, and Risk
        sub-agents into `oracle.risk` (`RiskManager`) so even legacy single-symbol
        call sites operate on learned parameters rather than static defaults.
        """
        rm = getattr(self.oracle, "risk", None)
        if rm is None:
            return {}
        dd = rm.portfolio.drawdown
        slot_budget = self.portfolio.learned_session_slot_budget(
            candidate_universe_size=universe_size,
            current_drawdown_fraction=dd,
        )
        daily_budget = self.portfolio.learned_daily_trade_budget(candidate_universe_size=universe_size)
        risk_frac = self.portfolio.learned_risk_fraction(current_drawdown_fraction=dd)
        max_dd = self.portfolio.learned_max_drawdown_fraction()
        daily_loss = self.portfolio.learned_daily_loss_limit_fraction()
        conv_floor = self.scout.learned_conviction_floor()
        max_curr = self.risk_subagent.learned_max_currency_exposure_fraction()
        loss_cd = self.risk_subagent.learned_cooldown_seconds(prior_won=False)
        win_cd = self.risk_subagent.learned_cooldown_seconds(prior_won=True)
        stop_m, targ_m = self.risk_subagent.learned_stop_target_multipliers()

        rm.risk_per_trade = risk_frac
        rm.max_positions = max(rm.max_positions, slot_budget)
        rm.max_drawdown = max_dd
        rm.daily_loss_limit = daily_loss
        rm.confidence_floor = conv_floor
        rm.max_currency_pct = max_curr
        rm.loss_cooldown_sec = loss_cd
        rm.win_cooldown_sec = win_cd
        rm.max_trades_per_symbol_per_day = max(1, daily_budget // 2)
        rm.stop_mult = stop_m
        rm.target_mult = targ_m
        rm.reward_risk = round(targ_m / max(0.1, stop_m), 3)

        return {
            "learned_session_slot_budget": slot_budget,
            "learned_daily_trade_budget": daily_budget,
            "learned_risk_per_trade": risk_frac,
            "learned_max_drawdown": max_dd,
            "learned_daily_loss_limit": daily_loss,
            "learned_conviction_floor": conv_floor,
            "learned_max_currency_pct": max_curr,
            "learned_loss_cooldown_sec": loss_cd,
            "learned_win_cooldown_sec": win_cd,
            "learned_stop_mult": stop_m,
            "learned_target_mult": targ_m,
        }

    def run_cio_tournament_cycle(
        self,
        symbols: List[str],
        *,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        max_session_slots_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Execute a full Wall Street CIO cycle across `symbols`:
          1. Scout evaluates all candidate symbols (fusing Forge blueprint +
             Sentinel news + Pulse social + Chronicle memory + HTF structure).
          2. Scout ranks all qualifying candidates in a Cross-Sectional Tournament.
          3. Portfolio determines how many session slots and daily trade slots are
             available based on learned concurrency & sequence distributions.
          4. Risk evaluates top-ranked candidates sequentially against open
             positions AND already-selected batch candidates for empirical return
             correlation, currency-leg concentration, cooldowns, and drawdown gates.
          5. Logs every selected trade, every unentered trade (including setups #4..#N
             that passed the conviction floor but lost the slot tournament!), and
             every exposed failure to `OracleDecisionLedger`.
        """
        clean_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        self.sync_learned_limits_to_risk_manager(universe_size=len(clean_symbols))

        rm = getattr(self.oracle, "risk", None)
        current_open: List[Dict[str, Any]] = []
        if open_positions is not None:
            current_open = list(open_positions)
        elif rm is not None:
            current_open = [p.__dict__ for p in rm.portfolio.positions]

        open_symbols = {str(p.get("symbol") or "").upper() for p in current_open}
        evaluations: List[ScoutCandidateEvaluation] = []
        price_histories: Dict[str, List[float]] = {}
        errors: List[Dict[str, Any]] = []

        # Collect price series for open positions so correlation checks have their returns
        for op in open_symbols:
            try:
                md = self.oracle.data.get(op)
                if md.get("status") == "complete" and md.get("series"):
                    price_histories[op] = list(md["series"].closes)
            except Exception as exc:
                self.decision_ledger.expose_failure(
                    component="MarketData",
                    operation="fetch_open_position_history",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=op,
                )

        # Phase 1: Scout Evaluation across all candidate symbols
        for sym in clean_symbols:
            if sym in open_symbols:
                continue
            try:
                sig = self.oracle.signal(sym)
                if sig.get("status") != "complete":
                    err_msg = str(sig.get("message") or "signal incomplete")
                    errors.append({"symbol": sym, "message": err_msg})
                    self.decision_ledger.expose_failure(
                        component="OracleSignal",
                        operation="signal",
                        error_type="SignalError",
                        error_message=err_msg,
                        symbol=sym,
                    )
                    continue

                md = self.oracle.data.get(sym)
                if md.get("status") == "complete" and md.get("series"):
                    price_histories[sym] = list(md["series"].closes)

                fused = sig["signal"]
                streams = fused.get("streams") or {}
                last_px = float(sig["last"])
                atr_val = float(sig.get("atr") or (last_px * 0.01))
                call = str(fused.get("call") or "hold").lower()
                dir_str = "long" if call == "buy" else "short"
                st_levels = rm.stop_target(last_px, dir_str, atr_val) if rm else {
                    "stop": last_px - 2.0 * atr_val,
                    "target": last_px + 3.0 * atr_val,
                }

                cand = self.scout.evaluate_candidate(
                    symbol=sym,
                    last_price=last_px,
                    atr=atr_val,
                    technicals=sig.get("_technicals") or {},
                    fused_signal=fused,
                    streams=streams,
                    stop_price=st_levels["stop"],
                    target_price=st_levels["target"],
                    using_evolved_champion=bool(sig.get("using_evolved_champion")),
                    strategy_id=(streams.get("technical") or {}).get("genome"),
                )
                evaluations.append(cand)
            except Exception as exc:
                errors.append({"symbol": sym, "message": str(exc)})
                self.decision_ledger.expose_failure(
                    component="OracleScoutSubAgent",
                    operation="evaluate_candidate",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=sym,
                )

        # Phase 2: Cross-Sectional Tournament Ranking
        qualifying, non_qualifying = self.scout.rank_tournament(evaluations)

        # Log all non-qualifying setups to the Decision Ledger (with counterfactual levels)
        for nq in non_qualifying:
            reason_code = (
                "no_directional_setup"
                if nq.direction == "hold"
                else "news_shock_pause"
                if nq.news_assessment.get("pause_new_entries")
                else "social_manipulation_pause"
                if nq.social_assessment.get("pause_new_entries")
                else "below_learned_conviction_floor"
            )
            self.decision_ledger.record_trade_not_entered(
                symbol=nq.symbol,
                direction=nq.direction if nq.direction in ("long", "short") else "hold",
                hypothetical_entry=nq.last_price,
                hypothetical_stop=nq.hypothetical_stop,
                hypothetical_target=nq.hypothetical_target,
                scout_score=nq.scout_score,
                effective_confidence=nq.effective_confidence,
                tournament_rank=None,
                candidates_evaluated=len(evaluations),
                regime=nq.regime,
                session=nq.session,
                strategy_id=nq.strategy_id,
                stage_rejected="scout",
                reason_code=reason_code,
                reasons=nq.why_not_reasons or [reason_code],
                streams=nq.streams,
                news_assessment=nq.news_assessment,
                social_assessment=nq.social_assessment,
            )

        # Phase 3: Portfolio Slot Budget & Risk Correlation Gate
        dd_now = rm.portfolio.drawdown if rm else 0.0
        learned_slots = self.portfolio.learned_session_slot_budget(
            candidate_universe_size=len(clean_symbols),
            current_drawdown_fraction=dd_now,
        )
        session_slot_cap = (
            min(learned_slots, max_session_slots_override)
            if max_session_slots_override is not None
            else learned_slots
        )
        available_slots = max(0, session_slot_cap - len(current_open))
        daily_budget = self.portfolio.learned_daily_trade_budget(candidate_universe_size=len(clean_symbols))
        opened_today_total = sum((rm.portfolio.trades_today or {}).values()) if rm else 0
        remaining_daily_budget = max(0, daily_budget - opened_today_total)

        selected_candidates: List[ScoutCandidateEvaluation] = []
        approved_plans: List[Dict[str, Any]] = []
        skipped_qualifying: List[Dict[str, Any]] = []

        for cand in qualifying:
            # Check Portfolio daily trade budget
            if len(selected_candidates) >= remaining_daily_budget:
                reason = (
                    f"Outranked for daily trade budget: ranked #{cand.tournament_rank}/{len(qualifying)} "
                    f"(learned daily budget={daily_budget}, opened_today={opened_today_total})"
                )
                self.decision_ledger.record_trade_not_entered(
                    symbol=cand.symbol,
                    direction=cand.direction,
                    hypothetical_entry=cand.last_price,
                    hypothetical_stop=cand.hypothetical_stop,
                    hypothetical_target=cand.hypothetical_target,
                    scout_score=cand.scout_score,
                    effective_confidence=cand.effective_confidence,
                    tournament_rank=cand.tournament_rank,
                    candidates_evaluated=len(evaluations),
                    regime=cand.regime,
                    session=cand.session,
                    strategy_id=cand.strategy_id,
                    stage_rejected="portfolio",
                    reason_code="daily_trade_budget_exhausted",
                    reasons=[reason],
                    streams=cand.streams,
                    news_assessment=cand.news_assessment,
                    social_assessment=cand.social_assessment,
                )
                skipped_qualifying.append({"symbol": cand.symbol, "rank": cand.tournament_rank, "reason": reason})
                continue

            # Check Portfolio concurrent session slots
            if len(selected_candidates) >= available_slots:
                winners_str = ", ".join(f"{s.symbol}(#{s.tournament_rank}, score={s.scout_score:.3f})" for s in selected_candidates)
                reason = (
                    f"Outranked in Scout Tournament: ranked #{cand.tournament_rank}/{len(qualifying)} "
                    f"(score={cand.scout_score:.3f}); session slot budget ({session_slot_cap}) filled by [{winners_str}]"
                )
                self.decision_ledger.record_trade_not_entered(
                    symbol=cand.symbol,
                    direction=cand.direction,
                    hypothetical_entry=cand.last_price,
                    hypothetical_stop=cand.hypothetical_stop,
                    hypothetical_target=cand.hypothetical_target,
                    scout_score=cand.scout_score,
                    effective_confidence=cand.effective_confidence,
                    tournament_rank=cand.tournament_rank,
                    candidates_evaluated=len(evaluations),
                    regime=cand.regime,
                    session=cand.session,
                    strategy_id=cand.strategy_id,
                    stage_rejected="portfolio",
                    reason_code="outranked_for_session_slots",
                    reasons=[reason],
                    streams=cand.streams,
                    news_assessment=cand.news_assessment,
                    social_assessment=cand.social_assessment,
                )
                skipped_qualifying.append({"symbol": cand.symbol, "rank": cand.tournament_rank, "reason": reason})
                continue

            # Phase 4: Risk & Cross-Symbol Correlation Evaluation
            since_loss = rm.portfolio.seconds_since_last_loss(cand.symbol) if rm else None
            since_win = rm.portfolio.seconds_since_last_win(cand.symbol) if rm else None
            risk_check = self.risk_subagent.evaluate_candidate_risk(
                candidate=cand,
                open_positions=current_open,
                selected_in_batch=selected_candidates,
                price_histories=price_histories,
                seconds_since_last_loss=since_loss,
                seconds_since_last_win=since_win,
            )
            if not risk_check["approved"]:
                self.decision_ledger.record_trade_not_entered(
                    symbol=cand.symbol,
                    direction=cand.direction,
                    hypothetical_entry=cand.last_price,
                    hypothetical_stop=cand.hypothetical_stop,
                    hypothetical_target=cand.hypothetical_target,
                    scout_score=cand.scout_score,
                    effective_confidence=cand.effective_confidence,
                    tournament_rank=cand.tournament_rank,
                    candidates_evaluated=len(evaluations),
                    regime=cand.regime,
                    session=cand.session,
                    strategy_id=cand.strategy_id,
                    stage_rejected="risk",
                    reason_code="correlation_or_cooldown_rejected",
                    reasons=risk_check["reasons"],
                    streams=cand.streams,
                    news_assessment=cand.news_assessment,
                    social_assessment=cand.social_assessment,
                )
                skipped_qualifying.append({
                    "symbol": cand.symbol,
                    "rank": cand.tournament_rank,
                    "reason": "; ".join(risk_check["reasons"]),
                })
                continue

            # Also run RiskManager's account-level drawdown, daily loss, and currency concentration gate
            if rm is not None:
                rm.risk_per_trade = self.portfolio.learned_risk_fraction(
                    effective_confidence=cand.effective_confidence,
                    current_drawdown_fraction=dd_now,
                    asset_class=cand.asset_class,
                )
                plan = rm.evaluate(
                    cand.symbol,
                    cand.direction,
                    cand.last_price,
                    cand.atr,
                    cand.effective_confidence,
                )
                if not plan.get("approved"):
                    self.decision_ledger.record_trade_not_entered(
                        symbol=cand.symbol,
                        direction=cand.direction,
                        hypothetical_entry=cand.last_price,
                        hypothetical_stop=cand.hypothetical_stop,
                        hypothetical_target=cand.hypothetical_target,
                        scout_score=cand.scout_score,
                        effective_confidence=cand.effective_confidence,
                        tournament_rank=cand.tournament_rank,
                        candidates_evaluated=len(evaluations),
                        regime=cand.regime,
                        session=cand.session,
                        strategy_id=cand.strategy_id,
                        stage_rejected="risk",
                        reason_code="account_risk_gate_rejected",
                        reasons=plan.get("reasons") or ["risk gate rejected"],
                        streams=cand.streams,
                        news_assessment=cand.news_assessment,
                        social_assessment=cand.social_assessment,
                    )
                    skipped_qualifying.append({
                        "symbol": cand.symbol,
                        "rank": cand.tournament_rank,
                        "reason": "; ".join(plan.get("reasons") or ["risk gate rejected"]),
                    })
                    continue
            else:
                plan = {
                    "approved": True,
                    "symbol": cand.symbol,
                    "direction": cand.direction,
                    "entry": cand.last_price,
                    "stop": cand.hypothetical_stop,
                    "target": cand.hypothetical_target,
                    "size": 1.0,
                }

            selected_candidates.append(cand)
            plan["scout_score"] = cand.scout_score
            plan["expected_net_r"] = cand.expected_net_r
            plan["tournament_rank"] = cand.tournament_rank
            plan["candidates_evaluated"] = len(evaluations)
            plan["qualifying_setups_count"] = len(qualifying)
            plan["why_trade_reasons"] = cand.why_trade_reasons
            plan["dimension_scores"] = cand.dimension_scores
            plan["highest_directional_correlation"] = risk_check["highest_directional_correlation"]

            approved_plans.append({
                "symbol": cand.symbol,
                "plan": plan,
                "signal": {
                    "symbol": cand.symbol,
                    "call": cand.call,
                    "direction": 1.0 if cand.direction == "long" else -1.0,
                    "confidence": cand.raw_confidence,
                    "streams": cand.streams,
                },
                "effective_confidence": cand.effective_confidence,
                "scout_score": cand.scout_score,
                "expected_net_r": cand.expected_net_r,
                "tournament_rank": cand.tournament_rank,
                "using_evolved_champion": cand.using_evolved_champion,
                "news_impact": cand.news_assessment["level"],
                "news_reason": cand.news_assessment["reason"],
                "social_risk": cand.social_assessment["level"],
                "social_reason": cand.social_assessment["reason"],
                "atr": cand.atr,
                "_streams": cand.streams,
                "_dimension_scores": cand.dimension_scores,
                "_risk_check": risk_check,
            })

        return {
            "status": "complete",
            "schema": "oracle.v2.cio_tournament.v1",
            "candidates_scanned": len(clean_symbols),
            "candidates_evaluated": len(evaluations),
            "qualifying_setups_count": len(qualifying),
            "learned_session_slot_budget": session_slot_cap,
            "available_session_slots": available_slots,
            "learned_daily_trade_budget": daily_budget,
            "remaining_daily_budget": remaining_daily_budget,
            "selected_count": len(approved_plans),
            "selected_trades": approved_plans,
            "skipped_qualifying_setups": skipped_qualifying,
            "leaderboard": [
                {
                    "rank": c.tournament_rank,
                    "symbol": c.symbol,
                    "direction": c.direction,
                    "scout_score": c.scout_score,
                    "expected_net_r": c.expected_net_r,
                    "effective_confidence": c.effective_confidence,
                    "learned_conviction_floor": c.learned_conviction_floor,
                    "regime": c.regime,
                    "selected": any(p["symbol"] == c.symbol for p in approved_plans),
                }
                for c in qualifying
            ],
            "errors": errors,
        }

    def record_realized_outcome(
        self,
        *,
        symbol: str,
        direction: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        initial_stop: float = 0.0,
        initial_target: float = 0.0,
        entry_confidence: float = 0.6,
        entry_regime: str = "ranging",
        exit_regime: str = "ranging",
        pnl_r: float,
        won: bool,
        exit_reason: str = "unknown",
        duration_sec: float = 0.0,
        dimension_scores_at_entry: Optional[Dict[str, float]] = None,
        concurrency_at_entry: int = 1,
        daily_trade_seq: int = 1,
        max_corr_at_entry: float = 0.0,
        currency_exposure_frac: float = 0.0,
        elapsed_since_prior_close_sec: Optional[float] = None,
        prior_trade_won: Optional[bool] = None,
        entry_atr: Optional[float] = None,
        strategy_id: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """
        Update all 3 sub-agents (Scout, Portfolio, Risk) and record the closed
        trade in `OracleDecisionLedger`.
        """
        sym = symbol.upper()
        session = current_trading_session()
        asset_class = classify_asset_class(sym)

        atr_val = float(entry_atr or (abs(entry_price - initial_stop) / 2.0) or 1e-6)
        stop_atr_mult = abs(entry_price - initial_stop) / atr_val if (atr_val > 0 and initial_stop > 0) else 2.0
        target_atr_mult = abs(initial_target - entry_price) / atr_val if (atr_val > 0 and initial_target > 0) else 3.0

        scout_update = self.scout.learn_from_outcome(
            symbol=sym,
            regime=entry_regime,
            session=session,
            entry_conviction=entry_confidence,
            dimension_scores_at_entry=dimension_scores_at_entry or {},
            won=won,
            pnl_r=pnl_r,
        )
        portfolio_update = self.portfolio.learn_from_outcome(
            asset_class=asset_class,
            concurrency_at_entry=concurrency_at_entry,
            daily_trade_seq=daily_trade_seq,
            won=won,
            pnl_r=pnl_r,
        )
        risk_update = self.risk_subagent.learn_from_outcome(
            max_corr_at_entry=max_corr_at_entry,
            currency_exposure_frac=currency_exposure_frac,
            elapsed_since_prior_close_sec=elapsed_since_prior_close_sec,
            prior_trade_won=prior_trade_won,
            stop_atr_mult=stop_atr_mult,
            target_atr_mult=target_atr_mult,
            won=won,
            pnl_r=pnl_r,
            concurrent_positions_count=concurrency_at_entry,
            symbol=sym,
        )

        synced = self.sync_learned_limits_to_risk_manager()
        learning_updates = {
            "scout": scout_update,
            "portfolio": portfolio_update,
            "risk": risk_update,
            "synced_risk_manager": synced,
        }
        ledger_rec = self.decision_ledger.record_trade_closed(
            symbol=sym,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_r=pnl_r,
            won=won,
            exit_reason=exit_reason,
            duration_sec=duration_sec,
            entry_regime=entry_regime,
            exit_regime=exit_regime,
            strategy_id=strategy_id,
            learning_updates=learning_updates,
        )
        return {
            "status": "complete",
            "ledger_event_id": ledger_rec["event_id"],
            "learning_updates": learning_updates,
        }

    def replay_pending_counterfactuals(
        self,
        *,
        max_items: int = 25,
        max_records: Optional[int] = None,
        invoke_shadow_runner_on_demand: bool = False,
    ) -> Dict[str, Any]:
        """
        Replay unentered trades from `OracleDecisionLedger` against subsequent
        market price bars (or on-demand `ForgeShadowRunner`) and feed the
        counterfactual outcomes (`MISSED_WINNER` vs `AVOIDED_LOSS`) into Scout,
        Portfolio, and Risk sub-agents.
        """
        limit = max_records if max_records is not None else max_items
        pending = self.decision_ledger.unentered_trades_pending_replay(max_items=limit)
        replayed: List[Dict[str, Any]] = []
        missed_winners = 0
        avoided_losses = 0

        for item in pending:
            sym = str(item.get("symbol") or "").upper()
            direction = str(item.get("direction") or "hold").lower()
            if direction not in ("long", "short", "buy", "sell"):
                continue
            entry = float(item.get("hypothetical_entry") or 0.0)
            stop = float(item.get("hypothetical_stop") or 0.0)
            target = float(item.get("hypothetical_target") or 0.0)
            risk_unit = abs(entry - stop)
            if entry <= 0 or risk_unit <= 1e-12:
                continue

            # Evaluate against current/subsequent market bars
            would_win = False
            cf_r = 0.0
            try:
                md = self.oracle.data.get(sym)
                if md.get("status") == "complete" and md.get("series"):
                    bars = md["series"].bars
                    recent_bars = bars[-15:] if len(bars) >= 15 else bars
                    sign = 1.0 if direction in ("long", "buy") else -1.0
                    hit_tp = False
                    hit_sl = False
                    for bar in recent_bars[-5:]:
                        if sign > 0:
                            if target > entry and bar.high >= target:
                                hit_tp = True
                                break
                            if bar.low <= stop:
                                hit_sl = True
                                break
                        else:
                            if 0 < target < entry and bar.low <= target:
                                hit_tp = True
                                break
                            if bar.high >= stop:
                                hit_sl = True
                                break
                    if hit_tp:
                        would_win = True
                        cf_r = abs(target - entry) / risk_unit
                    elif hit_sl:
                        would_win = False
                        cf_r = -1.0
                    else:
                        last_close = float(md["series"].last)
                        cf_r = sign * (last_close - entry) / risk_unit
                        would_win = cf_r > 0.25
            except Exception as exc:
                self.decision_ledger.expose_failure(
                    component="CounterfactualReplay",
                    operation="replay_bars",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=sym,
                )
                continue

            shadow_summary = None
            if invoke_shadow_runner_on_demand:
                try:
                    from Forge.core.shadow_runner import ForgeShadowRunner
                    eco_root = _ORACLE_ROOT.parent
                    runner = ForgeShadowRunner(
                        eco_root,
                        paper_only=True,
                        output_namespace="counterfactual_on_demand",
                    )
                    sr_out = runner.run_shadow_session(sym, "ALL", 160, False)
                    shadow_summary = {
                        "shadow_trades": sr_out.get("shadow_trades_evaluated", 0),
                        "win_rate": sr_out.get("win_rate", 0.0),
                    }
                    del runner
                    gc.collect()
                except Exception as exc:
                    self.decision_ledger.expose_failure(
                        component="ForgeShadowRunner",
                        operation="on_demand_counterfactual",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        symbol=sym,
                    )

            classification = (
                "MISSED_WINNER"
                if would_win and cf_r >= 0.5
                else "AVOIDED_LOSS"
                if not would_win and cf_r <= -0.25
                else "NEUTRAL"
            )
            if classification == "MISSED_WINNER":
                missed_winners += 1
            elif classification == "AVOIDED_LOSS":
                avoided_losses += 1

            adj = self.scout.learn_from_counterfactual(
                symbol=sym,
                regime=str(item.get("regime") or "ranging"),
                conviction=float(item.get("effective_confidence") or 0.5),
                stage_rejected=str(item.get("stage_rejected") or "scout"),
                counterfactual_r=cf_r,
                would_have_won=would_win,
            )
            if shadow_summary:
                adj["on_demand_shadow_runner"] = shadow_summary

            rec = self.decision_ledger.record_counterfactual_replay(
                original_event_id=str(item.get("event_id")),
                symbol=sym,
                direction=direction,
                stage_rejected=str(item.get("stage_rejected") or "scout"),
                reason_code=str(item.get("reason_code") or "unknown"),
                would_have_won=would_win,
                counterfactual_r=cf_r,
                classification=classification,
                learning_adjustments=adj,
            )
            replayed.append(rec)

        updated_floor = self.scout.learned_conviction_floor()
        return {
            "status": "complete",
            "replayed_count": len(replayed),
            "missed_winners": missed_winners,
            "avoided_losses": avoided_losses,
            "replays": replayed,
            "learned_conviction_floor": updated_floor,
            "updated_conviction_floor": updated_floor,
        }

    def conduct_atlas_forge_research_review(
        self,
        *,
        focus_symbol: Optional[str] = None,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Oracle <-> Atlas <-> Forge Research & Accountability Loop:
          1. Oracle inspects its Decision Ledger for losing (symbol, regime)
             clusters, missed counterfactual winners, and exposed failures.
          2. Oracle queries Atlas ("What's new in quantitative trading for this
             regime/asset class, and what structural edges are our current
             strategies missing?").
          3. Oracle dispatches concrete deficiency & retraining mandates back to
             Forge so Forge continuously improves its training methods and
             challenges weak incumbents.
        """
        diag = self.decision_ledger.summarize_diagnostics()
        losing_clusters = diag.get("losing_symbol_regimes") or []
        if symbols:
            wanted = {str(s).strip().upper() for s in symbols if str(s).strip()}
            matching = [c for c in losing_clusters if str(c.get("symbol") or "").upper() in wanted]
            if not matching:
                matching = [
                    {
                        "symbol": sym_up,
                        "regime": "ranging",
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "total_r": 0.0,
                        "expectancy_r": 0.0,
                        "exit_reasons": {},
                    }
                    for sym_up in sorted(wanted)
                ]
            targets = matching[:3]
        elif focus_symbol:
            sym_up = focus_symbol.upper()
            matching = [c for c in losing_clusters if c.get("symbol") == sym_up]
            if not matching:
                matching = [{
                    "symbol": sym_up,
                    "regime": "ranging",
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_r": 0.0,
                    "expectancy_r": 0.0,
                    "exit_reasons": {},
                }]
            targets = matching[:3]
        else:
            targets = losing_clusters[:3] if losing_clusters else [{
                "symbol": "EURUSD",
                "regime": "ranging",
                "trades": diag.get("trades_closed", 0),
                "wins": 0,
                "losses": 0,
                "total_r": 0.0,
                "expectancy_r": 0.0,
                "exit_reasons": {},
            }]

        atlas_client = getattr(self.oracle, "atlas", None)
        forge_client = getattr(self.oracle, "forge", None)

        atlas_findings: List[Dict[str, Any]] = []
        forge_mandates: List[Dict[str, Any]] = []

        for cluster in targets:
            sym = str(cluster.get("symbol") or "EURUSD").upper()
            regime = str(cluster.get("regime") or "ranging")
            exp_r = float(cluster.get("expectancy_r") or 0.0)
            query = (
                f"Institutional quantitative trading advancements and regime adaptation for {sym} "
                f"in {regime} market conditions (realized expectancy {exp_r:+.2f}R, "
                f"missed winners={diag.get('missed_winners_count', 0)}). "
                f"Identify concrete feature, multi-timeframe structure, and execution-filter improvements."
            )
            atlas_summary = "Atlas unavailable; mandate generated from Oracle empirical ledger."
            atlas_sources: List[Any] = []
            if atlas_client is not None:
                try:
                    if hasattr(atlas_client, "act"):
                        res = atlas_client.act(
                            "research.investigate",
                            {
                                "query": query,
                                "domain": "trading",
                                "depth": "standard",
                                "_sender": "oracle_v2_cio",
                            },
                        )
                    elif hasattr(atlas_client, "handle"):
                        res = atlas_client.handle({"context": {"query": query, "domain": "trading"}})
                    else:
                        res = {}
                    rep = (res or {}).get("report") or (res or {})
                    if isinstance(rep, dict) and rep.get("summary"):
                        atlas_summary = str(rep["summary"])
                        atlas_sources = list(rep.get("sources") or [])
                except Exception as exc:
                    self.decision_ledger.expose_failure(
                        component="Atlas",
                        operation="research.investigate",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        symbol=sym,
                    )

            atlas_findings.append({
                "symbol": sym,
                "regime": regime,
                "query": query,
                "summary": atlas_summary,
                "sources": atlas_sources,
            })

            # Dispatch concrete retraining & hypothesis mandate to Forge
            mandate_statement = (
                f"Oracle V2 CIO Deficiency Mandate for {sym}::{regime} "
                f"(realized E[R]={exp_r:+.2f}R): integrate Atlas research findings "
                f"({atlas_summary[:140]}) and retrain multi-timeframe entry/exit blueprint."
            )
            forge_status = "forge_not_connected"
            forge_response: Dict[str, Any] = {}
            if forge_client is not None:
                try:
                    if hasattr(forge_client, "act"):
                        forge_response = forge_client.act(
                            "oracle.improvement.request",
                            {
                                "request": {
                                    "symbol": sym,
                                    "regime": regime,
                                    "reason": mandate_statement,
                                    "performance_metrics": cluster,
                                    "atlas_research_summary": atlas_summary,
                                },
                                "statement": mandate_statement,
                                "proposed_by": "oracle_v2_cio",
                            },
                        ) or {}
                        forge_status = str(forge_response.get("status") or "submitted")
                except Exception as exc:
                    self.decision_ledger.expose_failure(
                        component="Forge",
                        operation="oracle.improvement.request",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        symbol=sym,
                    )
                    forge_status = f"error:{type(exc).__name__}"

            forge_mandates.append({
                "symbol": sym,
                "regime": regime,
                "deficiency_metrics": cluster,
                "atlas_research_summary": atlas_summary,
                "mandate_statement": mandate_statement,
                "forge_status": forge_status,
                "forge_response": forge_response,
            })

        return {
            "status": "complete",
            "schema": "oracle.v2.atlas_forge_review.v1",
            "diagnostics_summary": diag,
            "deficiencies_detected": targets,
            "atlas_findings": atlas_findings,
            "atlas_insights": atlas_findings,
            "forge_mandates": forge_mandates,
        }

    def run_atlas_forge_research_review(
        self,
        symbols: Optional[List[str]] = None,
        *,
        focus_symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Alias for conduct_atlas_forge_research_review."""
        return self.conduct_atlas_forge_research_review(
            focus_symbol=focus_symbol,
            symbols=symbols,
        )

    def status_snapshot(self) -> Dict[str, Any]:
        """Full observability snapshot of Oracle V2 CIO and its 3 sub-agents."""
        return {
            "architecture": "oracle_v2_wall_street_cio",
            "role": "Why-To-Trade Scouting, Portfolio Allocation & Risk Correlation CIO",
            "zero_hardcoded_variables": True,
            "sub_agents": {
                "scout": {
                    "learned_dimension_weights": self.scout.learned_dimension_weights(),
                    "learned_conviction_floor": self.scout.learned_conviction_floor(),
                    "tracked_symbol_regimes": len(self.learning_store.data["scout"].get("symbol_regime_stats", {})),
                },
                "portfolio": {
                    "learned_session_slot_budget": self.portfolio.learned_session_slot_budget(),
                    "learned_daily_trade_budget": self.portfolio.learned_daily_trade_budget(),
                    "learned_risk_fraction": self.portfolio.learned_risk_fraction(),
                    "learned_max_drawdown_fraction": self.portfolio.learned_max_drawdown_fraction(),
                    "learned_daily_loss_limit_fraction": self.portfolio.learned_daily_loss_limit_fraction(),
                    "realized_trades": self.learning_store.data["portfolio"].get("realized_trades", 0),
                },
                "risk": {
                    "learned_max_correlation": self.risk_subagent.learned_max_correlation(),
                    "learned_max_currency_exposure_fraction": self.risk_subagent.learned_max_currency_exposure_fraction(),
                    "learned_loss_cooldown_sec": self.risk_subagent.learned_cooldown_seconds(prior_won=False),
                    "learned_win_cooldown_sec": self.risk_subagent.learned_cooldown_seconds(prior_won=True),
                    "learned_stop_target_multipliers": self.risk_subagent.learned_stop_target_multipliers(),
                },
            },
            "decision_ledger": self.decision_ledger.summarize_diagnostics(),
        }

    def status(self) -> Dict[str, Any]:
        """Alias for status_snapshot()."""
        return self.status_snapshot()

