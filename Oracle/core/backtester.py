"""
Oracle.core.backtester
======================
Walk-forward backtesting. (Book III Part II Ch XV Certification; Book I Article
X: observed outcome vs expected; institutional practice: never trade a strategy
you have not validated on out-of-sample history.)

A real desk validates before it risks capital. This engine replays a strategy
bar-by-bar over historical data WITHOUT lookahead (each decision uses only data
up to that bar), applies the same risk rules as live, and reports institutional
performance metrics: total return, win rate, profit factor, max drawdown, and a
Sharpe proxy. Every metric is computed from the simulated equity curve.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional


class Backtester:
    def __init__(self, risk_per_trade: float = 0.01, starting_equity: float = 10000.0):
        self.risk_per_trade = risk_per_trade
        self.starting_equity = starting_equity

    def run(self, series, decide_fn: Callable[[List[float], List[float], List[float]], Dict[str, Any]],
            warmup: int = 50, atr_period: int = 14) -> Dict[str, Any]:
        """
        decide_fn(closes_so_far, highs_so_far, lows_so_far) -> {"call","direction"} using
        ONLY data up to the current bar (no lookahead). One position at a time.
        """
        closes, highs, lows = series.closes, series.lows, series.highs  # note: guarded below
        closes, highs, lows = series.closes, series.highs, series.lows
        n = len(closes)
        if n < warmup + 20:
            return {"status": "error", "message": "insufficient history to backtest"}

        equity = self.starting_equity
        peak = equity
        max_dd = 0.0
        trades: List[Dict[str, Any]] = []
        position = None  # {"dir","entry","stop","target","size"}

        for i in range(warmup, n):
            price = closes[i]
            
            # ATR-based calculation for stops/trailing
            trs = []
            for k in range(i - atr_period, i):
                if k > 0:
                    trs.append(max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]),
                                 abs(lows[k] - closes[k - 1])))
            atr_val = sum(trs) / len(trs) if trs else price * 0.01

            # manage open position
            if position:
                # Allow decide_fn to update management parameters
                mgmt = decide_fn(closes[:i + 1], highs[:i + 1], lows[:i + 1], 
                                current_stop=position["stop"], direction=position["dir"], atr_val=atr_val)
                if mgmt.get("updated_stop"):
                    position["stop"] = mgmt["updated_stop"]

                hit_stop = (position["dir"] == 1 and lows[i] <= position["stop"]) or \
                          (position["dir"] == -1 and highs[i] >= position["stop"])
                hit_target = (position["dir"] == 1 and highs[i] >= position["target"]) or \
                            (position["dir"] == -1 and lows[i] <= position["target"])
                exit_price = None
                if hit_stop:
                    exit_price = position["stop"]
                elif hit_target:
                    exit_price = position["target"]
                if exit_price is not None:
                    pnl = position["dir"] * (exit_price - position["entry"]) * position["size"]
                    equity += pnl
                    trades.append({"entry": position["entry"], "exit": exit_price,
                                 "dir": position["dir"], "pnl": round(pnl, 2),
                                 "win": pnl > 0})
                    peak = max(peak, equity)
                    max_dd = max(max_dd, (peak - equity) / peak if peak else 0)
                    position = None

            # consider a new entry only when flat
            if position is None:
                decision = decide_fn(closes[:i + 1], highs[:i + 1], lows[:i + 1])
                call = decision.get("call", "hold")
                if call in ("buy", "sell"):
                    direction = 1 if call == "buy" else -1
                    stop = price - direction * 2 * atr_val
                    target = price + direction * 3 * atr_val
                    risk_budget = equity * self.risk_per_trade
                    size = risk_budget / (2 * atr_val) if atr_val else 0
                    if size > 0:
                        position = {"dir": direction, "entry": price, "stop": stop,
                                  "target": target, "size": size}

        # close any residual position at last price
        if position:
            pnl = position["dir"] * (closes[-1] - position["entry"]) * position["size"]
            equity += pnl
            trades.append({"entry": position["entry"], "exit": closes[-1],
                         "dir": position["dir"], "pnl": round(pnl, 2), "win": pnl > 0})

        return self._metrics(equity, trades, max_dd)

    def _metrics(self, equity: float, trades: List[Dict], max_dd: float) -> Dict[str, Any]:
        n = len(trades)
        if n == 0:
            return {"status": "complete", "trades": 0, "note": "no trades generated",
                   "total_return": 0.0, "final_equity": round(equity, 2)}
        wins = [t for t in trades if t["win"]]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in trades if not t["win"]))
        pnls = [t["pnl"] for t in trades]
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / n
        downside = [min(p, 0.0) for p in pnls]
        downside_var = sum(p ** 2 for p in downside) / n
        sharpe = mean / math.sqrt(var) if var else 0.0
        sortino = mean / math.sqrt(downside_var) if downside_var else 0.0
        recovery = ((equity - self.starting_equity) / self.starting_equity) / max_dd if max_dd else 0.0
        expectancy = sum(pnls) / n
        consistency = len(wins) / n if n else 0.0
        return {"status": "complete", "trades": n, "win_rate": round(len(wins) / n, 3),
               "total_return": round((equity - self.starting_equity) / self.starting_equity, 4),
               "final_equity": round(equity, 2),
               "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
               "max_drawdown": round(max_dd, 4), "sharpe_proxy": round(sharpe, 3),
               "sortino_proxy": round(sortino, 3),
               "recovery_factor": round(recovery, 3),
               "expectancy": round(expectancy, 2),
               "consistency": round(consistency, 3),
               "avg_pnl": round(mean, 2)}
