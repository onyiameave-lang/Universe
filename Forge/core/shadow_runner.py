"""
Forge.core.shadow_runner
========================
Native Python 3.10 Shadow Runner for Forge V2 × Oracle V2 Strategies.

Purpose:
  While Oracle V1 runs on the cTrader Demo account, there are many intraday
  sessions and setups where Oracle V1 is unable to place a trade (e.g. V1 is
  waiting on a daily D1 signal or static regime filter).
  
  The Shadow Runner runs Forge's newly certified 7-timeframe strategies
  (SCALPING, DAY_TRADING, SWING_TRADING, RANGE_SPECIALIST) in the background
  ("Shadow Mode" — NEVER placing orders on the cTrader demo/live account).

  1. Detects setups where Oracle V1 did NOT place a trade ("V1_NO_TRADE_OPPORTUNITY")
     or where V1 and V2 diverge.
  2. Opens a background Shadow Trade with the strategy's learned Stop-Loss,
     Break-Even ratchet, 50% TP1 scale-out, and 50% Chandelier-trailed TP2.
  3. Monitors the Shadow Trade bar-by-bar across real intraday candles (including
     verified cTrader spread, slippage, and commission).
  4. Records whether the shadow trade succeeded or failed in:
     - `Forge/memory/shadow_trades_ledger.json`
     - `Oracle/memory/forge_strategy_feedback_outbox.jsonl`
     - `Chronicle` (using non-duplicating "Happened again on <date>" consolidation)
     so Forge continuously learns from shadow execution before any strategy is
     promoted to live trading.

CLI Usage (from repository root):
  python3 -m Forge.core.shadow_runner --symbol GBPUSD --style ALL
"""
from __future__ import annotations

import argparse
import bisect
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from Chronicle.core.dedup_ledger import ChronicleDeduplicatingLedger
from Forge.core.mtf_institutional_engine import MultiTimeframeInstitutionalEngine


class ForgeShadowRunner:
    """
    Background Shadow Mode execution & monitoring engine for Forge V2 strategies.
    Never touches cTrader demo/live orders; evaluates and monitors trades in shadow mode.
    """

    def __init__(self, workspace_root: Optional[Path] = None) -> None:
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self.mtf_engine = MultiTimeframeInstitutionalEngine(self.workspace_root)
        self.chronicle_ledger = ChronicleDeduplicatingLedger(self.workspace_root)

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
        """
        Reads the live V1 -> V2 API bridge telemetry stored in Chronicle via
        `POST /internal/forge/act` (`oracle.decision.observation` & `oracle.trader.heartbeat`).
        Whenever V1 fails to place a trade (e.g. `no_setup`, `risk_rejected`, `signal_timeout`),
        V1 posts that observation to the API, which persists `forge.oracle.decision_summary.*`.
        """
        upper = symbol.upper()
        kv_path = (
            self.workspace_root / "Chronicle" / "memory" / "store" / "kv_state.json"
        )
        kv_store = self._read_json(kv_path, {})
        summaries: List[Dict[str, Any]] = []
        latest_heartbeat: Optional[Dict[str, Any]] = None

        if isinstance(kv_store, dict):
            for k, entry in kv_store.items():
                val = entry.get("value") if isinstance(entry, dict) else None
                if not isinstance(val, dict):
                    continue
                if k.startswith("forge.oracle.decision_summary.") and str(val.get("symbol", "")).upper() == upper:
                    summaries.append(val)
                elif k.startswith("forge.oracle.trader_heartbeat.latest."):
                    if (
                        latest_heartbeat is None
                        or float(val.get("last_seen_at_unix", 0) or 0)
                        > float(latest_heartbeat.get("last_seen_at_unix", 0) or 0)
                    ):
                        latest_heartbeat = val

        summaries.sort(
            key=lambda s: float(s.get("last_event_at_unix", 0) or 0), reverse=True
        )
        top_summary = summaries[0] if summaries else {}
        recent_events = top_summary.get("recent_events") or []
        latest_event = recent_events[-1] if recent_events else {}

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
        Runs the Shadow Runner across the live 7-timeframe market feed for `symbol`,
        placing background shadow trades whenever Oracle V1 is idle/blocked (monitored via
        `POST /internal/forge/act` decision observations + D1 signal state) and
        monitoring each shadow trade to completion (or active open status).
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

        shadow_trades: List[Dict[str, Any]] = []
        style_summaries: Dict[str, Any] = {}

        for style in styles:
            loaded = self.load_v2_candidate_blueprint(upper_sym, style)
            if not loaded:
                continue
            bp = loaded["blueprint"]
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
            sl_atr_mult = float(exc.get("learned_optimal_stop_atr_mult", 1.5))
            tp_atr_mult = float(exc.get("learned_optimal_tp_atr_mult", 3.0))
            rr_target = float(exc.get("learned_risk_reward_ratio", 2.0))
            be_trigger_r = float(exc.get("learned_be_trigger_r", 0.75))
            tp1_r = float(exc.get("learned_tp1_r", 1.25))
            tp2_r = float(exc.get("learned_tp2_r", 2.4))
            trail_atr_mult = float(exc.get("learned_trail_atr_mult", 1.35))

            horizon_bars = (
                12 if style == "SCALPING" else (16 if style in ("DAY_TRADING", "RANGE_SPECIALIST") else 24)
            )

            start_idx = max(30, len(trigger_series) - lookback_bars)
            end_idx = len(trigger_series)

            style_trades: List[Dict[str, Any]] = []
            i = start_idx

            while i < end_idx - 1:
                bar = trigger_series[i]
                unix_sec = int(bar["unix_sec"])

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

                # 2. Check Top-Down 7-Timeframe Context
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

                sig = self.mtf_engine.evaluate_trader_archetype_signal(
                    bar=bar,
                    macro_bars=macro_bars,
                    setup_bars=setup_bars,
                    trader_style=style,
                )
                if sig == 0:
                    i += 1
                    continue

                # 3. Check if Oracle V1 was able to trade here
                d1_bar = self._find_latest_completed_bar(dataset["series"].get("D1", []), unix_sec)
                v1_status = self.evaluate_v1_legacy_signal(d1_bar)
                v1_placed_demo = self.did_oracle_v1_trade_near(v1_timestamps, unix_sec)

                # Shadow Runner specifically captures trades when V1 did NOT place a demo trade
                # or when V1 was blocked by its daily-only filter
                v1_missed_opportunity = (not v1_placed_demo) or v1_status.startswith("V1_BLOCKED") or v1_status.startswith("V1_IDLE")
                if not v1_missed_opportunity:
                    i += 1
                    continue

                is_long = sig == 1
                entry_price = float(bar["close"])
                atr = max(float(bar.get("atr14", 0.0005)), entry_price * 0.00015)

                struct_dist = (
                    max(entry_price - float(bar["swing_low_16"]), atr * 0.75)
                    if is_long
                    else max(float(bar["swing_high_16"]) - entry_price, atr * 0.75)
                )
                stop_distance = min(
                    max(struct_dist, atr * (sl_atr_mult * 0.85)),
                    atr * (sl_atr_mult * 1.35),
                )
                target_distance = max(atr * tp_atr_mult, stop_distance * rr_target)

                stop_distance_bps = (stop_distance / entry_price) * 10000.0
                if total_cost_bps > stop_distance_bps * 0.38:
                    i += 1
                    continue

                initial_stop = round(
                    entry_price - stop_distance if is_long else entry_price + stop_distance, 5
                )
                tp1_price = round(
                    entry_price + stop_distance * tp1_r
                    if is_long
                    else entry_price - stop_distance * tp1_r,
                    5,
                )
                tp2_price = round(
                    entry_price + stop_distance * tp2_r
                    if is_long
                    else entry_price - stop_distance * tp2_r,
                    5,
                )

                # 4. Monitor the Shadow Trade bar-by-bar in the background
                current_stop = initial_stop
                be_active = False
                tp1_filled = False
                banked_r = 0.0
                remaining_weight = 1.0
                peak_mfe_r = 0.0
                worst_mae_r = 0.0
                exit_idx = min(end_idx - 1, i + horizon_bars)
                exit_price = float(trigger_series[exit_idx]["close"])
                shadow_status = "CLOSED_TIME_HORIZON"
                lifecycle_events: List[str] = []

                for k in range(i + 1, min(end_idx, i + horizon_bars + 1)):
                    f_bar = trigger_series[k]
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

                    if is_long:
                        if f_low <= current_stop:
                            exit_idx = k
                            exit_price = current_stop
                            stop_exit_r = (current_stop - entry_price) / stop_distance
                            gross_r = banked_r + remaining_weight * stop_exit_r
                            remaining_weight = 0.0
                            shadow_status = (
                                "CLOSED_TP1_AND_TRAILED_STOP"
                                if tp1_filled
                                else ("CLOSED_BREAKEVEN_PROTECTED" if be_active else "CLOSED_STOP_LOSS")
                            )
                            lifecycle_events.append(
                                f"Exited at {round(current_stop, 5)} ({shadow_status}) on {f_bar['ts']}"
                            )
                            break
                        if not tp1_filled and fav_r >= tp1_r:
                            banked_r += 0.5 * tp1_r
                            remaining_weight = 0.5
                            tp1_filled = True
                            be_active = True
                            current_stop = max(current_stop, entry_price + stop_distance * 0.12)
                            lifecycle_events.append(
                                f"Banked 50% TP1 at +{tp1_r}R ({tp1_price}) & locked SL at +{round(0.12, 2)}R"
                            )
                        elif not be_active and fav_r >= be_trigger_r:
                            be_active = True
                            current_stop = max(current_stop, entry_price + stop_distance * 0.10)
                            lifecycle_events.append(
                                f"Activated Dynamic Break-Even at +{be_trigger_r}R (SL moved to {round(current_stop, 5)})"
                            )
                        if tp1_filled:
                            chand_stop = f_close - atr * trail_atr_mult
                            if chand_stop > current_stop:
                                current_stop = chand_stop
                        if fav_r >= tp2_r:
                            exit_idx = k
                            exit_price = tp2_price
                            gross_r = banked_r + remaining_weight * tp2_r
                            remaining_weight = 0.0
                            shadow_status = "CLOSED_FULL_TP2_TARGET"
                            lifecycle_events.append(
                                f"Reached full Runner TP2 at +{tp2_r}R ({tp2_price}) on {f_bar['ts']}"
                            )
                            break
                    else:
                        if f_high >= current_stop:
                            exit_idx = k
                            exit_price = current_stop
                            stop_exit_r = (entry_price - current_stop) / stop_distance
                            gross_r = banked_r + remaining_weight * stop_exit_r
                            remaining_weight = 0.0
                            shadow_status = (
                                "CLOSED_TP1_AND_TRAILED_STOP"
                                if tp1_filled
                                else ("CLOSED_BREAKEVEN_PROTECTED" if be_active else "CLOSED_STOP_LOSS")
                            )
                            lifecycle_events.append(
                                f"Exited at {round(current_stop, 5)} ({shadow_status}) on {f_bar['ts']}"
                            )
                            break
                        if not tp1_filled and fav_r >= tp1_r:
                            banked_r += 0.5 * tp1_r
                            remaining_weight = 0.5
                            tp1_filled = True
                            be_active = True
                            current_stop = min(current_stop, entry_price - stop_distance * 0.12)
                            lifecycle_events.append(
                                f"Banked 50% TP1 at +{tp1_r}R ({tp1_price}) & locked SL at +{round(0.12, 2)}R"
                            )
                        elif not be_active and fav_r >= be_trigger_r:
                            be_active = True
                            current_stop = min(current_stop, entry_price - stop_distance * 0.10)
                            lifecycle_events.append(
                                f"Activated Dynamic Break-Even at +{be_trigger_r}R (SL moved to {round(current_stop, 5)})"
                            )
                        if tp1_filled:
                            chand_stop = f_close + atr * trail_atr_mult
                            if chand_stop < current_stop:
                                current_stop = chand_stop
                        if fav_r >= tp2_r:
                            exit_idx = k
                            exit_price = tp2_price
                            gross_r = banked_r + remaining_weight * tp2_r
                            remaining_weight = 0.0
                            shadow_status = "CLOSED_FULL_TP2_TARGET"
                            lifecycle_events.append(
                                f"Reached full Runner TP2 at +{tp2_r}R ({tp2_price}) on {f_bar['ts']}"
                            )
                            break

                if remaining_weight > 0.0:
                    # Check if the trade is still actively open at the very end of the series
                    if i + horizon_bars >= end_idx - 1:
                        shadow_status = "OPEN_MONITORING"
                    final_move = (
                        exit_price - entry_price if is_long else entry_price - exit_price
                    )
                    gross_r = banked_r + remaining_weight * (final_move / stop_distance)
                    lifecycle_events.append(
                        f"{'Currently Open & Monitoring' if shadow_status == 'OPEN_MONITORING' else 'Closed at time horizon'} at {round(exit_price, 5)}"
                    )

                cost_drag_r = total_cost_bps / max(stop_distance_bps, 0.1)
                realized_r = round(gross_r - cost_drag_r, 4)
                net_return_pct = round(realized_r * 0.5, 4)  # 0.50% constant monetary risk

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
                    else "Institutional Pullback / Edge Reversion"
                )

                trade_record = {
                    "shadow_trade_id": f"shd-{upper_sym.lower()}-{style.lower()}-{unix_sec}",
                    "symbol": upper_sym,
                    "trader_style": style,
                    "candidate_id": loaded["candidate_id"],
                    "trigger_timeframe": trigger_tf,
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
                    "tp1_level": tp1_price,
                    "tp2_level": tp2_price,
                    "exit_price": round(exit_price, 5),
                    "shadow_status": shadow_status,
                    "profitable": realized_r > 0,
                    "realized_r": realized_r,
                    "net_return_pct": net_return_pct,
                    "peak_mfe_r": round(peak_mfe_r, 3),
                    "worst_mae_r": round(worst_mae_r, 3),
                    "be_ratchet_triggered": be_active,
                    "tp1_partial_banked": tp1_filled,
                    "lifecycle_events": lifecycle_events,
                }

                style_trades.append(trade_record)
                shadow_trades.append(trade_record)

                # If a shadow trade failed, append to feedback outbox so Forge's Mistake Clinic learns from it
                if realized_r < 0:
                    feedback_event = {
                        "event_id": f"shd-fb-{unix_sec}-{upper_sym}-{style}",
                        "event_type": "forge.shadow_runner.trade_loss",
                        "symbol": upper_sym,
                        "trader_style": style,
                        "regime": bar.get("regime"),
                        "session": bar.get("session"),
                        "adaptive_session": bar.get("adaptive_session"),
                        "realized_r": realized_r,
                        "exit_reason": "STOP_INVALIDATION" if shadow_status == "CLOSED_STOP_LOSS" else "TIME_HORIZON_EXIT",
                        "failure_reason": f"Shadow {style} trade stopped out ({realized_r}R) in {bar.get('adaptive_session')} ({bar.get('regime')}); MFE reached +{round(peak_mfe_r, 2)}R before reversal.",
                        "timestamp_unix": time.time(),
                    }
                    with self.feedback_outbox_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(feedback_event) + "\n")

                i = max(i + 1, exit_idx + 1)

            wins = sum(1 for t in style_trades if t["realized_r"] > 0)
            losses = sum(1 for t in style_trades if t["realized_r"] <= 0)
            total_r = round(sum(t["realized_r"] for t in style_trades), 3)
            avg_r = round(total_r / max(1, len(style_trades)), 3)
            be_saves = sum(
                1 for t in style_trades if t["shadow_status"] in ("CLOSED_BREAKEVEN_PROTECTED", "CLOSED_TP1_AND_TRAILED_STOP")
            )

            style_summaries[style] = {
                "trader_style": style,
                "candidate_id": loaded["candidate_id"],
                "trigger_timeframe": trigger_tf,
                "shadow_trades_count": len(style_trades),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round((wins / max(1, len(style_trades))) * 100.0, 1),
                "total_realized_r": total_r,
                "expectancy_r": avg_r,
                "net_shadow_return_pct": round(total_r * 0.5, 2),
                "dynamic_be_and_tp1_saves": be_saves,
                "verdict": (
                    "PROFITABLE_IN_SHADOW"
                    if total_r > 0 and wins / max(1, len(style_trades)) >= 0.45
                    else "NEEDS_MISTAKE_CLINIC_REFINEMENT"
                ),
            }

            # Store non-duplicating lesson in Chronicle
            self.chronicle_ledger.record_lesson(
                domain="shadow_runner_evaluation",
                content=(
                    f"Shadow Runner evaluated {upper_sym} {style} ({trigger_tf}) on setups missed by Oracle V1: "
                    f"{len(style_trades)} shadow trades, WinRate={style_summaries[style]['win_rate_pct']}%, "
                    f"Total={total_r}R (Expectancy={avg_r}R, Dynamic BE/TP1 saves={be_saves})."
                ),
                concept_key=f"shadow_runner::{upper_sym}::{style}",
                tags=["forge", "shadow_runner", upper_sym, style],
                source_repository="forge",
            )

        shadow_trades.sort(key=lambda x: x["opened_at_ts"], reverse=True)
        total_wins = sum(1 for t in shadow_trades if t["realized_r"] > 0)
        total_net_r = round(sum(t["realized_r"] for t in shadow_trades), 3)

        m5_series = dataset.get("series", {}).get("M5", [])
        m15_series = dataset.get("series", {}).get("M15", [])

        report = {
            "schema": "forge.shadow_runner.report.v1",
            "mode": "BACKGROUND_SHADOW_ONLY_NO_DEMO_ORDERS",
            "symbol": upper_sym,
            "evaluated_at_unix": time.time(),
            "live_data_feed": {
                "source": dataset.get("source", "yahoo_finance_live_7tf_institutional"),
                "fetched_at_unix": dataset.get("fetched_at_unix"),
                "latest_m5_bar_ts": m5_series[-1]["ts"] if m5_series else None,
                "latest_m15_bar_ts": m15_series[-1]["ts"] if m15_series else None,
                "latest_live_price": m5_series[-1]["close"] if m5_series else (m15_series[-1]["close"] if m15_series else None),
            },
            "oracle_v1_api_bridge_monitor": v1_api_bridge,
            "oracle_v1_demo_trades_in_history": len(v1_timestamps),
            "v1_missed_opportunities_captured_by_v2": len(shadow_trades),
            "overall_shadow_win_rate_pct": round(
                (total_wins / max(1, len(shadow_trades))) * 100.0, 1
            ),
            "overall_shadow_total_r": total_net_r,
            "overall_shadow_net_return_pct": round(total_net_r * 0.5, 2),
            "style_summaries": style_summaries,
            "recent_shadow_trades": shadow_trades[:40],
        }

        # Persist to Forge/memory/shadow_trades_ledger.json
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

        return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forge V2 Background Shadow Runner (Tests V2 Strategies When Oracle V1 Is Idle)"
    )
    parser.add_argument(
        "--symbol", default="GBPUSD", help="Single symbol (e.g. GBPUSD, EURUSD)"
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols for multi-pair shadow monitoring (e.g. GBPUSD,EURUSD)",
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
        help="Number of recent trigger-timeframe bars to shadow-monitor",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in a background loop alongside Oracle V1",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=300,
        help="Seconds between shadow evaluation cycles when --daemon is active (default: 300s / 5m)",
    )
    args = parser.parse_args()

    symbols_list = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else [args.symbol.upper()]
    )

    runner = ForgeShadowRunner()

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
                        "v1_missed_opportunities_captured_by_v2": report.get(
                            "v1_missed_opportunities_captured_by_v2"
                        ),
                        "overall_shadow_win_rate_pct": report.get(
                            "overall_shadow_win_rate_pct"
                        ),
                        "overall_shadow_total_r": report.get("overall_shadow_total_r"),
                        "overall_shadow_net_return_pct": report.get(
                            "overall_shadow_net_return_pct"
                        ),
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

