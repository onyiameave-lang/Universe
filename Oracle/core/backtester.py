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


class _PrefixView:
    """Read-only list-like view over a history prefix without copying it."""

    __slots__ = ("_values", "_length")

    def __init__(self, values: List[float], length: int):
        self._values = values
        self._length = length

    def __len__(self):
        return self._length

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self._length)
            return self._values[start:stop:step]
        if index < 0:
            index += self._length
        if index < 0 or index >= self._length:
            raise IndexError("history prefix index out of range")
        return self._values[index]

    def __iter__(self):
        for index in range(self._length):
            yield self._values[index]


class Backtester:
    def __init__(self, risk_per_trade: float = 0.01, starting_equity: float = 10000.0):
        self.risk_per_trade = risk_per_trade
        self.starting_equity = starting_equity

    def run(self, series, decide_fn: Callable[[List[float], List[float], List[float]], Dict[str, Any]],
            warmup: int = 50, atr_period: int = 14,
            capture_bar_returns: bool = False,
            execution_mode: str = "next_open",
            commission_bps: float = 0.0,
            slippage_bps: float = 0.0,
            spread_bps: float = 0.0,
            price_basis: Optional[str] = None,
            strategy_genome: Optional[Any] = None) -> Dict[str, Any]:
        """
        ``decide_fn(closes_so_far, highs_so_far, lows_so_far)`` returns a call.
        Use ``execution_mode="next_open"`` when decisions observe the current
        close; it fills orders at the next bar's open. One position at a time.

        ``next_open`` evaluates the signal after a completed bar and fills it at
        the following bar's open. ``legacy_close`` is available only to reproduce
        historical results and must not be used for governed promotion evidence.
        """
        if execution_mode == "next_open":
            return self._run_next_open(
                series, decide_fn, warmup, atr_period, capture_bar_returns,
                commission_bps, slippage_bps, spread_bps, price_basis, strategy_genome,
            )
        if execution_mode != "legacy_close":
            raise ValueError("execution_mode must be 'legacy_close' or 'next_open'")
        closes, highs, lows = series.closes, series.lows, series.highs  # note: guarded below
        closes, highs, lows = series.closes, series.highs, series.lows
        n = len(closes)
        if n < warmup + 20:
            return {"status": "error", "message": "insufficient history to backtest"}

        equity = self.starting_equity
        peak = equity
        max_dd = 0.0
        trades: List[Dict[str, Any]] = []
        bar_returns: List[float] = []
        previous_marked_equity = equity
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

            if capture_bar_returns:
                marked_equity = equity
                if position is not None:
                    marked_equity += (
                        position["dir"] * (price - position["entry"]) * position["size"]
                    )
                if previous_marked_equity <= 0:
                    raise ValueError("Backtest equity became non-positive while capturing bar returns")
                bar_return = marked_equity / previous_marked_equity - 1.0
                if not math.isfinite(bar_return):
                    raise ValueError("Backtest produced a non-finite bar return")
                bar_returns.append(bar_return)
                previous_marked_equity = marked_equity

        # close any residual position at last price
        if position:
            pnl = position["dir"] * (closes[-1] - position["entry"]) * position["size"]
            equity += pnl
            trades.append({"entry": position["entry"], "exit": closes[-1],
                         "dir": position["dir"], "pnl": round(pnl, 2), "win": pnl > 0})

        result = self._metrics(equity, trades, max_dd)
        if capture_bar_returns:
            result["bar_returns"] = bar_returns
            result["bar_observation_count"] = len(bar_returns)
        return result

    def _run_next_open(
        self,
        series,
        decide_fn,
        warmup: int,
        atr_period: int,
        capture_bar_returns: bool,
        commission_bps: float,
        slippage_bps: float,
        spread_bps: float,
        price_basis: str,
        strategy_genome: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Simulate close-derived signals with next-bar-open fills."""
        costs = {"commission_bps": commission_bps, "slippage_bps": slippage_bps,
                 "spread_bps": spread_bps}
        for name, value in costs.items():
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 10000
            ):
                raise ValueError(f"{name} must be finite and between 0 and 10000 basis points")
        if price_basis is None and float(spread_bps) == 0:
            # Preserve legacy no-spread research calls. Forge evaluations require
            # an explicit price basis and a complete cost profile before running.
            execution_price_basis = "mid"
        elif price_basis in {"mid", "bid", "ask"}:
            execution_price_basis = price_basis
        else:
            raise ValueError("price_basis must explicitly identify OHLC prices as 'mid', 'bid', or 'ask'")
        commission_rate = float(commission_bps) / 10000.0
        slippage_rate = float(slippage_bps) / 10000.0
        spread_rate = float(spread_bps) / 10000.0
        use_prefix_views = getattr(decide_fn, "supports_prefix_views", False) is True

        def history_prefix(values, end):
            return _PrefixView(values, end) if use_prefix_views else values[:end]

        opens, closes, highs, lows = series.opens, series.closes, series.highs, series.lows
        n = len(closes)
        if n < warmup + 20:
            return {"status": "error", "message": "insufficient history to backtest"}
        if len(opens) != n or len(highs) != n or len(lows) != n:
            raise ValueError("OHLC series must have matching row counts")

        equity = self.starting_equity
        peak = equity
        max_dd = 0.0
        trades: List[Dict[str, Any]] = []
        bar_returns: List[float] = []
        previous_marked_equity = equity
        final_bar_start_equity = equity
        position = None
        pending_entry = None

        def bid_ask(price: float) -> tuple[float, float]:
            if execution_price_basis == "mid":
                half = spread_rate / 2.0
                return price * (1.0 - half), price * (1.0 + half)
            if execution_price_basis == "bid":
                return price, price * (1.0 + spread_rate)
            ask = price
            return ask / (1.0 + spread_rate), ask

        def atr_at(index: int) -> float:
            true_ranges = []
            for bar_index in range(max(1, index - atr_period + 1), index + 1):
                true_ranges.append(max(
                    highs[bar_index] - lows[bar_index],
                    abs(highs[bar_index] - closes[bar_index - 1]),
                    abs(lows[bar_index] - closes[bar_index - 1]),
                ))
            return sum(true_ranges) / len(true_ranges) if true_ranges else closes[index] * 0.01

        for i in range(warmup, n):
            final_bar_start_equity = previous_marked_equity
            open_price = opens[i]
            close_price = closes[i]
            bid_open, ask_open = bid_ask(open_price)
            bid_high, ask_high = bid_ask(highs[i])
            bid_low, ask_low = bid_ask(lows[i])

            if pending_entry is not None and position is None:
                direction = pending_entry["direction"]
                atr_value = pending_entry["atr"]
                current_drawdown = (peak - equity) / peak if peak > 0 else 1.0
                risk_module = getattr(strategy_genome, "risk", None)
                if risk_module is not None and not risk_module.check(current_drawdown):
                    pending_entry = None
                else:
                    position_module = getattr(strategy_genome, "position", None)
                    position_params = getattr(position_module, "params", {}) or {}
                    risk_fraction = float(position_params.get("risk_fraction", self.risk_per_trade))
                    if not math.isfinite(risk_fraction) or not 0 < risk_fraction <= 0.10:
                        raise ValueError("strategy risk_fraction must be finite and in (0, 0.10]")
                    risk_budget = equity * risk_fraction
                    entry_quote = ask_open if direction == 1 else bid_open
                    fill_price = entry_quote * (1.0 + direction * slippage_rate)
                    exit_module = getattr(strategy_genome, "exit", None)
                    if exit_module is None:
                        stop_price = fill_price - direction * 2 * atr_value
                        target_price = fill_price + direction * 3 * atr_value
                    else:
                        stop_price, target_price = exit_module.get_stops(fill_price, atr_value, direction)
                    stop_distance = abs(fill_price - stop_price)
                    if not math.isfinite(stop_distance) or stop_distance <= 0:
                        raise ValueError("strategy exit module produced an invalid stop distance")
                    per_unit_risk = (
                        stop_distance
                        + 2 * abs(fill_price) * (commission_rate + slippage_rate)
                        + abs(fill_price) * spread_rate
                    )
                    size = (position_module.get_size(equity, risk_fraction, per_unit_risk)
                            if position_module is not None else risk_budget / per_unit_risk)
                    if size > 0:
                        entry_fee = abs(fill_price * size) * commission_rate
                        equity -= entry_fee
                        position = {
                            "dir": direction,
                            "entry": fill_price,
                            "entry_fee": entry_fee,
                            "stop": stop_price,
                            "target": target_price,
                            "size": size,
                        }
                        if equity <= 0:
                            raise ValueError("Trading costs exhausted backtest equity at entry")
                    pending_entry = None

            if position is not None:
                stop_price = position["stop"]
                target_price = position["target"]
                direction = position["dir"]
                execution_open = ask_open if direction == -1 else bid_open
                execution_high = ask_high if direction == -1 else bid_high
                execution_low = ask_low if direction == -1 else bid_low
                if direction == 1:
                    open_stop = execution_open <= stop_price
                    open_target = execution_open >= target_price
                    range_stop = execution_low <= stop_price
                    range_target = execution_high >= target_price
                else:
                    open_stop = execution_open >= stop_price
                    open_target = execution_open <= target_price
                    range_stop = execution_high >= stop_price
                    range_target = execution_low <= target_price

                exit_price = None
                if open_stop:
                    exit_price = execution_open
                elif open_target:
                    exit_price = execution_open
                elif range_stop:
                    exit_price = stop_price
                elif range_target:
                    exit_price = target_price
                if exit_price is not None:
                    fill_exit = exit_price * (1.0 - direction * slippage_rate)
                    exit_fee = abs(fill_exit * position["size"]) * commission_rate
                    gross_pnl = direction * (fill_exit - position["entry"]) * position["size"]
                    equity += gross_pnl - exit_fee
                    net_pnl = gross_pnl - exit_fee - position["entry_fee"]
                    trades.append({
                        "entry": position["entry"], "exit": fill_exit,
                        "dir": direction, "pnl": round(net_pnl, 2), "win": net_pnl > 0,
                    })
                    position = None

            if position is not None:
                management = decide_fn(
                    history_prefix(closes, i + 1), history_prefix(highs, i + 1),
                    history_prefix(lows, i + 1),
                    current_stop=position["stop"], direction=position["dir"],
                    atr_val=atr_at(i),
                )
                updated_stop = management.get("updated_stop") if isinstance(management, dict) else None
                if updated_stop is not None:
                    updated_stop = float(updated_stop)
                    if not math.isfinite(updated_stop):
                        raise ValueError("Strategy produced a non-finite stop price")
                    position["stop"] = updated_stop

            if position is None and i < n - 1:
                decision = decide_fn(
                    history_prefix(closes, i + 1), history_prefix(highs, i + 1),
                    history_prefix(lows, i + 1),
                )
                call = decision.get("call", "hold") if isinstance(decision, dict) else "hold"
                if call in ("buy", "sell"):
                    pending_entry = {
                        "direction": 1 if call == "buy" else -1,
                        "atr": atr_at(i),
                    }

            marked_equity = equity
            if position is not None:
                bid_close, ask_close = bid_ask(close_price)
                liquidation_quote = bid_close if position["dir"] == 1 else ask_close
                liquidation_fill = liquidation_quote * (1.0 - position["dir"] * slippage_rate)
                marked_equity += (
                    position["dir"] * (liquidation_fill - position["entry"]) * position["size"]
                    - abs(liquidation_fill * position["size"]) * commission_rate
                )
            if marked_equity <= 0 or not math.isfinite(marked_equity):
                raise ValueError("Backtest equity became non-positive or non-finite")
            peak = max(peak, marked_equity)
            max_dd = max(max_dd, (peak - marked_equity) / peak if peak else 0.0)
            if capture_bar_returns:
                if previous_marked_equity <= 0:
                    raise ValueError("Backtest equity became non-positive while capturing bar returns")
                bar_return = marked_equity / previous_marked_equity - 1.0
                if not math.isfinite(bar_return):
                    raise ValueError("Backtest produced a non-finite bar return")
                bar_returns.append(bar_return)
                previous_marked_equity = marked_equity

        if position is not None:
            bid_close, ask_close = bid_ask(closes[-1])
            liquidation_quote = bid_close if position["dir"] == 1 else ask_close
            exit_price = liquidation_quote * (1.0 - position["dir"] * slippage_rate)
            exit_fee = abs(exit_price * position["size"]) * commission_rate
            gross_pnl = position["dir"] * (exit_price - position["entry"]) * position["size"]
            equity += gross_pnl - exit_fee
            net_pnl = gross_pnl - exit_fee - position["entry_fee"]
            trades.append({
                "entry": position["entry"], "exit": exit_price,
                "dir": position["dir"], "pnl": round(net_pnl, 2), "win": net_pnl > 0,
            })
            if capture_bar_returns and final_bar_start_equity > 0:
                # Include terminal spread, slippage, and commission in the final
                # statistical observation rather than omitting liquidation cost.
                bar_returns[-1] = equity / final_bar_start_equity - 1.0
            max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
        result = self._metrics(equity, trades, max_dd)
        if capture_bar_returns:
            result["bar_returns"] = bar_returns
            result["bar_observation_count"] = len(bar_returns)
        result["execution_mode"] = "next_open"
        result["commission_bps_per_side"] = commission_bps
        result["slippage_bps_per_side"] = slippage_bps
        result["spread_bps_full"] = spread_bps
        result["price_basis"] = price_basis or "unspecified_no_spread"
        return result

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
        return {"status": "complete", "trades": n, "winning_trades": len(wins),
               "win_rate": round(len(wins) / n, 3), "win_rate_raw": len(wins) / n,
               "total_return": round((equity - self.starting_equity) / self.starting_equity, 4),
               "total_return_raw": (equity - self.starting_equity) / self.starting_equity,
               "final_equity": round(equity, 2),
               "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
               "max_drawdown": round(max_dd, 4), "max_drawdown_raw": max_dd,
               "sharpe_proxy": round(sharpe, 3),
               "sortino_proxy": round(sortino, 3),
               "recovery_factor": round(recovery, 3),
               "expectancy": round(expectancy, 2),
               "consistency": round(consistency, 3),
               "avg_pnl": round(mean, 2)}
