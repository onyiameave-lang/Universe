"""
Oracle Trade Journal
====================
Roadmap Phase 3 ("Build Better Intelligence") groundwork: Trade
Explainability. Every trade decision -- entered, held, or rejected --
already has real reasons computed (risk.evaluate()'s rejection list,
News/Social Intelligence's level+reason, entry_streams' per-source
breakdown) but none of it was being kept anywhere reviewable; it only
ever appeared in transient log lines. This module makes that reviewable:
every decision gets written to Chronicle as a human-readable episodic
memory, and entries get linked back to their eventual outcome (win/loss)
once the trade closes.

Design
------
Chronicle's memory store is append-only (no in-place update), so an
entry and its later outcome are two SEPARATE memory records, linked by a
shared `journal_id` tag rather than editing the original. Chronicle's own
autolink/search can then reconstruct the pair (or a caller can
`memory.search` by the journal_id tag directly).

Matches the existing dual-interface Chronicle-write convention already
used in oracle_agent.py's `_preserve()` (a `.store()` method if present,
else `.act("memory.store", ...)`), rather than inventing a new one.

Fails silently (logs a debug line, never raises) if no Chronicle client
is available -- journaling is an observability aid, not something that
should ever be able to block a real trade decision.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("oracle.trade_journal")


class TradeJournal:
    def __init__(self, chronicle_client=None):
        self.chronicle = chronicle_client

    # -- internal: matches oracle_agent.py's _preserve() convention exactly --

    def _write(self, content: str, summary: str, tags: List[str]) -> None:
        if self.chronicle is None:
            return
        try:
            store_fn = getattr(self.chronicle, "store", None)
            if callable(store_fn):
                store_fn(content=content, memory_type="episodic", domain="trade_journal",
                          summary=summary, tags=tags, source="trade_journal")
            elif hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {
                    "content": content, "pillar": "episodic", "domain": "trade_journal",
                    "summary": summary, "tags": tags, "_sender": "trade_journal",
                })
        except Exception as exc:
            log.debug("trade journal write failed: %s", exc)

    @staticmethod
    def _new_journal_id(symbol: str) -> str:
        return f"{symbol}-{int(time.time())}"

    # -- public API --------------------------------------------------------

    def log_entry(self, symbol: str, direction: str, confidence: float, regime: str,
                  news_level: str, news_reason: str, social_level: str, social_reason: str,
                  entry_streams: Optional[Dict[str, Any]], size: float) -> str:
        """Call once, right when a trade actually opens. Returns a
        journal_id -- keep it (e.g. on the Position) so log_outcome() can
        link back to this exact entry later."""
        journal_id = self._new_journal_id(symbol)
        news_line = f"News: {news_level} — {news_reason}" if news_level != "none" \
            else "News: nothing significant at entry"
        social_line = f"Social: {social_level} — {social_reason}" if social_level != "none" \
            else "Social: no notable coordinated activity at entry"
        streams_line = ", ".join(f"{k}={v}" for k, v in (entry_streams or {}).items()) or "n/a"
        content = (
            f"ENTERED {direction.upper()} {symbol} | confidence={confidence:.2f} "
            f"regime={regime} size={size}\n{news_line}\n{social_line}\n"
            f"Signal breakdown: {streams_line}"
        )
        self._write(content, f"Entered {direction} {symbol} at {confidence:.0%} confidence",
                    ["trade_journal", "entry", symbol, journal_id])
        return journal_id

    def log_hold(self, symbol: str, reason: str, confidence: float, regime: str) -> None:
        """Call from the Continuous Trade Manager's poll loop when a HOLD
        decision fires. Callers should dedup (only call when `reason`
        differs from the last one journaled for this position) -- holds
        fire every poll cycle, and journaling an identical reason every
        15 seconds would drown out everything else."""
        content = f"HELD {symbol} | confidence={confidence:.2f} regime={regime}\nReason: {reason}"
        self._write(content, f"Held {symbol}: {reason[:80]}",
                    ["trade_journal", "hold", symbol])

    def log_rejection(self, symbol: str, direction: str, confidence: float,
                       reasons: List[str]) -> None:
        """Call when risk.evaluate() rejects a proposed trade (approved=False)."""
        reasons_text = "; ".join(reasons) if reasons else "no reason given"
        content = (f"REJECTED {direction.upper()} {symbol} | confidence={confidence:.2f}\n"
                   f"Reasons: {reasons_text}")
        self._write(content, f"Rejected {direction} {symbol}: {reasons_text[:80]}",
                    ["trade_journal", "rejection", symbol])

    def log_outcome(self, journal_id: str, symbol: str, won: bool, pnl_r: float,
                     exit_reason: str, lesson: str = "") -> None:
        """Call once a trade closes (from TradeLearningEngine.record_close()).
        Links back to the original log_entry() call via journal_id -- a
        SEPARATE memory record (Chronicle is append-only), correlated by
        the shared journal_id tag rather than edited in place."""
        result = "WIN" if won else "LOSS"
        content = (f"OUTCOME for {journal_id}: {result} {pnl_r:+.2f}R on {symbol}\n"
                   f"Exit reason: {exit_reason}")
        if lesson:
            content += f"\nLesson: {lesson}"
        self._write(content, f"{result} {pnl_r:+.2f}R on {symbol} ({journal_id})",
                    ["trade_journal", "outcome", symbol, journal_id, result.lower()])