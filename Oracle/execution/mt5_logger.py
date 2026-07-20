"""
Oracle/tools/mt5_logger.py
==========================
CSV trade logger + session summary for Oracle MT5 demo trading.

Drop this file into:  Universal_AI/Oracle/tools/mt5_logger.py

Produces two CSV files (paths from mt5_config.py):
  logs/mt5_demo_trades.csv    — one row per trade signal (filled or rejected)
  logs/mt5_demo_sessions.csv  — one row per completed session summary
"""
from __future__ import annotations

import csv
import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
#  Trade log schema
# ─────────────────────────────────────────────────────────────────────────────
TRADE_FIELDS = [
    "timestamp",        # ISO-8601 UTC
    "session_id",       # YYYYMMDD_HHMMSS of session start
    "cycle",            # cycle number within session
    "symbol",           # Oracle canonical name (e.g. EURUSD)
    "broker_symbol",    # broker name (e.g. EURUSDm)
    "direction",        # long / short
    "confidence",       # float 0-1
    "regime",           # trending / ranging / volatile / unknown
    "size",             # lots
    "price",            # fill price (0 if paper/rejected)
    "stop",             # stop-loss price
    "target",           # take-profit price
    "reward_risk",      # R:R ratio
    "outcome",          # filled / rejected / paper / blocked / error / timeout / hold / unmapped
    "retcode",          # MT5 retcode (empty if paper/rejected)
    "reason",           # rejection / error reason (empty if filled)
    "account_type",     # demo / real / paper
    "equity_before",    # account equity before this trade
    "note",             # free-text note
]

SESSION_FIELDS = [
    "session_id",
    "start_time",
    "end_time",
    "duration_min",
    "symbols_traded",
    "cycles_completed",
    "trades_filled",
    "trades_rejected",
    "trades_hold",
    "trades_error",
    "trades_timeout",
    "trades_unmapped",
    "start_equity",
    "end_equity",
    "pnl",
    "pnl_pct",
    "kill_switch_fired",
    "paper_mode",
    "note",
]


class TradeLogger:
    """
    Append-only CSV logger for Oracle MT5 demo trades.

    Usage:
        logger = TradeLogger(trade_log="logs/mt5_demo_trades.csv",
                             session_log="logs/mt5_demo_sessions.csv")
        logger.start_session(start_equity=10000.0)
        logger.log_trade(cycle=1, symbol="EURUSD", broker_symbol="EURUSD",
                         direction="long", confidence=0.72, ...)
        logger.end_session(end_equity=10150.0, cycles=3)
    """

    def __init__(self, trade_log: str, session_log: str, oracle_root: Optional[Path] = None):
        root = oracle_root or Path(__file__).resolve().parents[1]
        self._trade_path   = root / trade_log
        self._session_path = root / session_log
        self._trade_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_path.parent.mkdir(parents=True, exist_ok=True)

        self._session_id:    str   = ""
        self._start_time:    Optional[datetime.datetime] = None
        self._start_equity:  float = 0.0
        self._paper:         bool  = True
        self._trade_rows:    List[Dict] = []

        # Write headers if files are new
        self._ensure_header(self._trade_path,   TRADE_FIELDS)
        self._ensure_header(self._session_path, SESSION_FIELDS)

    # ── internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _ensure_header(path: Path, fields: List[str]) -> None:
        if not path.exists() or path.stat().st_size == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()

    @staticmethod
    def _now_utc() -> datetime.datetime:
        return datetime.datetime.utcnow()

    @staticmethod
    def _iso(dt: datetime.datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── session lifecycle ─────────────────────────────────────────────────────

    def start_session(self, start_equity: float = 0.0, paper: bool = True) -> str:
        """Call once at the start of a trading session. Returns session_id."""
        self._start_time   = self._now_utc()
        self._session_id   = self._start_time.strftime("%Y%m%d_%H%M%S")
        self._start_equity = start_equity
        self._paper        = paper
        self._trade_rows   = []
        print(f"[Logger] Session {self._session_id} started | "
              f"equity={start_equity:.2f} | paper={paper}")
        return self._session_id

    def end_session(
        self,
        end_equity: float = 0.0,
        cycles: int = 0,
        kill_switch: bool = False,
        note: str = "",
    ) -> Dict[str, Any]:
        """Call at session end. Writes summary row. Returns summary dict."""
        end_time = self._now_utc()
        duration = (end_time - self._start_time).total_seconds() / 60 if self._start_time else 0

        # Tally outcomes from logged trade rows
        counts: Dict[str, int] = {}
        symbols_traded = set()
        for row in self._trade_rows:
            outcome = row.get("outcome", "")
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "filled":
                symbols_traded.add(row.get("symbol", ""))

        pnl     = end_equity - self._start_equity
        pnl_pct = (pnl / self._start_equity * 100) if self._start_equity else 0.0

        summary = {
            "session_id":       self._session_id,
            "start_time":       self._iso(self._start_time) if self._start_time else "",
            "end_time":         self._iso(end_time),
            "duration_min":     f"{duration:.1f}",
            "symbols_traded":   len(symbols_traded),
            "cycles_completed": cycles,
            "trades_filled":    counts.get("filled",  0),
            "trades_rejected":  counts.get("rejected", 0) + counts.get("blocked", 0),
            "trades_hold":      counts.get("hold",    0),
            "trades_error":     counts.get("error",   0),
            "trades_timeout":   counts.get("timeout", 0),
            "trades_unmapped":  counts.get("unmapped", 0),
            "start_equity":     f"{self._start_equity:.2f}",
            "end_equity":       f"{end_equity:.2f}",
            "pnl":              f"{pnl:.2f}",
            "pnl_pct":          f"{pnl_pct:.3f}",
            "kill_switch_fired": kill_switch,
            "paper_mode":       self._paper,
            "note":             note,
        }

        with open(self._session_path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=SESSION_FIELDS).writerow(summary)

        print(f"\n[Logger] Session {self._session_id} ended | "
              f"cycles={cycles} | filled={summary['trades_filled']} | "
              f"PnL={pnl:+.2f} ({pnl_pct:+.2f}%) | "
              f"kill_switch={kill_switch}")
        return summary

    # ── trade logging ─────────────────────────────────────────────────────────

    def log_trade(
        self,
        *,
        cycle: int,
        symbol: str,
        broker_symbol: str,
        direction: str,
        confidence: float,
        regime: str,
        size: float,
        price: float,
        stop: float,
        target: float,
        reward_risk: float,
        outcome: str,
        retcode: Any = "",
        reason: str = "",
        account_type: str = "demo",
        equity_before: float = 0.0,
        note: str = "",
    ) -> None:
        """Append one trade row to the CSV log."""
        row = {
            "timestamp":     self._iso(self._now_utc()),
            "session_id":    self._session_id,
            "cycle":         cycle,
            "symbol":        symbol,
            "broker_symbol": broker_symbol,
            "direction":     direction,
            "confidence":    f"{confidence:.4f}",
            "regime":        regime,
            "size":          f"{size:.4f}",
            "price":         f"{price:.5f}",
            "stop":          f"{stop:.5f}",
            "target":        f"{target:.5f}",
            "reward_risk":   f"{reward_risk:.2f}",
            "outcome":       outcome,
            "retcode":       retcode,
            "reason":        reason,
            "account_type":  account_type,
            "equity_before": f"{equity_before:.2f}",
            "note":          note,
        }
        self._trade_rows.append(row)
        with open(self._trade_path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(row)

    def log_signal_only(
        self,
        *,
        cycle: int,
        symbol: str,
        broker_symbol: str,
        outcome: str,
        direction: str = "",
        confidence: float = 0.0,
        regime: str = "",
        reason: str = "",
        note: str = "",
    ) -> None:
        """Log a non-trade signal event (hold, reject, error, timeout, unmapped)."""
        self.log_trade(
            cycle=cycle, symbol=symbol, broker_symbol=broker_symbol,
            direction=direction, confidence=confidence, regime=regime,
            size=0.0, price=0.0, stop=0.0, target=0.0, reward_risk=0.0,
            outcome=outcome, reason=reason, note=note,
        )

    # ── convenience: print trade log path ────────────────────────────────────

    def paths(self) -> Dict[str, str]:
        return {
            "trades":  str(self._trade_path),
            "sessions": str(self._session_path),
        }
