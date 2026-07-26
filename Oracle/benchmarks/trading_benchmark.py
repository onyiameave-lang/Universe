"""
Oracle Trading Benchmark ("Benchmark Everything" — roadmap Phase 1 item 5)
============================================================================
NOT to be confused with benchmarks/benchmark_engine.py, which tracks
research/evolution metrics (backtest promotion rate, OOS Sharpe, the
Oracle Intelligence Score). That engine is observer-only over the
*evolution* pipeline and is left completely untouched by this module.

This module tracks the roadmap's actual ask: metrics over REAL (paper or
demo) trade outcomes as they close --

    Win rate, Profit factor, Sharpe, Drawdown, Average trade,
    Average holding time, Maximum consecutive losses, Recovery factor

-- saved incrementally after every trade (not just "after every session",
since with intermittent runtime there may not be a clean session
boundary -- saving on every close is strictly more robust).

Fed by intelligence.trade_learning.TradeLearningEngine.record_close(),
which already computes a TradeOutcome for every closed position regardless
of which path closed it (Continuous Trade Manager or native broker SL/TP).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("oracle.trading_benchmark")

DEFAULT_STORE_DIR = Path(__file__).resolve().parent  # Oracle/benchmarks/


def _atomic_write(path: Path, content: str) -> None:
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(path))
    except Exception as exc:
        log.warning("atomic write failed for %s: %s; falling back", path, exc)
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc2:
            log.error("fallback write also failed for %s: %s", path, exc2)


class TradingBenchmark:
    def __init__(self, storage_dir: Optional[Path] = None):
        self._dir = storage_dir or DEFAULT_STORE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._json_path = self._dir / "trading_benchmark.json"
        self._markdown_path = self._dir / "TRADING_BENCHMARKS.md"
        self._data = self._load_or_init()

    # -- persistence -------------------------------------------------------

    def _load_or_init(self) -> Dict[str, Any]:
        if self._json_path.exists():
            try:
                data = json.loads(self._json_path.read_text())
                for key, val in self._default().items():
                    data.setdefault(key, val)
                return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("trading_benchmark.json unreadable (%s); starting fresh", exc)
        return self._default()

    @staticmethod
    def _default() -> Dict[str, Any]:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "sum_r": 0.0, "sum_win_r": 0.0, "sum_loss_r": 0.0, "sum_r_squared": 0.0,
            "sum_duration_sec": 0.0,
            "current_win_streak": 0, "max_consecutive_wins": 0,
            "current_loss_streak": 0, "max_consecutive_losses": 0,
            "equity_r": 0.0, "peak_equity_r": 0.0, "max_drawdown_r": 0.0,
            "by_symbol": {},
            "last_updated": None,
        }

    def _save(self) -> None:
        _atomic_write(self._json_path, json.dumps(self._data, indent=2, sort_keys=True))

    # -- recording -----------------------------------------------------------

    def record_trade(self, outcome) -> Dict[str, Any]:
        """
        outcome: an intelligence.trade_learning.TradeOutcome (duck-typed —
        only .symbol/.pnl_r/.won/.duration_sec are read, so any object with
        those attributes works, which keeps this testable standalone).
        """
        d = self._data
        d["total_trades"] += 1
        pnl_r = float(outcome.pnl_r)
        d["sum_r"] += pnl_r
        d["sum_r_squared"] += pnl_r * pnl_r
        d["sum_duration_sec"] += float(outcome.duration_sec)

        if outcome.won:
            d["wins"] += 1
            d["sum_win_r"] += pnl_r
            d["current_win_streak"] += 1
            d["current_loss_streak"] = 0
            d["max_consecutive_wins"] = max(d["max_consecutive_wins"], d["current_win_streak"])
        else:
            d["losses"] += 1
            d["sum_loss_r"] += pnl_r   # pnl_r is negative for a loss
            d["current_loss_streak"] += 1
            d["current_win_streak"] = 0
            d["max_consecutive_losses"] = max(d["max_consecutive_losses"], d["current_loss_streak"])

        # Running equity curve (in R) and drawdown, same convention as the
        # Continuous Trade Manager's per-trade R-multiples.
        d["equity_r"] += pnl_r
        d["peak_equity_r"] = max(d["peak_equity_r"], d["equity_r"])
        drawdown_now = d["peak_equity_r"] - d["equity_r"]
        d["max_drawdown_r"] = max(d["max_drawdown_r"], drawdown_now)

        # Per-symbol breakdown.
        sym = d["by_symbol"].setdefault(outcome.symbol.upper(), {
            "trades": 0, "wins": 0, "losses": 0, "sum_r": 0.0,
        })
        sym["trades"] += 1
        sym["sum_r"] += pnl_r
        if outcome.won:
            sym["wins"] += 1
        else:
            sym["losses"] += 1

        d["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()
        self._regenerate_markdown()
        return self.summary()

    # -- derived metrics -----------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        d = self._data
        n = d["total_trades"]
        if n == 0:
            return {"total_trades": 0, "note": "no closed trades yet"}

        win_rate = d["wins"] / n
        avg_trade_r = d["sum_r"] / n
        avg_holding_min = (d["sum_duration_sec"] / n) / 60.0

        loss_sum_abs = abs(d["sum_loss_r"])
        profit_factor = (d["sum_win_r"] / loss_sum_abs) if loss_sum_abs > 1e-9 else None

        mean = avg_trade_r
        variance = max(0.0, d["sum_r_squared"] / n - mean * mean)
        std = variance ** 0.5
        sharpe_proxy = (mean / std) if std > 1e-9 else None

        recovery_factor = (d["equity_r"] / d["max_drawdown_r"]) if d["max_drawdown_r"] > 1e-9 else None

        return {
            "total_trades": n,
            "wins": d["wins"], "losses": d["losses"],
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
            "sharpe_proxy": round(sharpe_proxy, 3) if sharpe_proxy is not None else None,
            "avg_trade_r": round(avg_trade_r, 4),
            "avg_holding_minutes": round(avg_holding_min, 1),
            "max_consecutive_losses": d["max_consecutive_losses"],
            "max_consecutive_wins": d["max_consecutive_wins"],
            "equity_r": round(d["equity_r"], 3),
            "max_drawdown_r": round(d["max_drawdown_r"], 3),
            "recovery_factor": round(recovery_factor, 3) if recovery_factor is not None else None,
            "last_updated": d["last_updated"],
        }

    def by_symbol_summary(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for sym, s in self._data["by_symbol"].items():
            n = s["trades"] or 1
            out[sym] = {
                "trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
                "win_rate": round(s["wins"] / n, 3),
                "avg_trade_r": round(s["sum_r"] / n, 4),
            }
        return out

    # -- markdown --------------------------------------------------------

    def _regenerate_markdown(self) -> None:
        s = self.summary()
        if s.get("total_trades", 0) == 0:
            return
        by_sym = self.by_symbol_summary()
        rows = "\n".join(
            f"| {sym} | {v['trades']} | {v['win_rate']:.1%} | {v['avg_trade_r']:+.3f} |"
            for sym, v in sorted(by_sym.items())
        )
        pf = f"{s['profit_factor']:.2f}" if s['profit_factor'] is not None else "n/a (no losses yet)"
        sharpe = f"{s['sharpe_proxy']:.2f}" if s['sharpe_proxy'] is not None else "n/a"
        recovery = f"{s['recovery_factor']:.2f}" if s['recovery_factor'] is not None else "n/a"
        md = f"""# Oracle Trading Benchmark

> Auto-generated from real (paper/demo) trade outcomes. Do not edit manually.
> Last updated: {s['last_updated']}

## Summary

| Metric | Value |
|--------|-------|
| Total Trades | {s['total_trades']} |
| Wins / Losses | {s['wins']} / {s['losses']} |
| Win Rate | {s['win_rate']:.1%} |
| Profit Factor | {pf} |
| Sharpe (per-trade proxy) | {sharpe} |
| Average Trade | {s['avg_trade_r']:+.3f}R |
| Average Holding Time | {s['avg_holding_minutes']:.1f} min |
| Max Consecutive Losses | {s['max_consecutive_losses']} |
| Max Consecutive Wins | {s['max_consecutive_wins']} |
| Equity (cumulative) | {s['equity_r']:+.3f}R |
| Max Drawdown | {s['max_drawdown_r']:.3f}R |
| Recovery Factor | {recovery} |

## By Symbol

| Symbol | Trades | Win Rate | Avg Trade |
|--------|--------|----------|-----------|
{rows}
"""
        _atomic_write(self._markdown_path, md)