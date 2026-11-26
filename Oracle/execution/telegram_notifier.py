"""Telegram delivery and recent-signal tracking for Oracle's signal-only mode."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

log = logging.getLogger("oracle.telegram")
_LEDGER_LOCK = threading.Lock()


def _record_delivery(event_key: str, text: str, status: str, error: str = "") -> None:
    """Persist Telegram delivery state without credentials for the Oracle dashboard."""
    try:
        root = Path(__file__).resolve().parents[2]
        path = root / "Oracle" / "memory" / "logs" / "telegram_signal_ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp_unix": time.time(),
            "event_key": str(event_key)[:180],
            "status": status,
            "message": str(text)[:4000],
        }
        if error:
            row["error"] = error[:120]
        with _LEDGER_LOCK, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        log.warning("Could not record Telegram delivery state")


def signal_only_enabled(override_env: Optional[str] = None) -> bool:
    """Fail closed: use a broker-specific setting, then the shared fallback."""
    raw = os.getenv(override_env, "").strip() if override_env else ""
    if not raw:
        raw = os.getenv("ORACLE_SIGNAL_ONLY", "true")
    raw = raw.strip().lower()
    return raw not in {"0", "false", "no", "off"}


class TelegramNotifier:
    """Small Bot API client with per-event deduplication and chat rate limiting."""

    def __init__(self) -> None:
        self._token = os.getenv("ORACLE_TELEGRAM_BOT_TOKEN", "").strip()
        self._chat_id = os.getenv("ORACLE_TELEGRAM_CHAT_ID", "").strip()
        try:
            self._dedupe_sec = max(60.0, float(os.getenv("ORACLE_TELEGRAM_DEDUPE_SEC", "1800")))
        except ValueError:
            self._dedupe_sec = 1800.0
        self._sent_at: Dict[str, float] = {}
        self._last_send = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str, event_key: str) -> bool:
        """Send once per event window; never log the token or request URL."""
        if not self.enabled:
            _record_delivery(event_key, text, "not_configured")
            return False
        now = time.monotonic()
        with self._lock:
            self._sent_at = {key: sent for key, sent in self._sent_at.items()
                             if now - sent < self._dedupe_sec}
            last = self._sent_at.get(event_key)
            if last is not None and now - last < self._dedupe_sec:
                _record_delivery(event_key, text, "deduplicated")
                return False

            # Telegram advises no more than one message per second in a chat.
            wait = 1.05 - (now - self._last_send)
            if wait > 0:
                time.sleep(wait)

            payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode("utf-8")
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self._last_send = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not result.get("ok"):
                    log.warning("Telegram rejected notification (HTTP %s)",
                                result.get("error_code", "unknown"))
                    _record_delivery(event_key, text, "rejected", str(result.get("error_code", "unknown")))
                    return False
            except urllib.error.HTTPError as exc:
                # Do not print exception text: urllib includes the tokenized URL.
                log.warning("Telegram delivery failed (HTTP %s)", exc.code)
                _record_delivery(event_key, text, "failed", f"HTTP {exc.code}")
                return False
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                log.warning("Telegram delivery failed (%s)", type(exc).__name__)
                _record_delivery(event_key, text, "failed", type(exc).__name__)
                return False

            self._sent_at[event_key] = time.monotonic()
            _record_delivery(event_key, text, "sent")
            return True


class RecentSignalCache:
    """Match a recent Oracle recommendation to a manual MT5 position by symbol and side."""

    def __init__(self) -> None:
        try:
            self._ttl = max(60.0, float(os.getenv("ORACLE_SIGNAL_MATCH_TTL_SEC", "21600")))
        except ValueError:
            self._ttl = 21600.0
        self._signals: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def remember(self, symbol: str, context: Dict[str, Any]) -> None:
        with self._lock:
            self._signals[symbol.upper()] = {**context, "sent_at": time.time()}

    def match(self, symbol: str, direction: str, opened_at: Optional[float]) -> Optional[Dict[str, Any]]:
        if opened_at is None:
            return None
        now = time.time()
        key = symbol.upper()
        with self._lock:
            context = self._signals.get(key)
            if not context:
                return None
            sent_at = float(context.get("sent_at", 0.0))
            if now - sent_at > self._ttl:
                self._signals.pop(key, None)
                return None
            if opened_at < sent_at - 120 or opened_at > sent_at + self._ttl:
                return None
            expected = str(context.get("direction", "")).lower()
            actual = "long" if direction.lower() in {"buy", "long"} else "short"
            if expected != actual:
                return None
            self._signals.pop(key, None)
            return dict(context)


def _price(value: Any) -> str:
    try:
        return f"{float(value):.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "n/a"


def format_entry_alert(symbol: str, result: Dict[str, Any]) -> str:
    plan = result.get("plan") or {}
    signal = result.get("signal") or {}
    direction = str(plan.get("direction", "unknown")).upper()
    raw_conf = signal.get("confidence", 0.0)
    effective_conf = result.get("effective_confidence", raw_conf)
    floor = plan.get("confidence_floor", "n/a")
    lines = [
        "ORACLE APPROVED SIGNAL — MANUAL ACTION ONLY",
        f"{symbol.upper()} | {direction}",
        f"Entry reference: {_price(plan.get('entry'))}",
        f"Stop loss: {_price(plan.get('stop'))}",
        f"Take profit: {_price(plan.get('target'))}",
        f"Confidence: {float(effective_conf):.1%} effective ({float(raw_conf):.1%} raw; floor {floor})",
        f"Risk budget: {plan.get('risk_amount', 'n/a')} | Reward/risk: {plan.get('reward_risk', 'n/a')}",
    ]
    if result.get("scout_enabled", True):
        scout_score = result.get("scout_score")
        learned_floor = result.get("learned_conviction_floor")
        if scout_score is not None or learned_floor is not None:
            lines.append(
                f"Scout: score {float(scout_score or 0.0):.3f} | "
                f"learned floor {float(learned_floor or 0.0):.3f}"
            )
    else:
        lines.append("Selection: Scout disabled; Oracle confidence floor and risk checks passed.")
    streams = signal.get("streams") or {}
    technical = streams.get("technical") or {}
    reasons = technical.get("reasons") or []
    if reasons:
        lines.append("Signal evidence: " + "; ".join(str(reason) for reason in reasons[:4]))
    if result.get("news_impact") not in (None, "none"):
        lines.append(f"News: {result.get('news_impact')} — {result.get('news_reason', '')}".rstrip())
    if result.get("social_risk") not in (None, "none"):
        lines.append(f"Social risk: {result.get('social_risk')} — {result.get('social_reason', '')}".rstrip())
    lines.append("Oracle will not place this trade. If you enter manually, use these SL/TP levels for V1 monitoring.")
    return "\n".join(lines)


def format_execution_alert(symbol: str, proposal: Dict[str, Any],
                           execution: Dict[str, Any], broker: str) -> str:
    """Format a broker-confirmed entry; call only after a confirmed fill."""
    plan = proposal.get("plan") or {}
    signal = proposal.get("signal") or {}
    broker_symbol = str(plan.get("broker_symbol") or execution.get("symbol") or symbol).upper()
    fill_price = execution.get("price", execution.get("fill_price", plan.get("entry")))
    volume = execution.get("volume", execution.get("filled_volume", plan.get("size")))
    order_id = execution.get("order") or execution.get("ticket") or execution.get("deal_id") or "n/a"
    effective_confidence = proposal.get("effective_confidence", signal.get("confidence", 0.0))
    raw_confidence = signal.get("confidence", 0.0)
    lines = [
        f"ORACLE TRADE OPENED — {str(broker).upper()} FILL CONFIRMED",
        f"{symbol.upper()} ({broker_symbol}) | {str(plan.get('direction', 'unknown')).upper()}",
        f"Fill: {_price(fill_price)} | Size: {volume if volume is not None else 'n/a'}",
        f"Order/ticket: {order_id}",
        f"Stop loss: {_price(plan.get('stop'))} | Take profit: {_price(plan.get('target'))}",
        f"Confidence: {float(effective_confidence):.1%} effective ({float(raw_confidence):.1%} raw)",
    ]
    if proposal.get("scout_enabled", True):
        scout_score = proposal.get("scout_score")
        learned_floor = proposal.get("learned_conviction_floor")
        if scout_score is not None or learned_floor is not None:
            lines.append(
                f"Scout: score {float(scout_score or 0.0):.3f} | "
                f"learned floor {float(learned_floor or 0.0):.3f}"
            )
    lines.append("Broker confirmed the fill; manage the open position through the broker account.")
    return "\n".join(lines)


def format_position_alert(symbol: str, ticket: Any, action: str,
                          decision: Any, stop: Any, target: Any) -> str:
    action_text = "CLOSE manually" if action == "close" else "TIGHTEN STOP manually"
    lines = [
        f"ORACLE POSITION ALERT — {action_text}",
        f"{symbol.upper()} | ticket {ticket}",
        f"Reason: {decision.reason}",
        f"Current confidence: {decision.current_confidence:.1%} | P/L: {decision.unrealized_r:.2f}R",
        f"Oracle stop baseline: {_price(stop)} | Target baseline: {_price(target)}",
    ]
    if action != "close":
        lines.append(f"Suggested new stop: {_price(decision.new_stop)}")
    lines.append("Recommendation only; Oracle has not modified or closed the position.")
    return "\n".join(lines)
