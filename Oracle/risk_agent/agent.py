"""
Oracle.risk_agent.agent
=======================
Sub-Agent 3: Empirical Cross-Symbol Correlation, Currency Concentration,
and Active Risk Defense Gate.
Answers "Is this trade worth the risk?" by computing live return correlations
and currency-leg overlap against open & batch-selected positions, using
thresholds learned from historical multi-position drawdown events.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from scout_agent.models import ScoutCandidateEvaluation  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import ScoutCandidateEvaluation  # type: ignore

try:
    from risk_agent.correlation_matrix import (  # type: ignore
        compute_currency_leg_similarity,
        compute_learned_max_correlation,
        compute_learned_max_currency_exposure_fraction,
        compute_return_correlation,
    )
    from risk_agent.cooldown_and_shocks import (  # type: ignore
        compute_learned_cooldown_seconds,
        compute_learned_news_multipliers,
        compute_learned_social_multipliers,
        compute_learned_stop_target_multipliers,
    )
    from risk_agent.learner import update_risk_from_outcome  # type: ignore
except ImportError:
    from Oracle.risk_agent.correlation_matrix import (  # type: ignore
        compute_currency_leg_similarity,
        compute_learned_max_correlation,
        compute_learned_max_currency_exposure_fraction,
        compute_return_correlation,
    )
    from Oracle.risk_agent.cooldown_and_shocks import (  # type: ignore
        compute_learned_cooldown_seconds,
        compute_learned_news_multipliers,
        compute_learned_social_multipliers,
        compute_learned_stop_target_multipliers,
    )
    from Oracle.risk_agent.learner import update_risk_from_outcome  # type: ignore


class OracleRiskSubAgent:
    """
    Sub-Agent 3: Empirical Cross-Symbol Correlation, Currency Concentration,
    and Active Risk Defense Gate.
    """

    def __init__(
        self,
        learning_store: Any,
        decision_ledger: Any = None,
    ) -> None:
        self.store = learning_store
        self.ledger = decision_ledger

    @property
    def _risk_state(self) -> Dict[str, Any]:
        return self.store.data["risk"]

    @property
    def _scout_state(self) -> Dict[str, Any]:
        return self.store.data["scout"]

    def learned_max_correlation(self) -> float:
        return compute_learned_max_correlation(self._risk_state)

    def learned_max_currency_exposure_fraction(self) -> float:
        return compute_learned_max_currency_exposure_fraction(self._risk_state)

    def learned_cooldown_seconds(
        self,
        prior_won: Any = False,
        kind: Optional[str] = None,
        bar_interval_sec: float = 900.0,
    ) -> float:
        return compute_learned_cooldown_seconds(
            self._risk_state,
            prior_won=prior_won,
            kind=kind,
            bar_interval_sec=bar_interval_sec,
        )

    def learned_news_multipliers(self) -> Dict[str, float]:
        return compute_learned_news_multipliers(self._scout_state)

    def learned_social_multipliers(self) -> Dict[str, float]:
        return compute_learned_social_multipliers(self._scout_state)

    def learned_stop_target_multipliers(self) -> Tuple[float, float]:
        return compute_learned_stop_target_multipliers(self._risk_state)

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
            since_loss = (
                portfolio_state.seconds_since_last_loss(sym)
                if hasattr(portfolio_state, "seconds_since_last_loss")
                else None
            )
            since_win = (
                portfolio_state.seconds_since_last_win(sym)
                if hasattr(portfolio_state, "seconds_since_last_win")
                else None
            )
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
        update_risk_from_outcome(
            self._risk_state,
            self._scout_state,
            max_corr_at_entry=max_corr_at_entry,
            currency_exposure_frac=currency_exposure_frac,
            elapsed_since_prior_close_sec=elapsed_since_prior_close_sec,
            prior_trade_won=prior_trade_won,
            stop_atr_mult=stop_atr_mult,
            target_atr_mult=target_atr_mult,
            won=won,
            pnl_r=pnl_r,
            concurrent_positions_count=concurrent_positions_count,
            symbol=symbol,
            news_impact=news_impact,
            social_risk=social_risk,
        )
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
