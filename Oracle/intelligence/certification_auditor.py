"""
Oracle.intelligence.certification_auditor
==========================================
Verbose certification pipeline that traces EVERY decision from genome
creation through champion promotion. No genome can fail without an
explicit, human-readable reason.

Usage:
    auditor = CertificationAuditor(backtester)
    report = auditor.full_audit(genome, in_sample, out_sample)
    # report contains complete trace of every stage
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.certification")


class CertificationAuditor:
    """
    Traces a genome through training, validation, and certification.
    Records every signal decision for diagnostic visibility.
    """

    def __init__(self, backtester=None):
        self.backtester = backtester

    def full_audit(self, genome, in_sample, out_sample,
                   adaptive_thresh: float = 0.25) -> Dict[str, Any]:
        """
        Complete audit of a genome from evolution through promotion.
        Returns a detailed certification report.
        """
        report = {
            "genome_id": genome.genome_id,
            "genome_config": {
                "trend": {"type": genome.trend.logic_type, "params": genome.trend.params},
                "momentum": {"type": genome.momentum.logic_type, "params": genome.momentum.params},
                "volatility": {"type": genome.volatility.logic_type, "params": genome.volatility.params},
                "entry": {"type": genome.entry.logic_type, "params": genome.entry.params},
                "exit": {"type": genome.exit.logic_type, "params": genome.exit.params},
                "regime": {"type": genome.market_regime.logic_type, "params": genome.market_regime.params},
            },
            "training": {},
            "validation": {},
            "certification": {},
            "promotion": {},
        }

        # ---- Stage 1: Training (in-sample) ----
        log.info("=" * 60)
        log.info("CERTIFICATION AUDIT: %s", genome.genome_id)
        log.info("=" * 60)

        train_result = self._audit_stage(
            genome, in_sample, adaptive_thresh, stage_name="TRAINING"
        )
        report["training"] = train_result

        # ---- Stage 2: Validation (out-of-sample) ----
        val_result = self._audit_stage(
            genome, out_sample, adaptive_thresh, stage_name="VALIDATION"
        )
        report["validation"] = val_result

        # ---- Stage 3: Certification decision ----
        cert = self._certification_decision(train_result, val_result, out_sample)
        report["certification"] = cert
        report["promotion"] = cert

        return report

    def _audit_stage(self, genome, series, threshold: float,
                     stage_name: str = "STAGE") -> Dict[str, Any]:
        """
        Run a genome through a series with FULL signal tracing.
        Records every bar's decision and why.
        """
        try:
            from intelligence.technicals import analyze, sma, ema, rsi, macd, atr
        except ImportError:
            from Oracle.intelligence.technicals import analyze, sma, ema, rsi, macd, atr  # type: ignore

        closes = series.closes
        highs = series.highs
        lows = series.lows
        n = len(closes)
        symbol = getattr(series, 'symbol', 'unknown')

        log.info("--- %s AUDIT ---", stage_name)
        log.info("  Candles: %d", n)
        log.info("  Symbol: %s", symbol)
        log.info("  Threshold: %.3f", threshold)

        # Check indicator warmup requirements
        warmup_report = self._check_warmup(genome, n)
        log.info("  Indicator warmup: %s", warmup_report)

        # Determine effective warmup
        min_warmup = warmup_report["required_bars"]
        effective_warmup = min(min_warmup, max(5, n - 10))

        if n < effective_warmup + 5:
            msg = (f"{stage_name}: Only {n} bars but need {effective_warmup}+5. "
                   f"Cannot generate any signals.")
            log.warning("  %s", msg)
            return {
                "candles": n,
                "effective_warmup": effective_warmup,
                "tradeable_bars": 0,
                "signals": 0,
                "trades": 0,
                "return": 0.0,
                "error": msg,
                "signal_trace": [],
            }

        tradeable_bars = n - effective_warmup
        log.info("  Tradeable bars: %d (warmup=%d)", tradeable_bars, effective_warmup)

        # ---- Signal trace: every bar decision ----
        signal_trace = []
        total_signals = 0
        buy_signals = 0
        sell_signals = 0
        hold_count = 0
        rejection_reasons: Dict[str, int] = {}

        # Collect votes for analysis
        all_votes = []
        nan_count = 0

        for i in range(effective_warmup, n):
            bar_closes = closes[:i + 1]
            bar_highs = highs[:i + 1]
            bar_lows = lows[:i + 1]

            # Build series view
            try:
                from intelligence.evolution import make_series
            except ImportError:
                from Oracle.intelligence.evolution import make_series  # type: ignore
            s = make_series(bar_closes, bar_highs, bar_lows, symbol)

            # 1. Compute vote
            try:
                vote = genome.vote(s)
            except Exception as exc:
                vote = 0.0
                nan_count += 1

            if vote != vote:  # NaN check
                nan_count += 1
                vote = 0.0

            all_votes.append(vote)

            # 2. Check threshold
            passes_threshold = abs(vote) >= threshold
            direction = "buy" if vote > 0 else "sell" if vote < 0 else "hold"

            if not passes_threshold:
                hold_count += 1
                reason = f"vote={vote:.3f} < threshold={threshold:.3f}"
                rejection_reasons["below_threshold"] = rejection_reasons.get("below_threshold", 0) + 1
                # Only trace first few + last few to avoid massive logs
                if i < effective_warmup + 3 or i > n - 3:
                    signal_trace.append({
                        "bar": i, "vote": round(vote, 4),
                        "decision": "hold", "reason": reason
                    })
            else:
                total_signals += 1
                if direction == "buy":
                    buy_signals += 1
                else:
                    sell_signals += 1
                signal_trace.append({
                    "bar": i, "vote": round(vote, 4),
                    "decision": direction, "reason": "threshold_passed"
                })

        log.info("  Signals generated: %d (buy=%d, sell=%d, hold=%d)",
                 total_signals, buy_signals, sell_signals, hold_count)
        log.info("  NaN values detected: %d", nan_count)

        if all_votes:
            avg_abs_vote = sum(abs(v) for v in all_votes) / len(all_votes)
            max_abs_vote = max(abs(v) for v in all_votes)
            log.info("  Vote stats: avg|vote|=%.4f, max|vote|=%.4f, threshold=%.3f",
                     avg_abs_vote, max_abs_vote, threshold)
        else:
            avg_abs_vote = 0.0
            max_abs_vote = 0.0

        # ---- Run actual backtest ----
        backtest_result = {"trades": 0, "total_return": 0.0, "status": "not_run"}
        if total_signals > 0 and self.backtester:
            def decide(c, h, l, **kwargs):
                s = make_series(c, h, l, symbol)
                v = genome.vote(s)
                if abs(v) >= threshold:
                    return {"call": "buy" if v > 0 else "sell"}
                return {"call": "hold"}

            try:
                backtest_result = self.backtester.run(series, decide, warmup=effective_warmup)
            except Exception as exc:
                backtest_result = {"status": "error", "message": str(exc),
                                   "trades": 0, "total_return": 0.0}
                log.warning("  Backtest crashed: %s", exc)

        actual_trades = backtest_result.get("trades", 0) or 0
        actual_return = backtest_result.get("total_return", 0.0) or 0.0

        log.info("  Backtest: status=%s, trades=%d, return=%.4f",
                 backtest_result.get("status"), actual_trades, actual_return)

        # If signals exist but no trades, explain why
        trade_gap_reason = ""
        if total_signals > 0 and actual_trades == 0:
            trade_gap_reason = self._explain_signal_trade_gap(
                total_signals, backtest_result, n, effective_warmup
            )
            log.warning("  SIGNAL-TRADE GAP: %s", trade_gap_reason)

        return {
            "candles": n,
            "effective_warmup": effective_warmup,
            "tradeable_bars": tradeable_bars,
            "nan_values": nan_count,
            "signals": total_signals,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "hold_bars": hold_count,
            "trades": actual_trades,
            "return": actual_return,
            "sharpe": backtest_result.get("sharpe_proxy", 0.0),
            "drawdown": backtest_result.get("max_drawdown", 0.0),
            "win_rate": backtest_result.get("win_rate", 0.0),
            "backtest_status": backtest_result.get("status", "unknown"),
            "backtest_message": backtest_result.get("message", ""),
            "vote_stats": {
                "avg_abs": round(avg_abs_vote, 4),
                "max_abs": round(max_abs_vote, 4),
                "threshold": threshold,
                "pct_above_threshold": round(total_signals / max(tradeable_bars, 1) * 100, 1),
            },
            "rejection_breakdown": rejection_reasons,
            "trade_gap_reason": trade_gap_reason,
            "signal_trace_sample": signal_trace[:10],  # First 10 for readability
        }

    def _check_warmup(self, genome, n: int) -> Dict[str, Any]:
        """Determine indicator warmup requirements vs available data."""
        required = 5  # Minimum

        # Trend module requirements
        trend_params = genome.trend.params
        if genome.trend.logic_type == "sma_crossover":
            required = max(required, int(trend_params.get("slow", 50)) + 1)
        elif genome.trend.logic_type in ("ema_slope", "hma_slope"):
            required = max(required, int(trend_params.get("period", 20)) + 2)
        elif genome.trend.logic_type == "price_above_sma":
            required = max(required, int(trend_params.get("period", 50)) + 1)
        elif genome.trend.logic_type in ("supertrend", "donchian_trend"):
            required = max(required, int(trend_params.get("period", 20)) + 2)
        elif genome.trend.logic_type == "ichimoku_cloud":
            required = max(required, int(trend_params.get("kijun", 26)) + 1)
        elif genome.trend.logic_type == "adx_trend":
            required = max(required, int(trend_params.get("period", 14)) + 3)

        # Momentum module
        mom_params = genome.momentum.params
        if genome.momentum.logic_type == "rsi":
            required = max(required, int(mom_params.get("period", 14)) + 2)
        elif genome.momentum.logic_type == "macd_hist":
            required = max(required, int(mom_params.get("slow", 26)) + 10)
        elif genome.momentum.logic_type in ("stochastic", "williams_r", "cci"):
            required = max(required, int(mom_params.get("period", mom_params.get("k_period", 14))) + 2)
        elif genome.momentum.logic_type == "roc":
            required = max(required, int(mom_params.get("period", 12)) + 2)

        sufficient = n >= required + 5
        return {
            "required_bars": required,
            "available_bars": n,
            "sufficient": sufficient,
            "gap": max(0, required + 5 - n),
        }

    def _explain_signal_trade_gap(self, signals: int, result: Dict, n: int, warmup: int) -> str:
        """Explain why signals were generated but backtester produced zero trades."""
        status = result.get("status", "unknown")
        message = result.get("message", "")

        if status == "error":
            if "insufficient" in message.lower():
                return (f"Backtester rejected: '{message}'. "
                        f"Series has {n} bars, warmup={warmup}, "
                        f"backtester needs warmup+20={warmup+20} bars. "
                        f"{'INSUFFICIENT' if n < warmup + 20 else 'Should be sufficient'}")
            return f"Backtester error: {message}"

        if status == "complete" and result.get("trades", 0) == 0:
            return ("Backtester ran but no positions opened. Possible causes: "
                    "ATR=0 making size=0, all signals came while position was open, "
                    "or signals only appeared in last few bars with no time to exit.")

        return f"Unknown gap: status={status}, signals={signals}"

    def _certification_decision(self, training: Dict, validation: Dict,
                                 out_sample) -> Dict[str, Any]:
        """
        Apply certification rules and report EXACTLY which rule accepts/rejects.
        """
        rules_checked = []
        all_pass = True

        # Rule 1: Validation must have completed
        val_status = validation.get("backtest_status", "unknown")
        rule1 = {
            "rule": "validation_completed",
            "required": "status == complete",
            "actual": val_status,
            "passed": val_status == "complete",
        }
        rules_checked.append(rule1)
        if not rule1["passed"]:
            all_pass = False

        # Rule 2: Minimum trades (adaptive to OOS length)
        oos_bars = len(out_sample) if hasattr(out_sample, '__len__') else validation.get("candles", 0)
        min_trades = max(1, oos_bars // 30)
        val_trades = validation.get("trades", 0)
        rule2 = {
            "rule": "minimum_trades",
            "required": f">= {min_trades} (adaptive: {oos_bars} bars / 30)",
            "actual": val_trades,
            "passed": val_trades >= min_trades,
        }
        rules_checked.append(rule2)
        if not rule2["passed"]:
            all_pass = False

        # Rule 3: Positive return
        val_return = validation.get("return", 0.0)
        rule3 = {
            "rule": "positive_return",
            "required": "> 0.0",
            "actual": round(val_return, 4),
            "passed": val_return > 0,
        }
        rules_checked.append(rule3)
        if not rule3["passed"]:
            all_pass = False

        # Rule 4: Training must be profitable (sanity check)
        train_return = training.get("return", 0.0)
        rule4 = {
            "rule": "training_profitable",
            "required": "> 0.0",
            "actual": round(train_return, 4),
            "passed": train_return > 0,
        }
        rules_checked.append(rule4)
        if not rule4["passed"]:
            all_pass = False

        # Determine primary rejection reason
        rejection_reason = "ACCEPTED" if all_pass else ""
        if not all_pass:
            for rule in rules_checked:
                if not rule["passed"]:
                    rejection_reason = (
                        f"REJECTED by [{rule['rule']}]: "
                        f"required {rule['required']}, got {rule['actual']}"
                    )
                    break

        # If validation had zero trades, add detailed diagnosis
        if val_trades == 0:
            trade_gap = validation.get("trade_gap_reason", "")
            if not trade_gap:
                # Check if it was a data issue
                if validation.get("candles", 0) < 20:
                    trade_gap = f"OOS too short: only {validation.get('candles', 0)} bars"
                elif validation.get("signals", 0) == 0:
                    vote_stats = validation.get("vote_stats", {})
                    trade_gap = (
                        f"Zero signals generated. avg|vote|={vote_stats.get('avg_abs', 0):.3f}, "
                        f"max|vote|={vote_stats.get('max_abs', 0):.3f}, "
                        f"threshold={vote_stats.get('threshold', 0):.3f}"
                    )
                else:
                    trade_gap = (
                        f"Signals existed ({validation.get('signals', 0)}) but "
                        f"backtester error: {validation.get('backtest_message', 'unknown')}"
                    )
            rejection_reason += f" | DIAGNOSIS: {trade_gap}"

        decision = {
            "promoted": all_pass,
            "rejection_reason": rejection_reason,
            "rules_checked": rules_checked,
            "training_summary": {
                "trades": training.get("trades", 0),
                "return": round(training.get("return", 0.0), 4),
                "signals": training.get("signals", 0),
            },
            "validation_summary": {
                "candles": validation.get("candles", 0),
                "warmup": validation.get("effective_warmup", 0),
                "tradeable_bars": validation.get("tradeable_bars", 0),
                "signals": validation.get("signals", 0),
                "trades": val_trades,
                "return": round(val_return, 4),
                "sharpe": validation.get("sharpe", 0.0),
                "drawdown": validation.get("drawdown", 0.0),
                "win_rate": validation.get("win_rate", 0.0),
            },
        }

        # Log the full decision
        log.info("--- CERTIFICATION DECISION ---")
        for rule in rules_checked:
            status = "✓" if rule["passed"] else "✗"
            log.info("  %s [%s] required=%s actual=%s",
                     status, rule["rule"], rule["required"], rule["actual"])
        log.info("  RESULT: %s", "PROMOTED ✅" if all_pass else rejection_reason)

        return decision