"""
Oracle.scout_agent.agent
========================
Sub-Agent 1: Opportunity Scouting & Cross-Sectional Tournament Ranking.
Determines WHY we should trade a symbol and ranks all candidate setups across
the watchlist so Oracle only deploys capital into the highest expectancy
opportunities.
"""
from __future__ import annotations

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

try:
    from scout_agent.models import (  # type: ignore
        SCOUT_DIMENSIONS,
        ScoutCandidateEvaluation,
        classify_asset_class,
        current_trading_session,
    )
    from scout_agent.structure_analyzer import (  # type: ignore
        compute_htf_structure_score,
        compute_spread_efficiency_score,
        compute_stream_alignment,
    )
    from scout_agent.tournament import rank_cross_sectional_tournament  # type: ignore
    from scout_agent.learner import (  # type: ignore
        compute_learned_conviction_floor,
        compute_learned_dimension_weights,
        update_scout_from_counterfactual,
        update_scout_from_outcome,
    )
except ImportError:
    from Oracle.scout_agent.models import (  # type: ignore
        SCOUT_DIMENSIONS,
        ScoutCandidateEvaluation,
        classify_asset_class,
        current_trading_session,
    )
    from Oracle.scout_agent.structure_analyzer import (  # type: ignore
        compute_htf_structure_score,
        compute_spread_efficiency_score,
        compute_stream_alignment,
    )
    from Oracle.scout_agent.tournament import rank_cross_sectional_tournament  # type: ignore
    from Oracle.scout_agent.learner import (  # type: ignore
        compute_learned_conviction_floor,
        compute_learned_dimension_weights,
        update_scout_from_counterfactual,
        update_scout_from_outcome,
    )


class OracleScoutSubAgent:
    """
    Sub-Agent 1: Opportunity Scouting & Cross-Sectional Tournament Ranking.
    Fuses Forge blueprints + Sentinel news + Pulse social + Chronicle memory +
    HTF structure + spread efficiency, learns dimension weights and conviction
    floors empirically, and ranks all candidates in a Cross-Sectional Tournament.
    """

    def __init__(
        self,
        learning_store: Any,
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
        return compute_learned_dimension_weights(self._scout_state)

    def learned_conviction_floor(
        self,
        symbol: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> float:
        return compute_learned_conviction_floor(self._scout_state, symbol=symbol, regime=regime)

    def _compute_htf_structure_score(
        self,
        direction: str,
        last_price: float,
        atr: float,
        technicals: Dict[str, Any],
        stop_price: float,
        target_price: float,
    ) -> Tuple[float, float, List[str]]:
        return compute_htf_structure_score(
            direction=direction,
            last_price=last_price,
            atr=atr,
            technicals=technicals,
            stop_price=stop_price,
            target_price=target_price,
        )

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
        Evaluate a single symbol's setup across all 6 intelligence dimensions.
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

        tech_score = compute_stream_alignment(tech_stream, sign)
        news_score = compute_stream_alignment(news_stream, sign) * news_mult
        social_score = compute_stream_alignment(social_stream, sign) * social_mult
        memory_score = compute_stream_alignment(mem_stream, sign)

        htf_score, rr_ratio, htf_reasons = compute_htf_structure_score(
            direction, last_price, atr, technicals, stop_price, target_price
        )
        spread_score, spread_to_risk = compute_spread_efficiency_score(
            last_price, stop_price, spread_price
        )

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
        blended_conf = (0.5 * raw_conf + 0.5 * composite_conviction) * news_mult * social_mult
        effective_confidence = round(max(0.0, min(0.99, blended_conf)), 4)

        floor = self.learned_conviction_floor(sym, regime)
        passes_floor = (
            direction in ("long", "short")
            and not news_obj.pause_new_entries
            and not social_obj.pause_new_entries
            and round(effective_confidence, 4) >= round(floor, 4)
        )

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
            spread_to_risk_ratio=spread_to_risk,
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
        return rank_cross_sectional_tournament(evaluations)

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
        res = update_scout_from_outcome(
            self._scout_state,
            symbol=symbol,
            regime=regime,
            session=session,
            entry_conviction=entry_conviction,
            dimension_scores_at_entry=dimension_scores_at_entry,
            won=won,
            pnl_r=pnl_r,
        )
        self.store.save()
        return res

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
        res = update_scout_from_counterfactual(
            self._scout_state,
            symbol=symbol,
            regime=regime,
            conviction=conviction,
            stage_rejected=stage_rejected,
            counterfactual_r=counterfactual_r,
            would_have_won=would_have_won,
        )
        self.store.save()
        return res

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
        return self.learn_from_outcome(
            symbol=symbol,
            regime=regime,
            session=session or current_trading_session(),
            entry_conviction=entry_conviction,
            dimension_scores_at_entry=dimension_scores or dimension_scores_at_entry or {},
            won=won,
            pnl_r=pnl_r,
        )
