"""
Oracle.cio.engine
=================
The Boss: Oracle V2 Chief Investment Officer (CIO) Engine.
Coordinates the 3 specialized sub-agents (Scout, Portfolio, Risk) +
Sentinel + Pulse + Atlas + Chronicle + Forge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from intelligence.decision_ledger import OracleDecisionLedger  # type: ignore
except ImportError:
    from Oracle.intelligence.decision_ledger import OracleDecisionLedger  # type: ignore

try:
    from scout_agent import (  # type: ignore
        OracleScoutSubAgent,
        classify_asset_class,
        current_trading_session,
    )
    from portfolio_agent import OraclePortfolioSubAgent  # type: ignore
    from risk_agent import OracleRiskSubAgent  # type: ignore
    from cio.learning_store import OracleV2LearningStore  # type: ignore
    from cio.tournament_runner import execute_cio_tournament_cycle  # type: ignore
    from cio.counterfactual_replayer import replay_unentered_counterfactuals  # type: ignore
    from cio.atlas_forge_loop import conduct_atlas_forge_review  # type: ignore
except ImportError:
    from Oracle.scout_agent import (  # type: ignore
        OracleScoutSubAgent,
        classify_asset_class,
        current_trading_session,
    )
    from Oracle.portfolio_agent import OraclePortfolioSubAgent  # type: ignore
    from Oracle.risk_agent import OracleRiskSubAgent  # type: ignore
    from Oracle.cio.learning_store import OracleV2LearningStore  # type: ignore
    from Oracle.cio.tournament_runner import execute_cio_tournament_cycle  # type: ignore
    from Oracle.cio.counterfactual_replayer import replay_unentered_counterfactuals  # type: ignore
    from Oracle.cio.atlas_forge_loop import conduct_atlas_forge_review  # type: ignore


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
        resolved_chronicle = (
            chronicle_client if chronicle_client is not None else getattr(oracle_agent, "chronicle", None)
        )
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
        return execute_cio_tournament_cycle(
            self,
            symbols,
            open_positions=open_positions,
            max_session_slots_override=max_session_slots_override,
        )

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
        return replay_unentered_counterfactuals(
            oracle_agent=self.oracle,
            scout_agent=self.scout,
            decision_ledger=self.decision_ledger,
            max_items=max_items,
            max_records=max_records,
            invoke_shadow_runner_on_demand=invoke_shadow_runner_on_demand,
        )

    def conduct_atlas_forge_research_review(
        self,
        *,
        focus_symbol: Optional[str] = None,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return conduct_atlas_forge_review(
            oracle_agent=self.oracle,
            decision_ledger=self.decision_ledger,
            focus_symbol=focus_symbol,
            symbols=symbols,
        )

    def run_atlas_forge_research_review(
        self,
        symbols: Optional[List[str]] = None,
        *,
        focus_symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.conduct_atlas_forge_research_review(
            focus_symbol=focus_symbol,
            symbols=symbols,
        )

    def status_snapshot(self) -> Dict[str, Any]:
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
        return self.status_snapshot()
