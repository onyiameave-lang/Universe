"""
Forge.core.shadow_runner
========================
Native Python 3.10 Shadow & MT5 Demo Execution Runner for Forge V2 × Oracle V2 Strategies.

Features:
  1. Zero-Hardcoding Learned Necessity & Two-Tier Penalty Standard:
     - True Wins (`is_true_tp_win = True`) require reaching/expanding the Main Take Profit
       (or a verified near-TP reversal save).
     - Breakeven / early stops (`is_positive_scratch_only = True`) are NEVER counted as
       unpenalized full Wins; they receive a learned Minor Penalty proportional to the
       uncaptured Main Take Profit shortfall.
     - Full Stop-Loss hits receive a learned Major Penalty.
     - Unnecessary early stops (where post-exit price action reached the Main Take Profit
       without hitting the initial Stop Loss) receive an additional counterfactual cheating
       penalty in the Mistake Clinic.
  2. Direct Oracle MetaTrader 5 (`MT5Broker` + `TradeManager` + `MT5Logger`) Wiring:
     - Connects to `Oracle/execution/mt5_broker.py` (`MT5Broker`), `SymbolMapper`,
       `TradeManager` (configured via `TradeManagerConfig.from_learned_policy`), and `MT5Logger`.
     - Reads real MT5 account equity, balance, server, and symbol contract specs
       (`trade_contract_size`, `volume_min`, `volume_step`, `point`), computes broker-clamped
       MT5 lot sizes (`0.01` lot step), logs trades to `Oracle/logs/mt5_demo_trades.csv`,
       and supports demo order execution via `--mt5-demo`. Historical replay signals are
       evaluation-only; only a fresh latest signal can submit an order, once per signal.

CLI Usage (from repository root):
  python3 -m Forge.core.shadow_runner --symbol GBPUSD --style ALL --mt5-demo
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from Chronicle.core.dedup_ledger import ChronicleDeduplicatingLedger
from Forge.core.institutional_standard import (
    ExplainabilityAndNoTradeEngine,
    LearnedPenaltyAndNecessityEngine,
    PositionSizingAndSafetyEngine,
)
from Forge.core.mtf_institutional_engine import (
    ALL_WATCHLIST_SYMBOLS,
    LIVE_PRESET_SYMBOLS,
    MultiTimeframeInstitutionalEngine,
)


class MT5ShadowExecutionBridge:
    """
    Wires ForgeShadowRunner directly to Oracle's MetaTrader 5 execution stack:
      - `Oracle/execution/mt5_broker.py` (`MT5Broker`)
      - `Oracle/execution/mt5_demo_trader.py` (`SymbolMapper`)
      - `Oracle/execution/trade_manager.py` (`TradeManager`, `TradeManagerConfig`, `Position`, `MarketSnapshot`, `Direction`)
      - `Oracle/execution/mt5_logger.py` (`MT5Logger`)
    """

    def __init__(self, workspace_root: Path, execute_mt5_demo_orders: bool = False) -> None:
        self.workspace_root = workspace_root
        self.oracle_root = self.workspace_root / "Oracle"
        self.execute_mt5_demo_orders = execute_mt5_demo_orders
        self.broker: Any = None
        self.sym_mapper: Any = None
        self.mt5_logger: Any = None
        self.trade_manager_cls: Any = None
        self.trade_manager_cfg_cls: Any = None
        self.position_cls: Any = None
        self.snapshot_cls: Any = None
        self.direction_enum: Any = None
        self.connection_status: Dict[str, Any] = {}
        self.order_ledger_path = (
            self.oracle_root / "memory" / "forge_mt5_signal_orders.sqlite3"
        )
        self.equity_state_path = (
            self.oracle_root / "memory" / "forge_mt5_equity_state.json"
        )
        self._tracked_equity_usd: Optional[float] = None
        self._init_oracle_mt5_components()

    def _claim_demo_signal(self, signal_id: str) -> bool:
        self.order_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.order_ledger_path, timeout=10) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS submitted_signals (
                    signal_id TEXT PRIMARY KEY,
                    claimed_at_unix REAL NOT NULL
                )
                """
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO submitted_signals (signal_id, claimed_at_unix) VALUES (?, ?)",
                (signal_id, time.time()),
            )
            return cursor.rowcount == 1

    def _claim_reconciled_deal(self, position_id: int) -> bool:
        self.order_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.order_ledger_path, timeout=10) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciled_mt5_deals (
                    position_id INTEGER PRIMARY KEY,
                    reconciled_at_unix REAL NOT NULL
                )
                """
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO reconciled_mt5_deals (position_id, reconciled_at_unix) VALUES (?, ?)",
                (int(position_id), time.time()),
            )
            return cursor.rowcount == 1

    def _init_oracle_mt5_components(self) -> None:
        oracle_str = str(self.oracle_root)
        if oracle_str not in sys.path:
            sys.path.insert(0, oracle_str)
        for subpkg in ("core", "intelligence", "shared", "execution", "tools", "memory"):
            mod = sys.modules.get(subpkg)
            sub_dir = str(self.oracle_root / subpkg)
            if mod is not None and hasattr(mod, "__path__") and sub_dir not in mod.__path__:
                mod.__path__.insert(0, sub_dir)
        try:
            from execution.mt5_broker import MT5Broker  # type: ignore
            from execution.mt5_demo_trader import SymbolMapper  # type: ignore
            from execution.mt5_logger import TradeLogger as MT5Logger  # type: ignore
            from execution.trade_manager import (  # type: ignore
                Direction,
                MarketSnapshot,
                Position,
                TradeManager,
                TradeManagerConfig,
            )

            paper_mode = not self.execute_mt5_demo_orders
            self.broker = MT5Broker(paper=paper_mode, allow_live=False)
            self.connection_status = self.broker.connect()
            self.sym_mapper = SymbolMapper()
            broker_syms = self.broker.symbols() if self.broker.status.connected else []
            self.sym_mapper.build(broker_syms or list(ALL_WATCHLIST_SYMBOLS), list(ALL_WATCHLIST_SYMBOLS))
            self.mt5_logger = MT5Logger(
                trade_log="logs/mt5_demo_trades.csv",
                session_log="logs/mt5_demo_sessions.csv",
                oracle_root=self.oracle_root,
            )
            self.trade_manager_cls = TradeManager
            self.trade_manager_cfg_cls = TradeManagerConfig
            self.position_cls = Position
            self.snapshot_cls = MarketSnapshot
            self.direction_enum = Direction
        except Exception as exc:
            self.connection_status = {
                "connected": False,
                "account_type": "paper_fallback",
                "equity": 100000.0,
                "balance": 100000.0,
                "currency": "USD",
                "reason": f"Oracle MT5 module load fallback: {exc}",
            }

    def _load_persisted_equity(self) -> Optional[float]:
        equity_path = getattr(self, "equity_state_path", None)
        if equity_path and equity_path.exists():
            try:
                data = json.loads(equity_path.read_text(encoding="utf-8"))
                eq = float(data.get("equity_usd", 0.0) or 0.0)
                if eq > 0:
                    return eq
            except Exception:
                pass
        return None

    def _save_persisted_equity(self, equity_usd: float, source: str, balance_usd: Optional[float] = None) -> None:
        equity_path = getattr(self, "equity_state_path", None)
        if not equity_path:
            return
        try:
            equity_path.parent.mkdir(parents=True, exist_ok=True)
            equity_path.write_text(
                json.dumps(
                    {
                        "equity_usd": round(float(equity_usd), 2),
                        "balance_usd": round(float(balance_usd if balance_usd is not None else equity_usd), 2),
                        "source": source,
                        "updated_at_unix": time.time(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def get_account_equity_usd(self) -> float:
        """
        Always reads live MT5 account equity (`mt5.account_info().equity`) when MT5 is connected.
        When MT5 is offline, returns the compounding tracked shadow equity rather than resetting
        to a static $100,000 on every trade.
        """
        if (
            self.broker is not None
            and not getattr(self.broker, "paper", False)
            and hasattr(self.broker, "account")
        ):
            try:
                acct = self.broker.account()
                if isinstance(acct, dict) and str(acct.get("status", "")).lower() != "paper":
                    self.connection_status = acct
                    eq = float(acct.get("equity") or acct.get("balance") or 0.0)
                    bal = float(acct.get("balance") or eq)
                    if eq > 0:
                        self._tracked_equity_usd = round(eq, 2)
                        self._save_persisted_equity(eq, "mt5_live_account", bal)
                        return self._tracked_equity_usd
            except Exception:
                pass

        tracked = getattr(self, "_tracked_equity_usd", None)
        if tracked is not None and tracked > 0:
            return round(float(tracked), 2)

        persisted = self._load_persisted_equity()
        if persisted is not None and persisted > 0:
            self._tracked_equity_usd = round(persisted, 2)
            return self._tracked_equity_usd

        self._tracked_equity_usd = 100000.0
        return 100000.0

    def update_tracked_equity_after_trade(
        self,
        net_return_pct: float,
        realized_pnl_usd: Optional[float] = None,
    ) -> float:
        """
        Updates the tracked equity after a trade outcome:
          - If MT5 is live & connected (and not in paper fallback), re-queries live `mt5.account_info().equity`.
          - If offline/shadow mode, updates `_tracked_equity_usd` by `realized_pnl_usd` (if provided)
            or compounds by `net_return_pct` so position sizing dynamically reflects gains and drawdowns.
        """
        if (
            self.broker is not None
            and getattr(self.broker, "status", None)
            and self.broker.status.connected
            and not getattr(self.broker, "paper", False)
            and hasattr(self.broker, "account")
        ):
            return self.get_account_equity_usd()
        current_eq = self.get_account_equity_usd()
        if realized_pnl_usd is not None:
            updated_eq = round(max(100.0, current_eq + float(realized_pnl_usd)), 2)
        else:
            updated_eq = round(max(100.0, current_eq * (1.0 + (float(net_return_pct) / 100.0))), 2)
        self._tracked_equity_usd = updated_eq
        self._save_persisted_equity(updated_eq, "shadow_compounded_equity")
        return updated_eq

    def resolve_broker_symbol(self, symbol: str) -> str:
        if self.sym_mapper is not None:
            try:
                mapped = self.sym_mapper.translate(symbol.upper())
                if mapped:
                    return str(mapped)
            except Exception:
                pass
        return symbol.upper()

    def get_mt5_symbol_specs(self, symbol: str, entry_price: float) -> Dict[str, Any]:
        broker_sym = self.resolve_broker_symbol(symbol)
        base_specs = PositionSizingAndSafetyEngine.contract_specs_for_symbol(symbol, entry_price)
        specs = {
            "canonical_symbol": symbol.upper(),
            "broker_symbol": broker_sym,
            "trade_contract_size": float(base_specs["contract_units_per_lot"]),
            "point": float(base_specs["pip_size"]) / 10.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "stops_level_points": 10,
            "source": "mt5_terminal_live" if (self.broker and getattr(self.broker, "_mt5", None)) else "mt5_standard_contract_profile",
        }
        if self.broker is not None and getattr(self.broker, "_mt5", None) is not None:
            try:
                mt5 = self.broker._mt5
                info = mt5.symbol_info(broker_sym)
                if info is not None:
                    specs["trade_contract_size"] = float(getattr(info, "trade_contract_size", specs["trade_contract_size"]) or specs["trade_contract_size"])
                    specs["point"] = float(getattr(info, "point", specs["point"]) or specs["point"])
                    specs["volume_min"] = float(getattr(info, "volume_min", 0.01) or 0.01)
                    specs["volume_max"] = float(getattr(info, "volume_max", 100.0) or 100.0)
                    specs["volume_step"] = float(getattr(info, "volume_step", 0.01) or 0.01)
                    specs["stops_level_points"] = int(getattr(info, "trade_stops_level", 10) or 10)
            except Exception:
                pass
        return specs

    def evaluate_with_oracle_trade_manager(
        self,
        *,
        symbol: str,
        is_long: bool,
        entry_price: float,
        initial_stop: float,
        target_price: float,
        entry_regime: str,
        entry_atr: float,
        exit_bar: Dict[str, Any],
        management_policy: Dict[str, Any],
        sentinel_signal: float,
        pulse_signal: float,
        current_stop: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Runs Oracle's `TradeManager.evaluate()` initialized with `TradeManagerConfig.from_learned_policy(management_policy)`
        so we can inspect the exact MT5 Demo Trader stop/exit decision alongside the shadow trade.
        """
        if not (
            getattr(self, "trade_manager_cls", None)
            and getattr(self, "trade_manager_cfg_cls", None)
            and getattr(self, "position_cls", None)
            and getattr(self, "snapshot_cls", None)
        ):
            return {"mt5_trade_manager_action": "hold", "mt5_trade_manager_reason": "trade_manager_unavailable"}

        cfg = self.trade_manager_cfg_cls.from_learned_policy(management_policy)
        mgr = self.trade_manager_cls(config=cfg)
        dir_val = self.direction_enum.BUY if is_long else self.direction_enum.SELL
        emp_probs = management_policy.get("empirical_probabilities") or {}
        learned_entry_conf = float(emp_probs.get("empirical_major_tp_hit_probability", 0.55))
        pos = self.position_cls(
            symbol=symbol.upper(),
            direction=dir_val,
            entry_price=entry_price,
            initial_stop=initial_stop,
            initial_target=target_price,
            entry_confidence=learned_entry_conf,
            entry_regime=entry_regime,
            entry_atr=entry_atr,
            execution_environment="demo",
        )
        if current_stop is not None:
            pos.current_stop = float(current_stop)
        close_p = float(exit_bar["close"])
        cur_atr = float(exit_bar.get("atr14", entry_atr))
        cur_regime = str(exit_bar.get("regime", entry_regime))
        dir_sign = 1.0 if is_long else -1.0
        invalidation_cut = float(management_policy.get("learned_early_invalidation_thesis_cutoff", -0.25))
        news_imp = "high" if (sentinel_signal * dir_sign <= invalidation_cut) else "none"
        soc_risk = "high" if (pulse_signal * dir_sign <= invalidation_cut) else "none"
        snap = self.snapshot_cls(
            price=close_p,
            confidence=learned_entry_conf,
            regime=cur_regime,
            atr=cur_atr,
            news_impact=news_imp,
            social_risk=soc_risk,
        )
        dec = mgr.evaluate(pos, snap)
        return {
            "mt5_trade_manager_action": str(dec.action.value),
            "mt5_trade_manager_new_stop": round(float(dec.new_stop), 5) if dec.new_stop is not None else None,
            "mt5_trade_manager_reason": str(dec.reason),
            "mt5_trade_manager_unrealized_r": round(float(dec.unrealized_r), 3),
            "learned_config_used": {
                "trail_activation_r": cfg.trail_activation_r,
                "trail_distance_r": cfg.trail_distance_r,
                "confidence_drop_threshold": cfg.confidence_drop_threshold,
                "confidence_collapse_ratio": cfg.confidence_collapse_ratio,
                "volatility_spike_multiple": cfg.volatility_spike_multiple,
            },
        }

    def manage_open_mt5_positions(
        self,
        *,
        symbol: str,
        latest_bar: Dict[str, Any],
        macro_bars: List[Dict[str, Any]],
        setup_bars: List[Dict[str, Any]],
        htf_bars: List[Dict[str, Any]],
        learned_genome: Dict[str, Any],
        management_policy: Dict[str, Any],
        sentinel_signal: float,
        pulse_signal: float,
        mtf_engine: Any,
    ) -> Dict[str, Any]:
        """
        Actively manages all currently open MT5 positions for `symbol`:
          - Queries open MT5 positions via `get_positions_by_symbol` / `observed_positions_by_symbol`.
          - Runs both Oracle's `TradeManager.evaluate()` and Forge's learned necessity & dynamic
            re-analysis engine (`evaluate_live_position_reanalysis`) on each open ticket.
          - Executes `close_position(ticket)` on MT5 when a learned necessity condition demands
            an early exit or near-TP reversal save.
          - Executes `modify_position(ticket, stop_loss=..., take_profit=...)` on MT5 when
            stop-tightening or dynamic HTF Major TP expansion triggers.
        """
        broker_sym = self.resolve_broker_symbol(symbol)
        if self.broker is None or not getattr(self.broker, "status", None) or not self.broker.status.connected:
            return {
                "mt5_connected": False,
                "open_positions_count": 0,
                "positions": [],
                "actions_taken": [],
            }

        open_positions: List[Dict[str, Any]] = []
        if hasattr(self.broker, "observed_positions_by_symbol"):
            open_positions = self.broker.observed_positions_by_symbol(broker_sym) or []
        elif hasattr(self.broker, "get_positions_by_symbol"):
            open_positions = self.broker.get_positions_by_symbol(broker_sym) or []

        actions_taken: List[Dict[str, Any]] = []
        close_p = float(latest_bar.get("close", 0.0) or 0.0)
        cur_atr = max(float(latest_bar.get("atr14", 0.0005) or 0.0005), close_p * 1e-4)
        mt5_specs = self.get_mt5_symbol_specs(symbol, close_p if close_p > 0 else 1.0)
        min_broker_stop = float(mt5_specs["point"]) * max(float(mt5_specs["stops_level_points"]), 10.0)

        for pos in open_positions:
            ticket = int(pos.get("ticket", 0) or 0)
            if ticket <= 0:
                continue
            is_long = str(pos.get("type", "buy")).lower() == "buy"
            entry_price = float(pos.get("price_open", close_p) or close_p)
            cur_sl = float(pos.get("sl", 0.0) or 0.0)
            cur_tp = float(pos.get("tp", 0.0) or 0.0)
            sl_atr_mult = float(management_policy.get("learned_optimal_stop_atr_mult", 1.2))
            inferred_stop_dist = (
                abs(entry_price - cur_sl)
                if cur_sl > 0 and ((is_long and cur_sl < entry_price) or (not is_long and cur_sl > entry_price))
                else max(cur_atr * sl_atr_mult, min_broker_stop)
            )
            initial_stop = (
                entry_price - inferred_stop_dist if is_long else entry_price + inferred_stop_dist
            )
            learned_tp_r = float(management_policy.get("learned_initial_major_tp_r", 2.0))
            active_tp = (
                cur_tp
                if cur_tp > 0
                else (entry_price + inferred_stop_dist * learned_tp_r if is_long else entry_price - inferred_stop_dist * learned_tp_r)
            )
            current_tp_r = max(0.5, abs(active_tp - entry_price) / max(inferred_stop_dist, 1e-9))
            unrealized_r = (
                (close_p - entry_price) / max(inferred_stop_dist, 1e-9)
                if is_long
                else (entry_price - close_p) / max(inferred_stop_dist, 1e-9)
            )

            tm_eval = self.evaluate_with_oracle_trade_manager(
                symbol=symbol,
                is_long=is_long,
                entry_price=entry_price,
                initial_stop=initial_stop,
                target_price=active_tp,
                entry_regime=str(latest_bar.get("regime", "CONSOLIDATION_RANGING")),
                entry_atr=cur_atr,
                exit_bar=latest_bar,
                management_policy=management_policy,
                sentinel_signal=sentinel_signal,
                pulse_signal=pulse_signal,
                current_stop=cur_sl if cur_sl > 0 else initial_stop,
            )

            fav_r = max(0.0, unrealized_r)
            adv_r = max(0.0, -unrealized_r)
            reanalysis = mtf_engine.evaluate_live_position_reanalysis(
                bar=latest_bar,
                macro_bars=macro_bars,
                setup_bars=setup_bars,
                is_long=is_long,
                entry_price=entry_price,
                stop_distance=inferred_stop_dist,
                current_stop=cur_sl if cur_sl > 0 else initial_stop,
                current_major_tp_r=current_tp_r,
                fav_r=fav_r,
                adv_r=adv_r,
                peak_mfe_r=fav_r,
                learned_genome=learned_genome,
                sentinel_signal=sentinel_signal,
                pulse_signal=pulse_signal,
                policy=management_policy,
                entry_confidence=float(
                    (management_policy.get("empirical_probabilities") or {}).get(
                        "empirical_major_tp_hit_probability", 0.55
                    )
                ),
                entry_regime=str(latest_bar.get("regime", "CONSOLIDATION_RANGING")),
                entry_atr=cur_atr,
            )

            tm_action = str(tm_eval.get("mt5_trade_manager_action", "hold"))
            necessity_demanded = bool(reanalysis.get("necessity_demanded", False))
            early_cut_needed = necessity_demanded and bool(reanalysis.get("thesis_broken_in_drawdown", False))
            near_tp_save_needed = necessity_demanded and bool(reanalysis.get("near_tp_reversal_detected", False))

            decision = "hold"
            reason = str(tm_eval.get("mt5_trade_manager_reason", "Thesis intact"))
            broker_res: Dict[str, Any] = {"status": "held", "ticket": ticket}

            if tm_action == "close" or early_cut_needed or near_tp_save_needed:
                decision = "close_position"
                if early_cut_needed:
                    reason = "Necessity-gated early loss cut: thesis broken in drawdown"
                elif near_tp_save_needed:
                    reason = "Necessity-gated near-TP reversal save"
                if (
                    self.execute_mt5_demo_orders
                    and self.broker.status.account_type == "demo"
                    and hasattr(self.broker, "close_position")
                ):
                    broker_res = self.broker.close_position(ticket)
                else:
                    broker_res = {"status": "close_recommended_paper_mode", "ticket": ticket}
            else:
                candidate_sl: Optional[float] = None
                candidate_tp: Optional[float] = None

                tm_sl = tm_eval.get("mt5_trade_manager_new_stop")
                re_sl = reanalysis.get("fitted_stop_price")
                for prop_sl in (tm_sl, re_sl):
                    if prop_sl is not None:
                        psl = float(prop_sl)
                        if is_long and psl < close_p - min_broker_stop:
                            if cur_sl <= 0 or psl > cur_sl + min_broker_stop * 0.25:
                                candidate_sl = max(candidate_sl or psl, psl)
                        elif not is_long and psl > close_p + min_broker_stop:
                            if cur_sl <= 0 or psl < cur_sl - min_broker_stop * 0.25:
                                candidate_sl = min(candidate_sl or psl, psl)

                if bool(reanalysis.get("can_expand_major_tp")) and reanalysis.get("next_major_tp_r") is not None:
                    next_tp_r = float(reanalysis["next_major_tp_r"])
                    new_tp_val = (
                        entry_price + inferred_stop_dist * next_tp_r
                        if is_long
                        else entry_price - inferred_stop_dist * next_tp_r
                    )
                    if (is_long and new_tp_val > max(cur_tp, close_p + min_broker_stop)) or (
                        not is_long and (cur_tp <= 0 or new_tp_val < min(cur_tp, close_p - min_broker_stop))
                    ):
                        candidate_tp = round(new_tp_val, 5)

                if candidate_sl is not None or candidate_tp is not None:
                    decision = "modify_position"
                    reason = str(tm_eval.get("mt5_trade_manager_reason") or "Necessity-gated stop/target update")
                    if (
                        self.execute_mt5_demo_orders
                        and self.broker.status.account_type == "demo"
                        and hasattr(self.broker, "modify_position")
                    ):
                        broker_res = self.broker.modify_position(
                            ticket,
                            stop_loss=round(candidate_sl, 5) if candidate_sl is not None else None,
                            take_profit=round(candidate_tp, 5) if candidate_tp is not None else None,
                        )
                    else:
                        broker_res = {
                            "status": "modify_recommended_paper_mode",
                            "ticket": ticket,
                            "proposed_sl": round(candidate_sl, 5) if candidate_sl is not None else cur_sl,
                            "proposed_tp": round(candidate_tp, 5) if candidate_tp is not None else cur_tp,
                        }

            actions_taken.append(
                {
                    "ticket": ticket,
                    "symbol": broker_sym,
                    "direction": "LONG" if is_long else "SHORT",
                    "volume_lots": float(pos.get("volume", 0.0) or 0.0),
                    "entry_price": entry_price,
                    "current_price": close_p,
                    "current_sl": cur_sl,
                    "current_tp": cur_tp,
                    "floating_profit_usd": float(pos.get("profit", 0.0) or 0.0),
                    "unrealized_r": round(unrealized_r, 3),
                    "decision": decision,
                    "reason": reason,
                    "broker_result": broker_res,
                }
            )

        return {
            "mt5_connected": True,
            "open_positions_count": len(open_positions),
            "positions": open_positions,
            "actions_taken": actions_taken,
        }

    def reconcile_mt5_closed_deals(
        self,
        *,
        symbol: str,
        management_policy: Dict[str, Any],
        feedback_outbox_path: Optional[Path] = None,
        lookback_days: int = 7,
    ) -> Dict[str, Any]:
        """
        Reconciles terminal win/loss reporting against real closed MT5 broker deals
        (`mt5.history_deals_get`), grades each closed MT5 position under the Two-Tier
        Penalty Standard, and feeds real MT5 losses/penalties into the Mistake Clinic.
        """
        broker_sym = self.resolve_broker_symbol(symbol)
        if (
            self.broker is None
            or not getattr(self.broker, "status", None)
            or not self.broker.status.connected
            or not hasattr(self.broker, "get_closed_deals")
        ):
            return {
                "mt5_connected": False,
                "closed_deals_count": 0,
                "mt5_real_wins": 0,
                "mt5_real_losses": 0,
                "mt5_real_scratches": 0,
                "mt5_real_win_rate_pct": 0.0,
                "mt5_real_net_profit_usd": 0.0,
                "reconciled_deals": [],
            }

        deals = self.broker.get_closed_deals(symbol=broker_sym, lookback_days=lookback_days) or []
        equity_now = max(self.get_account_equity_usd(), 100.0)
        unit_risk_usd = max(equity_now * 0.005, 10.0)
        target_r = float(management_policy.get("learned_initial_major_tp_r", 2.0))

        reconciled: List[Dict[str, Any]] = []
        wins = 0
        losses = 0
        scratches = 0
        net_profit_total = 0.0

        for d in deals:
            pos_id = int(d.get("position_id", 0) or 0)
            net_usd = float(d.get("net_profit_usd", 0.0) or 0.0)
            net_profit_total += net_usd
            est_r = round(net_usd / unit_risk_usd, 4)
            exit_reason = str(d.get("exit_reason", "CLIENT_OR_EXPERT_CLOSE"))

            if exit_reason == "TAKE_PROFIT" or est_r >= target_r:
                status_tag = "CLOSED_AT_MAJOR_TP"
            elif exit_reason == "STOP_LOSS" and net_usd < 0:
                status_tag = "CLOSED_INITIAL_STOP_LOSS"
            elif net_usd < 0:
                status_tag = "CLOSED_EARLY_LOSS_CUT_BEFORE_SL"
            else:
                status_tag = "CLOSED_BREAKEVEN_STOP_MINOR_PENALTY"

            audit = LearnedPenaltyAndNecessityEngine.grade_closed_trade_with_penalties(
                realized_r=est_r,
                initial_major_tp_r=target_r,
                final_major_tp_r=target_r,
                shadow_status=status_tag,
                policy=management_policy,
            )
            if audit["is_true_tp_win"]:
                wins += 1
            elif net_usd < 0:
                losses += 1
            else:
                scratches += 1

            deal_entry = {
                **d,
                "estimated_realized_r": est_r,
                "penalty_audit": audit,
            }
            reconciled.append(deal_entry)

            if (
                feedback_outbox_path is not None
                and pos_id > 0
                and audit["penalty_tier"] != "NONE"
                and self._claim_reconciled_deal(pos_id)
            ):
                fb_event = {
                    "event_id": f"mt5-deal-fb-{pos_id}-{symbol.upper()}",
                    "event_type": (
                        "forge.mt5_live_deal.major_sl_penalty"
                        if audit["penalty_tier"] == "MAJOR_PENALTY"
                        else "forge.mt5_live_deal.minor_early_exit_penalty"
                    ),
                    "source": "mt5_real_broker_deal_reconciliation",
                    "symbol": symbol.upper(),
                    "broker_symbol": broker_sym,
                    "position_id": pos_id,
                    "direction": d.get("direction"),
                    "volume_lots": d.get("volume_lots"),
                    "entry_price": d.get("entry_price"),
                    "exit_price": d.get("exit_price"),
                    "net_profit_usd": net_usd,
                    "realized_r": est_r,
                    "applied_penalty_r": audit["applied_penalty_r"],
                    "penalty_adjusted_r": audit["penalty_adjusted_r"],
                    "penalty_tier": audit["penalty_tier"],
                    "exit_reason": exit_reason,
                    "timestamp_unix": time.time(),
                }
                try:
                    with feedback_outbox_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(fb_event) + "\n")
                except Exception:
                    pass

        total_cnt = len(reconciled)
        return {
            "mt5_connected": True,
            "closed_deals_count": total_cnt,
            "mt5_real_wins": wins,
            "mt5_real_losses": losses,
            "mt5_real_scratches": scratches,
            "mt5_real_win_rate_pct": round((wins / max(1, total_cnt)) * 100.0, 1) if total_cnt > 0 else 0.0,
            "mt5_real_net_profit_usd": round(net_profit_total, 2),
            "reconciled_deals": reconciled[-25:],
        }

    def execute_or_log_mt5_demo_trade(
        self,
        *,
        cycle: int,
        symbol: str,
        direction: str,
        confidence: float,
        regime: str,
        sizing_info: Dict[str, Any],
        entry_price: float,
        stop_price: float,
        target_price: float,
        reward_risk: float,
        shadow_status: str,
        realized_r: float,
        penalty_audit: Dict[str, Any],
        current_signal: bool = False,
        latest_signal: bool = False,
        signal_id: str = "",
        equity_override_usd: Optional[float] = None,
    ) -> Dict[str, Any]:
        broker_sym = self.resolve_broker_symbol(symbol)
        lots = float(sizing_info.get("mt5_broker_lots", sizing_info.get("position_lots", 0.01)))
        units = float(sizing_info.get("position_units", 1000.0))
        equity_now = float(equity_override_usd) if equity_override_usd is not None else self.get_account_equity_usd()
        order_result: Dict[str, Any] = {
            "broker_symbol": broker_sym,
            "mt5_broker_lots": lots,
            "mt5_connected": bool(self.broker and self.broker.status.connected),
            "paper_mode": bool(self.broker.paper) if self.broker else True,
            "account_equity_usd": round(equity_now, 2),
            "execution_status": "shadow_logged_to_mt5_csv",
        }
        if self.execute_mt5_demo_orders and not latest_signal:
            order_result["execution_status"] = "historical_replay_not_executed"
        elif self.execute_mt5_demo_orders and not current_signal:
            order_result["execution_status"] = "stale_signal_not_executed"
        elif self.execute_mt5_demo_orders and self.broker is not None and self.broker.status.connected:
            if self.broker.status.account_type != "demo":
                order_result["execution_status"] = "blocked_non_demo_account"
            else:
                # Check if an open MT5 position already exists on this symbol to prevent duplicate/opposing stacking
                existing_positions: List[Dict[str, Any]] = []
                if hasattr(self.broker, "get_positions_by_symbol"):
                    existing_positions = self.broker.get_positions_by_symbol(broker_sym) or []
                desired_side = "buy" if direction.upper() == "LONG" else "sell"

                def _norm_side(raw_t: Any) -> str:
                    if raw_t in (0, "0", "buy", "BUY", "long", "LONG"):
                        return "buy"
                    return "sell"

                opposing_pos = next((p for p in existing_positions if _norm_side(p.get("type")) != desired_side), None)
                same_pos = next((p for p in existing_positions if _norm_side(p.get("type")) == desired_side), None)

                if opposing_pos is not None:
                    order_result["execution_status"] = "existing_opposing_position_open"
                    order_result["existing_ticket"] = int(opposing_pos.get("ticket", 0) or 0)
                elif same_pos is not None:
                    order_result["execution_status"] = "existing_same_direction_position_open"
                    order_result["existing_ticket"] = int(same_pos.get("ticket", 0) or 0)
                elif not signal_id or not self._claim_demo_signal(signal_id):
                    order_result["execution_status"] = "duplicate_signal_skipped"
                else:
                    plan = {
                        "approved": True,
                        "symbol": symbol.upper(),
                        "broker_symbol": broker_sym,
                        "direction": "long" if direction.upper() == "LONG" else "short",
                        "size": units,
                        "entry": entry_price,
                        "stop": stop_price,
                        "target": target_price,
                        "rebase_stops_to_market": True,
                        "max_entry_deviation": abs(entry_price - stop_price) * 0.5,
                    }
                    res = self.broker.place_order(plan, human_confirm=False)
                    order_result["execution_status"] = res.get("status", "filled")
                    order_result["ticket"] = res.get("order") or res.get("ticket")
                    order_result["retcode"] = res.get("retcode", "")
                    order_result["price"] = res.get("price", entry_price)
                    order_result["volume_lots"] = res.get("volume_lots", lots)
                    order_result["stop"] = res.get("sl", stop_price)
                    order_result["target"] = res.get("tp", target_price)
        elif self.execute_mt5_demo_orders:
            order_result["execution_status"] = "mt5_not_connected"

        should_log_demo_trade = (
            not self.execute_mt5_demo_orders or current_signal
        )
        if self.mt5_logger is not None and should_log_demo_trade:
            try:
                if not getattr(self.mt5_logger, "_session_id", ""):
                    self.mt5_logger.start_session(
                        start_equity=equity_now,
                        paper=not self.execute_mt5_demo_orders,
                    )
                self.mt5_logger.log_trade(
                    cycle=cycle,
                    symbol=symbol.upper(),
                    broker_symbol=broker_sym,
                    direction=direction.lower(),
                    confidence=confidence,
                    regime=regime,
                    size=float(order_result.get("volume_lots", lots)),
                    price=float(order_result.get("price", entry_price)),
                    stop=float(order_result.get("stop", stop_price)),
                    target=float(order_result.get("target", target_price)),
                    reward_risk=reward_risk,
                    outcome=(
                        order_result["execution_status"]
                        if self.execute_mt5_demo_orders
                        else str(penalty_audit.get("outcome_class", shadow_status))
                    ),
                    retcode=order_result.get("retcode", ""),
                    reason=f"signal_id={signal_id} | realized_r={realized_r:+.3f}R | penalty={penalty_audit.get('applied_penalty_r', 0.0):.3f}R ({penalty_audit.get('penalty_tier')})",
                    account_type=(
                        self.broker.status.account_type
                        if self.broker is not None
                        else "unknown"
                    ),
                    equity_before=equity_now,
                    note=f"cf={penalty_audit.get('counterfactual_audit')}",
                )
                order_result["mt5_csv_trade_log"] = self.mt5_logger.paths().get("trades")
            except Exception as exc:
                order_result["logger_warning"] = str(exc)

        return order_result


class ForgeShadowRunner:
    """
    Shadow & MT5 Demo execution and monitoring engine for Forge V2 strategies.
    Enforces the Learned Two-Tier Penalty (Major SL Penalty vs. Minor Early-Exit Penalty)
    and Necessity-Only Stop/Exit Standard while wiring directly into Oracle's MT5 Broker & TradeManager.
    """

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        execute_mt5_demo_orders: bool = False,
    ) -> None:
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self.mtf_engine = MultiTimeframeInstitutionalEngine(self.workspace_root)
        self.chronicle_ledger = ChronicleDeduplicatingLedger(self.workspace_root)
        env_mt5 = os.getenv("FORGE_MT5_DEMO_EXECUTION", "false").lower() in ("true", "1", "yes")
        self.execute_mt5_demo_orders = execute_mt5_demo_orders or env_mt5
        self.mt5_bridge = MT5ShadowExecutionBridge(
            self.workspace_root, execute_mt5_demo_orders=self.execute_mt5_demo_orders
        )
        self.forge_mem_dir = self.workspace_root / "Forge" / "memory"
        self.forge_mem_dir.mkdir(parents=True, exist_ok=True)
        self.shadow_ledger_path = self.forge_mem_dir / "shadow_trades_ledger.json"
        self.candidates_dir = (
            self.workspace_root / "Chronicle" / "memory" / "research" / "forge_candidates"
        )
        self.cost_profile_dir = (
            self.workspace_root / "Chronicle" / "memory" / "research" / "oracle_cost_profiles"
        )
        self.oracle_mem_dir = self.workspace_root / "Oracle" / "memory"
        self.oracle_mem_dir.mkdir(parents=True, exist_ok=True)
        self.feedback_outbox_path = (
            self.oracle_mem_dir / "forge_strategy_feedback_outbox.jsonl"
        )

    def _read_json(self, path: Path, fallback: Any) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return fallback

    def load_v1_demo_trade_timestamps(self, symbol: str) -> List[int]:
        """Loads timestamps where Oracle V1 placed demo trades so we know when V1 was silent."""
        upper = symbol.upper()
        closed_path = self.oracle_mem_dir / "learned_closed_trades.json"
        raw = self._read_json(closed_path, {})
        ts_list: List[int] = []
        if isinstance(raw, dict):
            for _, trade in raw.items():
                if isinstance(trade, dict) and str(trade.get("symbol", "")).upper() == upper:
                    ts = trade.get("open_time_unix") or trade.get("closed_at_unix") or 0
                    if ts:
                        ts_list.append(int(float(ts)))
        ts_list.sort()
        return ts_list

    def did_oracle_v1_trade_near(
        self, v1_timestamps: List[int], bar_unix_sec: int, window_sec: int = 14400
    ) -> bool:
        """Returns True if Oracle V1 placed a demo trade within +/- window_sec of bar_unix_sec."""
        if not v1_timestamps:
            return False
        idx = bisect.bisect_left(v1_timestamps, bar_unix_sec - window_sec)
        if idx < len(v1_timestamps) and abs(v1_timestamps[idx] - bar_unix_sec) <= window_sec:
            return True
        return False

    def evaluate_v1_legacy_signal(self, d1_bar: Optional[Dict[str, Any]]) -> str:
        """Checks whether Oracle V1's legacy D1-only logic would even permit a trade."""
        if not d1_bar:
            return "V1_NO_DAILY_BAR"
        er = float(d1_bar.get("efficiency_ratio", 0.0))
        rsi = float(d1_bar.get("rsi14", 50.0))
        if er < 0.36:
            return "V1_BLOCKED_LOW_DAILY_EFFICIENCY"
        if d1_bar["ema9"] > d1_bar["ema21"] and d1_bar["close"] > d1_bar["ema9"] and 52 <= rsi <= 68:
            return "V1_DAILY_LONG_ACTIVE"
        if d1_bar["ema9"] < d1_bar["ema21"] and d1_bar["close"] < d1_bar["ema9"] and 32 <= rsi <= 48:
            return "V1_DAILY_SHORT_ACTIVE"
        return "V1_IDLE_WAITING_FOR_DAILY_CLOSE"

    def load_v1_api_bridge_state(self, symbol: str) -> Dict[str, Any]:
        upper = symbol.upper()
        kv_path = (
            self.workspace_root / "Chronicle" / "memory" / "store" / "kv_state.json"
        )
        kv_store = self._read_json(kv_path, {})
        summaries: List[Dict[str, Any]] = []
        latest_heartbeat: Optional[Dict[str, Any]] = None
        for k, entry in kv_store.items():
            if not isinstance(entry, dict):
                continue
            val = entry.get("value")
            if not isinstance(val, dict):
                continue
            if k.startswith("forge.oracle.decision_summary.") and str(val.get("symbol", "")).upper() == upper:
                summaries.append(val)
            elif k.startswith("forge.oracle.heartbeat."):
                if latest_heartbeat is None or float(val.get("last_seen_at_unix", 0)) > float(
                    latest_heartbeat.get("last_seen_at_unix", 0)
                ):
                    latest_heartbeat = val

        summaries.sort(key=lambda s: float(s.get("last_event_at_unix") or 0), reverse=True)
        top_summary = summaries[0] if summaries else {}
        latest_event = top_summary.get("latest_event") or {}
        return {
            "bridge_endpoint": "POST /internal/forge/act (oracle.decision.observation & oracle.trader.heartbeat)",
            "active": True,
            "symbol": upper,
            "evaluations_total": int(top_summary.get("evaluations", 0) or 0),
            "evaluations_since_last_trade": int(
                top_summary.get("evaluations_since_last_trade", 0) or 0
            ),
            "outcome_counts_since_last_trade": top_summary.get(
                "outcome_counts_since_last_trade", {}
            ),
            "latest_v1_api_outcome_code": latest_event.get("outcome_code")
            or "AWAITING_V1_API_CYCLE",
            "latest_v1_api_reason_codes": latest_event.get("reason_codes") or [],
            "last_v1_decision_at_unix": top_summary.get("last_event_at_unix"),
            "last_v1_heartbeat_at_unix": (
                latest_heartbeat.get("last_seen_at_unix") if latest_heartbeat else None
            ),
            "v1_heartbeat_instance": (
                latest_heartbeat.get("instance_id") if latest_heartbeat else None
            ),
        }

    def load_v2_candidate_blueprint(
        self, symbol: str, trader_style: str
    ) -> Optional[Dict[str, Any]]:
        cand_id = f"cand-v2-{symbol.lower()}-{trader_style.lower()}"
        kv_path = (
            self.workspace_root / "Chronicle" / "memory" / "store" / "kv_state.json"
        )
        kv_store = self._read_json(kv_path, {})
        entry = kv_store.get(f"forge.core.candidate.{cand_id}") or kv_store.get(
            f"forge.candidates.{cand_id}"
        )
        raw = entry.get("value") if isinstance(entry, dict) else None
        if not raw or not isinstance(raw, dict):
            cand_path = self.candidates_dir / f"{cand_id}.json"
            raw = self._read_json(cand_path, None)
        if not raw or not isinstance(raw, dict):
            return None
        bp = raw.get("v1_compatibility_export")
        if not bp or not isinstance(bp, dict):
            return None
        return {
            "candidate_id": cand_id,
            "lifecycle_state": raw.get("lifecycle_state", "EVALUATED"),
            "blueprint": bp,
        }

    def load_verified_cost_bps(self, symbol: str) -> float:
        cp_path = self.cost_profile_dir / symbol.upper() / "latest.json"
        raw = self._read_json(cp_path, {})
        p = raw.get("profile") or raw
        comm = float(p.get("commission_bps", 0.35))
        spread = float(p.get("spread_bps", 1.10))
        slip = float(p.get("slippage_bps", 0.30))
        return round(comm + spread + slip, 4)

    @staticmethod
    def _find_latest_completed_bar(
        series: List[Dict[str, Any]], unix_sec: int
    ) -> Optional[Dict[str, Any]]:
        if not series:
            return None
        lo, hi = 0, len(series) - 1
        best: Optional[Dict[str, Any]] = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if int(series[mid]["unix_sec"]) <= unix_sec:
                best = series[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def run_shadow_session(
        self,
        symbol: str = "GBPUSD",
        trader_style: str = "ALL",
        lookback_bars: int = 260,
        refresh_live: bool = True,
    ) -> Dict[str, Any]:
        """
        Runs the Shadow & MT5 Demo Runner across the 7-timeframe market feed for `symbol`:
          - Uses empirically learned Necessity-Only Stop/Exit conditions (zero hardcoded thresholds).
          - Grades every trade under the Two-Tier Penalty Standard (Major SL Penalty vs. Minor
            Early-Exit/Breakeven Penalty + Post-Exit Counterfactual Cheating Audit).
          - Connects directly to Oracle's `MT5Broker`, `SymbolMapper`, `TradeManager`, and `MT5Logger`
            for broker-accurate position sizing, lot-step clamping, and MT5 demo execution/logging.
        """
        upper_sym = symbol.upper()
        dataset = (
            self.mtf_engine.fetch_live_7tf_dataset(upper_sym, max_age_sec=90)
            if refresh_live
            else self.mtf_engine.load_cached_dataset(upper_sym)
        )
        if not dataset:
            return {
                "status": "error",
                "message": f"No 7-timeframe dataset cached for {upper_sym}. Run 7-TF ingestion first.",
            }

        styles = (
            ["SCALPING", "DAY_TRADING", "SWING_TRADING", "RANGE_SPECIALIST"]
            if trader_style.upper() == "ALL"
            else [trader_style.upper()]
        )

        v1_timestamps = self.load_v1_demo_trade_timestamps(upper_sym)
        v1_api_bridge = self.load_v1_api_bridge_state(upper_sym)
        total_cost_bps = self.load_verified_cost_bps(upper_sym)
        if isinstance(dataset.get("live_spread_bps"), (int, float)) and float(dataset["live_spread_bps"]) > 0:
            cp_raw = self._read_json(self.cost_profile_dir / upper_sym / "latest.json", {})
            p_inner = cp_raw.get("profile") or cp_raw
            comm_bps = float(p_inner.get("commission_bps", 0.35))
            slip_bps = float(p_inner.get("slippage_bps", 0.30))
            total_cost_bps = round(float(dataset["live_spread_bps"]) + comm_bps + slip_bps, 4)

        starting_equity_usd = self.mt5_bridge.get_account_equity_usd()
        account_equity_usd = starting_equity_usd

        shadow_trades: List[Dict[str, Any]] = []
        style_summaries: Dict[str, Any] = {}
        trade_cycle_counter = 0
        latest_management_policy: Dict[str, Any] = {}
        latest_learned_genome: Dict[str, Any] = {}

        for style in styles:
            loaded = self.load_v2_candidate_blueprint(upper_sym, style)
            if not loaded:
                self.mtf_engine.train_and_certify_symbol_strategies(upper_sym)
                loaded = self.load_v2_candidate_blueprint(upper_sym, style)
            if not loaded:
                continue

            bp = loaded["blueprint"]
            management_policy = bp.get("dynamic_trade_management_policy") or {}
            # If candidate on disk predates the learned two-tier penalty & necessity engine, retrain it now
            if not management_policy.get("zero_hardcoded_parameters_verified"):
                self.mtf_engine.train_and_certify_symbol_strategies(
                    upper_sym, styles=[style], force_refresh_data=False
                )
                loaded = self.load_v2_candidate_blueprint(upper_sym, style) or loaded
                bp = loaded["blueprint"]
                management_policy = bp.get("dynamic_trade_management_policy") or {}

            latest_management_policy = management_policy
            latest_learned_genome = bp.get("learned_strategy_genome") or {}

            stack = bp.get("top_down_stack", {})
            trigger_tf = stack.get("trigger_timeframe", "M15")
            macro_tfs = stack.get("macro_timeframes", ["D1", "H4"])
            setup_tfs = stack.get("setup_timeframes", ["H1", "M30"])

            trigger_series = dataset.get("series", {}).get(trigger_tf, [])
            if len(trigger_series) < 40:
                continue

            best_sessions = bp.get("recommended_sessions", [])
            best_adaptive = bp.get("recommended_adaptive_sessions", [])
            best_regimes = bp.get("recommended_regimes", [])

            exc = bp.get("learned_excursion_parameters", {})
            sl_atr_mult = float(
                management_policy.get(
                    "learned_optimal_stop_atr_mult",
                    exc.get("learned_optimal_stop_atr_mult", 1.2),
                )
            )

            news_social_ctx = self.mtf_engine.fetch_pulse_and_sentinel_context(
                upper_sym, trigger_series
            )
            sentinel_scores = news_social_ctx["sentinel_bar_scores"]
            pulse_scores = news_social_ctx["pulse_bar_scores"]

            horizon_bars = (
                12 if style == "SCALPING" else (16 if style in ("DAY_TRADING", "RANGE_SPECIALIST") else 24)
            )
            start_idx = max(30, len(trigger_series) - lookback_bars)
            end_idx = len(trigger_series)
            trigger_interval_sec = {
                "M5": 5 * 60,
                "M15": 15 * 60,
                "M30": 30 * 60,
                "H1": 60 * 60,
            }.get(trigger_tf, 60 * 60)

            style_trades: List[Dict[str, Any]] = []
            i = start_idx
            while i < end_idx - 1:
                bar = trigger_series[i]
                unix_sec = int(bar["unix_sec"])
                latest_signal = i == end_idx - 2
                signal_age_sec = time.time() - unix_sec
                current_signal = (
                    latest_signal
                    and -60 <= signal_age_sec <= 2 * trigger_interval_sec
                )

                # 1. Check Adaptive Session & Regime filters
                if bar.get("adaptive_session") == "ROLLOVER_SPREAD_GUARD":
                    i += 1
                    continue
                if (
                    best_sessions
                    and bar.get("session") not in best_sessions
                    and bar.get("adaptive_session") not in best_adaptive
                ):
                    i += 1
                    continue
                if best_regimes and bar.get("regime") not in best_regimes:
                    i += 1
                    continue

                # 2. Check Top-Down 7-Timeframe Context + Pulse/Sentinel Agents
                macro_bars = [
                    b
                    for tf in macro_tfs
                    if (b := self._find_latest_completed_bar(dataset["series"].get(tf, []), unix_sec))
                ]
                setup_bars = [
                    b
                    for tf in setup_tfs
                    if (b := self._find_latest_completed_bar(dataset["series"].get(tf, []), unix_sec))
                ]
                s_val = float(sentinel_scores[i]) if i < len(sentinel_scores) else 0.0
                p_val = float(pulse_scores[i]) if i < len(pulse_scores) else 0.0
                sig = self.mtf_engine.evaluate_trader_archetype_signal(
                    bar=bar,
                    macro_bars=macro_bars,
                    setup_bars=setup_bars,
                    trader_style=style,
                    learned_genome=bp.get("learned_strategy_genome"),
                    sentinel_signal=s_val,
                    pulse_signal=p_val,
                )
                if sig == 0:
                    i += 1
                    continue

                # 3. Check if Oracle V1 was able to trade here
                d1_bar = self._find_latest_completed_bar(dataset["series"].get("D1", []), unix_sec)
                v1_status = self.evaluate_v1_legacy_signal(d1_bar)
                v1_placed_demo = self.did_oracle_v1_trade_near(v1_timestamps, unix_sec)
                v1_missed_opportunity = (
                    (not v1_placed_demo)
                    or v1_status.startswith("V1_BLOCKED")
                    or v1_status.startswith("V1_IDLE")
                )
                if not v1_missed_opportunity:
                    i += 1
                    continue

                is_long = sig == 1
                entry_price = float(bar["close"])
                atr = max(float(bar.get("atr14", 0.0005)), entry_price * 1e-4)
                mt5_specs = self.mt5_bridge.get_mt5_symbol_specs(upper_sym, entry_price)
                min_broker_stop = float(mt5_specs["point"]) * max(float(mt5_specs["stops_level_points"]), 10.0)
                struct_dist = (
                    max(entry_price - float(bar["swing_low_16"]), atr * sl_atr_mult)
                    if is_long
                    else max(float(bar["swing_high_16"]) - entry_price, atr * sl_atr_mult)
                )
                stop_distance = max(struct_dist, atr * sl_atr_mult, min_broker_stop)
                stop_distance_bps = (stop_distance / entry_price) * 10000.0
                if total_cost_bps > stop_distance_bps * 0.38:
                    i += 1
                    continue

                initial_stop = round(
                    entry_price - stop_distance if is_long else entry_price + stop_distance, 5
                )

                # 4. Execute & Monitor the Shadow Trade with Necessity-Gated Re-Analysis & Two-Tier Penalties
                sim = self.mtf_engine.simulate_real_trader_dynamic_execution(
                    trig_series=trigger_series,
                    dataset=dataset,
                    stack=stack,
                    entry_idx=i,
                    horizon_bars=horizon_bars,
                    is_long=is_long,
                    entry_price=entry_price,
                    stop_distance=stop_distance,
                    atr=atr,
                    learned_genome=bp.get("learned_strategy_genome") or {},
                    management_policy=management_policy,
                    sentinel_scores=sentinel_scores,
                    pulse_scores=pulse_scores,
                )
                gross_r = float(sim["gross_r"])
                exit_idx = int(sim["exit_idx"])
                exit_price = float(sim["exit_price"])
                current_stop = float(sim["active_stop_loss"])
                shadow_status = str(sim["shadow_status"])
                peak_mfe_r = float(sim["peak_mfe_r"])
                worst_mae_r = float(sim["worst_mae_r"])
                be_active = bool(sim["be_ratchet_triggered"])
                tp1_filled = bool(sim["tp1_partial_banked"])
                early_cut = bool(sim["early_loss_cut_triggered"])
                near_tp_save = bool(sim["near_tp_reversal_exit_triggered"])
                tp_expansions = int(sim["tp_expansions_count"])
                sl_fits = int(sim["progressive_sl_fits_count"])
                tp_history_r = list(sim["tp_expansion_history_r"])
                highest_stage = int(sim["highest_tp_stage_reached"])
                continued_past_tp = bool(sim["continued_past_primary_tp"])
                lifecycle_events = list(sim["lifecycle_events"])

                cost_drag_r = total_cost_bps / max(stop_distance_bps, 0.1)
                realized_r = round(gross_r - cost_drag_r, 4)
                net_return_pct = round(realized_r * 0.5, 4)

                # Grade net realized_r with Two-Tier Penalty + Post-Exit Counterfactual Honesty
                penalty_audit = LearnedPenaltyAndNecessityEngine.grade_closed_trade_with_penalties(
                    realized_r=realized_r,
                    initial_major_tp_r=float(sim["initial_major_tp_r"]),
                    final_major_tp_r=float(sim["final_major_tp_r"]),
                    shadow_status=shadow_status,
                    policy=management_policy,
                    post_exit_hit_major_tp_without_sl=bool(sim.get("post_exit_hit_major_tp_without_sl")),
                    post_exit_hit_initial_sl=bool(sim.get("post_exit_hit_initial_sl")),
                )

                micro_trigger = (
                    "Bullish Liquidity Sweep"
                    if bar.get("liquidity_sweep_bullish")
                    else "Bearish Liquidity Sweep"
                    if bar.get("liquidity_sweep_bearish")
                    else "Bullish 3-Bar FVG"
                    if bar.get("fvg_bullish")
                    else "Bearish 3-Bar FVG"
                    if bar.get("fvg_bearish")
                    else "Break of Structure (BOS)"
                    if bar.get("bos_direction", 0) != 0
                    else "Sentinel News Catalyst"
                    if abs(s_val) >= 0.35
                    else "Pulse Crowd Flow"
                    if abs(p_val) >= 0.35
                    else "Institutional Pullback / Edge Reversion"
                )
                trigger_feature_key = (
                    "liquidity_sweep"
                    if (bar.get("liquidity_sweep_bullish") or bar.get("liquidity_sweep_bearish"))
                    else "fvg_impulse"
                    if (bar.get("fvg_bullish") or bar.get("fvg_bearish"))
                    else "bos_breakout"
                    if bar.get("bos_direction", 0) != 0
                    else "sentinel_news_sentiment"
                    if abs(s_val) >= 0.35
                    else "pulse_social_sentiment"
                    if abs(p_val) >= 0.35
                    else "dealing_range_reversion"
                    if bar.get("discount_premium_zone") in ("DISCOUNT", "PREMIUM")
                    else "ema_trend"
                )

                # Refresh account equity before sizing this trade (live MT5 equity or compounding shadow equity)
                trade_entry_equity_usd = self.mt5_bridge.get_account_equity_usd()
                sizing_info = PositionSizingAndSafetyEngine.compute_position_size_for_stop(
                    symbol=upper_sym,
                    entry_price=entry_price,
                    stop_distance=stop_distance,
                    account_equity_usd=trade_entry_equity_usd,
                    risk_budget_pct=0.50,
                    mt5_symbol_specs=mt5_specs,
                )
                trade_thesis = ExplainabilityAndNoTradeEngine.build_structured_trade_thesis(
                    symbol=upper_sym,
                    trader_style=style,
                    direction="LONG" if is_long else "SHORT",
                    entry_bar=bar,
                    macro_bars=macro_bars,
                    setup_bars=setup_bars,
                    entry_price=entry_price,
                    stop_loss_price=initial_stop,
                    initial_major_tp_price=float(sim["active_major_tp_price"]),
                    partial_tp_price=float(sim["partial_tp_price"]),
                    management_policy=management_policy,
                    sizing_info=sizing_info,
                    cost_report=bp.get("cost_and_data_integrity_report") or {
                        "base_roundtrip_cost_bps": total_cost_bps,
                        "stress_roundtrip_cost_bps": round(total_cost_bps * 1.65, 4),
                    },
                    sentinel_score=s_val,
                    pulse_score=p_val,
                )

                # Evaluate through Oracle's TradeManager (wired with learned policy) & log to MT5 Demo Logger
                trade_cycle_counter += 1
                mt5_tm_eval = self.mt5_bridge.evaluate_with_oracle_trade_manager(
                    symbol=upper_sym,
                    is_long=is_long,
                    entry_price=entry_price,
                    initial_stop=initial_stop,
                    target_price=float(sim["active_major_tp_price"]),
                    entry_regime=str(bar.get("regime", "CONSOLIDATION_RANGING")),
                    entry_atr=atr,
                    exit_bar=trigger_series[exit_idx],
                    management_policy=management_policy,
                    sentinel_signal=s_val,
                    pulse_signal=p_val,
                )
                mt5_exec_record = self.mt5_bridge.execute_or_log_mt5_demo_trade(
                    cycle=trade_cycle_counter,
                    symbol=upper_sym,
                    direction="LONG" if is_long else "SHORT",
                    confidence=0.68,
                    regime=str(bar.get("regime", "CONSOLIDATION_RANGING")),
                    sizing_info=sizing_info,
                    entry_price=entry_price,
                    stop_price=initial_stop,
                    target_price=float(sim["active_major_tp_price"]),
                    reward_risk=float(sim["initial_major_tp_r"]),
                    shadow_status=shadow_status,
                    realized_r=realized_r,
                    penalty_audit=penalty_audit,
                    current_signal=current_signal,
                    latest_signal=latest_signal,
                    signal_id=f"{upper_sym}:{unix_sec}",
                    equity_override_usd=trade_entry_equity_usd,
                )

                # Update tracked equity after closed trade so subsequent trades reflect gains/losses
                if not shadow_status.startswith("OPEN"):
                    account_equity_usd = self.mt5_bridge.update_tracked_equity_after_trade(net_return_pct)

                trade_record = {
                    "shadow_trade_id": f"shd-{upper_sym.lower()}-{style.lower()}-{unix_sec}",
                    "symbol": upper_sym,
                    "broker_symbol": mt5_specs["broker_symbol"],
                    "trader_style": style,
                    "candidate_id": loaded["candidate_id"],
                    "lifecycle_state": loaded.get("lifecycle_state", "CERTIFIED"),
                    "trigger_timeframe": trigger_tf,
                    "trigger_feature_key": trigger_feature_key,
                    "sentinel_signal_at_entry": round(s_val, 3),
                    "pulse_signal_at_entry": round(p_val, 3),
                    "direction": "LONG" if is_long else "SHORT",
                    "opened_at_ts": bar["ts"],
                    "closed_at_ts": trigger_series[exit_idx]["ts"],
                    "session": bar.get("session"),
                    "adaptive_session": bar.get("adaptive_session"),
                    "regime": bar.get("regime"),
                    "oracle_v1_status": v1_status,
                    "why_v2_shadow_entered": f"Oracle V1 was idle ({v1_status}) while V2 {style} detected {micro_trigger} in {bar.get('discount_premium_zone')} zone during {bar.get('adaptive_session')}.",
                    "entry_price": round(entry_price, 5),
                    "initial_stop_loss": initial_stop,
                    "active_stop_loss": round(current_stop, 5),
                    "tp1_level": sim["partial_tp_price"],
                    "tp2_level": sim["active_major_tp_price"],
                    "primary_tp_level": sim["active_major_tp_price"],
                    "initial_major_tp_r": sim["initial_major_tp_r"],
                    "final_major_tp_r": sim["final_major_tp_r"],
                    "tp_expansion_history_r": tp_history_r,
                    "tp_expansions_count": tp_expansions,
                    "early_loss_cut_triggered": early_cut,
                    "near_tp_reversal_exit_triggered": near_tp_save,
                    "progressive_sl_fits_count": sl_fits,
                    "highest_tp_stage_reached": highest_stage,
                    "continued_past_primary_tp": continued_past_tp,
                    "active_necessity_triggers": sim.get("active_necessity_triggers", []),
                    "exit_price": round(exit_price, 5),
                    "shadow_status": shadow_status,
                    "profitable": bool(penalty_audit["is_true_tp_win"]),
                    "is_true_tp_win": bool(penalty_audit["is_true_tp_win"]),
                    "is_positive_scratch_only": bool(penalty_audit["is_positive_scratch_only"]),
                    "realized_r": realized_r,
                    "penalty_adjusted_r": penalty_audit["penalty_adjusted_r"],
                    "penalty_audit": penalty_audit,
                    "net_return_pct": net_return_pct,
                    "account_equity_before_trade_usd": round(trade_entry_equity_usd, 2),
                    "account_equity_after_trade_usd": round(account_equity_usd, 2),
                    "peak_mfe_r": round(peak_mfe_r, 3),
                    "worst_mae_r": round(worst_mae_r, 3),
                    "be_ratchet_triggered": be_active,
                    "tp1_partial_banked": tp1_filled,
                    "position_sizing": sizing_info,
                    "mt5_demo_execution": mt5_exec_record,
                    "oracle_mt5_trade_manager": mt5_tm_eval,
                    "trade_thesis": trade_thesis,
                    "lifecycle_events": lifecycle_events,
                }
                style_trades.append(trade_record)
                shadow_trades.append(trade_record)

                # Emit Mistake Clinic feedback for BOTH Major SL Penalties AND Minor Early-Exit/Breakeven Penalties!
                if penalty_audit["penalty_tier"] != "NONE":
                    feedback_event = {
                        "event_id": f"shd-fb-{unix_sec}-{upper_sym}-{style}",
                        "event_type": (
                            "forge.shadow_runner.major_sl_penalty"
                            if penalty_audit["penalty_tier"] == "MAJOR_PENALTY"
                            else "forge.shadow_runner.unnecessary_early_stop_penalty"
                            if penalty_audit["counterfactual_audit"] == "UNNECESSARY_EARLY_STOP_MISSED_MAIN_TP"
                            else "forge.shadow_runner.minor_early_exit_penalty"
                        ),
                        "symbol": upper_sym,
                        "trader_style": style,
                        "trigger_feature_key": trigger_feature_key,
                        "regime": bar.get("regime"),
                        "session": bar.get("session"),
                        "adaptive_session": bar.get("adaptive_session"),
                        "realized_r": realized_r,
                        "applied_penalty_r": penalty_audit["applied_penalty_r"],
                        "penalty_adjusted_r": penalty_audit["penalty_adjusted_r"],
                        "penalty_tier": penalty_audit["penalty_tier"],
                        "counterfactual_audit": penalty_audit["counterfactual_audit"],
                        "peak_mfe_r": round(peak_mfe_r, 3),
                        "worst_mae_r": round(worst_mae_r, 3),
                        "early_loss_cut_triggered": early_cut,
                        "exit_reason": shadow_status,
                        "failure_reason": (
                            f"Shadow {style} trade ({realized_r:+.3f}R, {shadow_status}) incurred "
                            f"{penalty_audit['penalty_tier']} (-{penalty_audit['applied_penalty_r']:.3f}R -> "
                            f"adj={penalty_audit['penalty_adjusted_r']:+.3f}R, counterfactual={penalty_audit['counterfactual_audit']})."
                        ),
                        "timestamp_unix": time.time(),
                    }
                    with self.feedback_outbox_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(feedback_event) + "\n")

                i = max(i + 1, exit_idx + 1)

            true_wins = sum(1 for t in style_trades if t["is_true_tp_win"])
            scratches = sum(1 for t in style_trades if t["is_positive_scratch_only"])
            losses = sum(1 for t in style_trades if t["realized_r"] < 0)
            total_r = round(sum(t["realized_r"] for t in style_trades), 3)
            pen_adj_total_r = round(sum(t["penalty_adjusted_r"] for t in style_trades), 3)
            avg_r = round(total_r / max(1, len(style_trades)), 3)
            major_pen_cnt = sum(
                1 for t in style_trades if t["penalty_audit"]["penalty_tier"] == "MAJOR_PENALTY"
            )
            minor_pen_cnt = sum(
                1 for t in style_trades if "MINOR" in t["penalty_audit"]["penalty_tier"]
            )
            unnecessary_stops_cnt = sum(
                1
                for t in style_trades
                if t["penalty_audit"]["counterfactual_audit"] == "UNNECESSARY_EARLY_STOP_MISSED_MAIN_TP"
            )
            necessary_saves_cnt = sum(
                1
                for t in style_trades
                if t["penalty_audit"]["counterfactual_audit"] == "NECESSARY_CAPITAL_SAVE_AVOIDED_FULL_SL"
            )
            early_cuts_cnt = sum(1 for t in style_trades if t.get("early_loss_cut_triggered"))
            near_tp_saves_cnt = sum(1 for t in style_trades if t.get("near_tp_reversal_exit_triggered"))
            tp_expansions_cnt = sum(int(t.get("tp_expansions_count", 0)) for t in style_trades)

            style_summaries[style] = {
                "trader_style": style,
                "candidate_id": loaded["candidate_id"],
                "trigger_timeframe": trigger_tf,
                "shadow_trades_count": len(style_trades),
                "wins": true_wins,
                "true_tp_wins": true_wins,
                "breakeven_and_early_scratches": scratches,
                "losses": losses,
                "win_rate_pct": round((true_wins / max(1, len(style_trades))) * 100.0, 1),
                "total_realized_r": total_r,
                "penalty_adjusted_total_r": pen_adj_total_r,
                "expectancy_r": avg_r,
                "net_shadow_return_pct": round(total_r * 0.5, 2),
                "major_sl_penalties_count": major_pen_cnt,
                "minor_early_exit_penalties_count": minor_pen_cnt,
                "unnecessary_early_stops_count": unnecessary_stops_cnt,
                "necessary_capital_saves_count": necessary_saves_cnt,
                "early_loss_cuts_before_sl": early_cuts_cnt,
                "near_tp_reversal_saves": near_tp_saves_cnt,
                "major_tp_expansions_count": tp_expansions_cnt,
                "verdict": (
                    "PROFITABLE_AND_HONEST_IN_SHADOW"
                    if pen_adj_total_r > 0 and true_wins > 0
                    else "NEEDS_MISTAKE_CLINIC_REFINEMENT"
                ),
            }

            self.chronicle_ledger.record_lesson(
                domain="shadow_runner_evaluation",
                content=(
                    f"Shadow & MT5 Demo Runner evaluated {upper_sym} {style} ({trigger_tf}) under Two-Tier Penalty Standard: "
                    f"{len(style_trades)} trades, TrueTPWins={true_wins} ({style_summaries[style]['win_rate_pct']}%), "
                    f"Scratches={scratches}, Losses={losses}, Net={total_r}R, PenaltyAdj={pen_adj_total_r}R "
                    f"(MajorPenalties={major_pen_cnt}, MinorPenalties={minor_pen_cnt}, UnnecessaryStops={unnecessary_stops_cnt})."
                ),
                concept_key=f"shadow_runner::{upper_sym}::{style}",
                tags=["forge", "shadow_runner", "mt5_demo", upper_sym, style],
                source_repository="forge",
            )

        shadow_trades.sort(key=lambda x: x["opened_at_ts"], reverse=True)
        total_true_wins = sum(1 for t in shadow_trades if t["is_true_tp_win"])
        total_scratches = sum(1 for t in shadow_trades if t["is_positive_scratch_only"])
        total_net_r = round(sum(t["realized_r"] for t in shadow_trades), 3)
        total_pen_adj_r = round(sum(t["penalty_adjusted_r"] for t in shadow_trades), 3)

        m5_series = dataset.get("series", {}).get("M5", [])
        m15_series = dataset.get("series", {}).get("M15", [])
        h1_series = dataset.get("series", {}).get("H1", [])
        h4_series = dataset.get("series", {}).get("H4", [])
        d1_series = dataset.get("series", {}).get("D1", [])
        w1_series = dataset.get("series", {}).get("W1", [])
        latest_live_bar = m5_series[-1] if m5_series else (m15_series[-1] if m15_series else {})
        htf_target_bars = [b for b in (h4_series[-1:] + d1_series[-1:] + w1_series[-1:]) if b]

        # 5. Live Lifecycle Management of Open MT5 Positions (modify_position / close_position)
        open_mt5_management = self.mt5_bridge.manage_open_mt5_positions(
            symbol=upper_sym,
            latest_bar=latest_live_bar,
            macro_bars=[b for b in (d1_series[-1:] + h4_series[-1:]) if b],
            setup_bars=[b for b in (h1_series[-1:] + m15_series[-1:]) if b],
            htf_bars=htf_target_bars,
            learned_genome=latest_learned_genome,
            management_policy=latest_management_policy,
            sentinel_signal=0.0,
            pulse_signal=0.0,
            mtf_engine=self.mtf_engine,
        )

        # 6. Reconcile Real Closed MT5 Deals (`mt5.history_deals_get`) & Feed Real Losses to Mistake Clinic
        mt5_deal_reconciliation = self.mt5_bridge.reconcile_mt5_closed_deals(
            symbol=upper_sym,
            management_policy=latest_management_policy,
            feedback_outbox_path=self.feedback_outbox_path,
            lookback_days=7,
        )

        final_equity_usd = self.mt5_bridge.get_account_equity_usd()
        report = {
            "schema": "forge.shadow_runner.report.v2",
            "mode": (
                "MT5_DEMO_LIVE_EXECUTION_AND_SHADOW"
                if self.execute_mt5_demo_orders
                else "MT5_PAPER_BRIDGE_AND_SHADOW_VERIFICATION"
            ),
            "symbol": upper_sym,
            "broker_symbol": self.mt5_bridge.resolve_broker_symbol(upper_sym),
            "evaluated_at_unix": time.time(),
            "mt5_demo_bridge_status": {
                **self.mt5_bridge.connection_status,
                "starting_session_equity_usd": round(starting_equity_usd, 2),
                "account_equity_usd": round(final_equity_usd, 2),
                "session_equity_change_usd": round(final_equity_usd - starting_equity_usd, 2),
                "broker_symbol": self.mt5_bridge.resolve_broker_symbol(upper_sym),
                "mt5_symbol_specs": self.mt5_bridge.get_mt5_symbol_specs(
                    upper_sym,
                    float(m15_series[-1]["close"]) if m15_series else 1.0,
                ),
                "mt5_csv_logs": self.mt5_bridge.mt5_logger.paths() if self.mt5_bridge.mt5_logger else {},
            },
            "mt5_open_position_management": open_mt5_management,
            "mt5_live_account_reconciliation": mt5_deal_reconciliation,
            "live_data_feed": {
                "source": dataset.get("source", "yahoo_finance_live_7tf_institutional"),
                "fetched_at_unix": dataset.get("fetched_at_unix"),
                "latest_m5_bar_ts": m5_series[-1]["ts"] if m5_series else None,
                "latest_m15_bar_ts": m15_series[-1]["ts"] if m15_series else None,
                "latest_live_price": m5_series[-1]["close"] if m5_series else (m15_series[-1]["close"] if m15_series else None),
                "live_spread_bps": dataset.get("live_spread_bps"),
            },
            "oracle_v1_api_bridge_monitor": v1_api_bridge,
            "oracle_v1_demo_trades_in_history": len(v1_timestamps),
            "v1_missed_opportunities_captured_by_v2": len(shadow_trades),
            "overall_shadow_win_rate_pct": round(
                (total_true_wins / max(1, len(shadow_trades))) * 100.0, 1
            ),
            "overall_true_tp_wins_count": total_true_wins,
            "overall_breakeven_scratches_count": total_scratches,
            "overall_shadow_total_r": total_net_r,
            "overall_penalty_adjusted_total_r": total_pen_adj_r,
            "overall_shadow_net_return_pct": round(total_net_r * 0.5, 2),
            "style_summaries": style_summaries,
            "recent_shadow_trades": shadow_trades[:40],
        }

        existing_ledger = self._read_json(self.shadow_ledger_path, {"symbols": {}})
        if not isinstance(existing_ledger, dict):
            existing_ledger = {"symbols": {}}
        if "symbols" not in existing_ledger:
            existing_ledger["symbols"] = {}
        existing_ledger["symbols"][upper_sym] = report
        existing_ledger["updated_at_unix"] = time.time()
        self.shadow_ledger_path.write_text(
            json.dumps(existing_ledger, indent=2), encoding="utf-8"
        )

        # Closed-Loop Reinforcement: feed the penalty-adjusted outcomes (including real MT5 deal losses) back into Forge
        if shadow_trades or mt5_deal_reconciliation.get("closed_deals_count", 0) > 0:
            try:
                self.mtf_engine.train_and_certify_symbol_strategies(
                    upper_sym, styles=styles, force_refresh_data=False
                )
            except Exception:
                pass

        return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forge V2 Shadow & MT5 Demo Runner (Learned Two-Tier Penalty & Necessity Standard)"
    )
    parser.add_argument(
        "--symbol", default="GBPUSD", help="Single symbol (e.g. GBPUSD, EURUSD)"
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols (e.g. GBPUSD,EURUSD,USDJPY,XAUUSD) or ALL",
    )
    parser.add_argument(
        "--preset",
        choices=["single", "live", "all"],
        default="single",
        help="Symbol preset: 'live' (10 core multi-asset symbols) or 'all' (full 38-symbol watchlist)",
    )
    parser.add_argument(
        "--style",
        default="ALL",
        help="Trader style: ALL, SCALPING, DAY_TRADING, SWING_TRADING, RANGE_SPECIALIST",
    )
    parser.add_argument(
        "--lookback-bars",
        type=int,
        default=260,
        help="Number of recent trigger-timeframe bars to monitor",
    )
    parser.add_argument(
        "--mt5-demo",
        action="store_true",
        help="Submit fresh current signals to a connected MT5 demo account; historical replay is never executed",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in a background loop alongside Oracle",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=300,
        help="Seconds between shadow evaluation cycles when --daemon is active (default: 300s / 5m)",
    )
    args = parser.parse_args()

    if args.preset == "all" or args.symbols.strip().upper() == "ALL":
        symbols_list = list(ALL_WATCHLIST_SYMBOLS)
    elif args.preset == "live":
        symbols_list = list(LIVE_PRESET_SYMBOLS)
    elif args.symbols.strip():
        symbols_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols_list = [args.symbol.upper()]

    runner = ForgeShadowRunner(execute_mt5_demo_orders=args.mt5_demo)
    while True:
        for sym in symbols_list:
            report = runner.run_shadow_session(
                symbol=sym,
                trader_style=args.style,
                lookback_bars=args.lookback_bars,
            )
            print(
                json.dumps(
                    {
                        "status": "complete",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "mode": report.get("mode"),
                        "symbol": report.get("symbol"),
                        "broker_symbol": report.get("broker_symbol"),
                        "live_data_feed_source": (report.get("live_data_feed") or {}).get("source"),
                        "mt5_demo_bridge_status": report.get("mt5_demo_bridge_status"),
                        "mt5_open_position_management": report.get("mt5_open_position_management"),
                        "mt5_live_account_reconciliation": report.get("mt5_live_account_reconciliation"),
                        "historical_shadow_replay_summary": {
                            "v1_missed_opportunities_captured_by_v2": report.get(
                                "v1_missed_opportunities_captured_by_v2"
                            ),
                            "overall_true_tp_win_rate_pct": report.get(
                                "overall_shadow_win_rate_pct"
                            ),
                            "overall_true_tp_wins_count": report.get("overall_true_tp_wins_count"),
                            "overall_breakeven_scratches_count": report.get("overall_breakeven_scratches_count"),
                            "overall_shadow_total_r": report.get("overall_shadow_total_r"),
                            "overall_penalty_adjusted_total_r": report.get("overall_penalty_adjusted_total_r"),
                        },
                        "style_summaries": report.get("style_summaries"),
                        "sample_shadow_trade": (
                            report.get("recent_shadow_trades") or [None]
                        )[0],
                    },
                    indent=2,
                ),
                flush=True,
            )
        if not args.daemon:
            break
        time.sleep(max(15, args.interval_sec))


if __name__ == "__main__":
    main()

