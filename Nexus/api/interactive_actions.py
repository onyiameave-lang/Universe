"""
Nexus Interactive Actions & Live Chart/Feed/Training Engine.

Provides full front-end to back-end interactive execution for:
1. Oracle:
   - Read authenticated MT5/cTrader account snapshots and verified positions
   - Read persisted OHLC history with source and freshness metadata
   - Read actual Telegram delivery records
2. Sentinel & Pulse:
   - Refresh / scan live news & social posts (`execute_feed_refresh`)
   - Expose only source-provided news and social fields; label missing metadata as unavailable
3. Forge:
   - Plain-English humanizer for every Forge training endpoint (`discover`, `shadow/run`, `retrain`, `transition`, `rollback`, `pilot/run`, `cost-profile`, `sync`, `flush`)
4. Chronicle, Aegis & Genesis:
   - Interactive backend actions with explicit recorded, blocked, or unavailable outcomes
"""
from __future__ import annotations

import csv
import json
import math
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
RESEARCH_DIR = ROOT_DIR / "Chronicle" / "memory" / "research"
MTF_DIR = RESEARCH_DIR / "mtf_market_data"
FEEDS_CACHE_PATH = RESEARCH_DIR / "ecosystem_live_feeds.json"
_MTF_CACHE_LOCK = threading.Lock()
_MTF_CACHE_PATH: Optional[Path] = None
_MTF_CACHE_MTIME_NS: Optional[int] = None
_MTF_CACHE_DATA: Dict[str, Any] = {}
_FEED_REFRESH_LOCK = threading.Lock()
_FEED_COLLECTION_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def enrich_sentinel_news_items(news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expose source-provided news fields without inventing article text, metadata, or market claims."""
    enriched: List[Dict[str, Any]] = []
    for idx, item in enumerate(news_list):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["headline"] = str(item.get("headline") or item.get("title") or "").strip()
        row["outlet"] = str(item.get("outlet") or item.get("source") or "Source unavailable").strip()
        row["category"] = str(item.get("category") or "Category unavailable").strip()
        row["summary"] = str(item.get("summary") or item.get("description") or "Summary unavailable").strip()
        row["time"] = str(item.get("time") or item.get("published_at") or "Timestamp unavailable")
        row["url"] = str(item.get("url") or "")
        row["image_url"] = str(item.get("image_url") or item.get("image") or "")
        row["full_article_paragraphs"] = [row["summary"]] if row["summary"] != "Summary unavailable" else []
        row["optional_trade_impact"] = "No verified market-impact assessment is attached to this news item. Ask Sentinel or Atlas to analyze the sourced evidence."
        row["market_impact_plain"] = row["optional_trade_impact"]
        enriched.append(row)
    return enriched


def enrich_pulse_social_items(posts_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expose recorded social-post fields; never synthesize engagement, authors, or discussion text."""
    enriched: List[Dict[str, Any]] = []
    for idx, p in enumerate(posts_list):
        if not isinstance(p, dict):
            continue
        platform = str(p.get("platform") or "Source unavailable").strip()
        handle = str(p.get("handle") or p.get("author") or p.get("display_name") or "Author unavailable").strip()
        display_name = str(p.get("display_name") or p.get("author") or "Author unavailable").strip()
        topic = str(p.get("topic") or "Topic unavailable").strip()
        post_text = str(p.get("post") or p.get("headline") or p.get("text") or p.get("title") or "").strip()
        row = dict(p)
        row.update({"platform": platform, "handle": handle, "author": handle,
                    "display_name": display_name, "topic": topic, "post": post_text,
                    "headline": str(p.get("headline") or post_text),
                    "time": str(p.get("time") or p.get("published_at") or "Timestamp unavailable"),
                    "url": str(p.get("url") or ""),
                    "image_url": str(p.get("image_url") or p.get("image") or ""),
                    "engagement": str(p.get("engagement") or "Unavailable · source did not provide engagement counts"),
                    "optional_trade_impact": "No verified market-impact assessment is attached to this post. Ask Pulse or Atlas to analyze the sourced evidence."})
        row["market_impact_plain"] = row["optional_trade_impact"]
        row["full_article_paragraphs"] = [post_text] if post_text else []
        enriched.append(row)
    return enriched


def _execute_feed_refresh(agent_id: str = "all", agent_registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Refresh using the running Sentinel/Pulse collector instances, then cache the observed payload."""
    data: Dict[str, Any] = {}
    refreshed = False
    refresh_error = None
    source_results: Dict[str, Any] = {}
    if agent_registry is not None:
        selected = {"all": ("sentinel", "pulse"), "sentinel": ("sentinel",), "pulse": ("pulse",)}.get(str(agent_id).lower())
        if selected is None:
            return {"status": "blocked", "agent": agent_id, "human_summary": "Choose Sentinel, Pulse, or both."}
        try:
            cached = json.loads(FEEDS_CACHE_PATH.read_text(encoding="utf-8"))
            data = cached if isinstance(cached, dict) else {}
        except (OSError, ValueError):
            data = {}
        collected_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for name in selected:
            agent = agent_registry.get(name)
            engine = getattr(agent, "engine", None) if agent is not None else None
            gather = getattr(engine, "gather", None)
            if not callable(gather):
                source_results[name] = {"status": "unavailable", "message": f"{name.title()} collector is not initialized."}
                continue
            try:
                if name == "sentinel":
                    raw = gather(consult_chronicle=False, include_deep_analysis=False, force_refresh=True)
                    field = "articles"
                else:
                    raw = gather(allow_external_enrichment=False)
                    field = "posts"
                rows = raw.get(field) if isinstance(raw, dict) else None
                rows = rows if isinstance(rows, list) else []
                block_key = "world_news" if name == "sentinel" else "social_posts"
                data[name] = {
                    **(data.get(name) if isinstance(data.get(name), dict) else {}),
                    block_key: rows,
                    "status": "refreshed" if rows else "completed_empty",
                    "updated_at": collected_at,
                    "source_status": raw.get("source_status", {}) if isinstance(raw, dict) else {},
                }
                source_results[name] = {
                    "status": "refreshed" if rows else "completed_empty",
                    "items": rows,
                    "source_status": raw.get("source_status", {}) if isinstance(raw, dict) else {},
                    "count": len(rows),
                }
            except Exception as exc:
                source_results[name] = {"status": "error", "message": f"{type(exc).__name__}: {exc}", "items": [], "source_status": {}}
        for name in {"sentinel", "pulse"} - set(selected):
            block = data.get(name) if isinstance(data.get(name), dict) else {}
            block_key = "world_news" if name == "sentinel" else "social_posts"
            source_results[name] = {"status": "cached_unverified" if block.get(block_key) else "unavailable",
                                    "count": len(block.get(block_key) or []), "items": block.get(block_key) or [],
                                    "updated_at": block.get("updated_at")}
        refreshed = any(source_results.get(name, {}).get("status") in {"refreshed", "completed_empty"} for name in selected)
        all_requested_complete = all(source_results.get(name, {}).get("status") in {"refreshed", "completed_empty"} for name in selected)
        data["updated_at"] = collected_at if all_requested_complete and set(selected) == {"sentinel", "pulse"} else data.get("updated_at")
        refresh_errors = [f"{name}: {result.get('message')}" for name, result in source_results.items() if result.get("status") == "error"]
        refresh_error = "; ".join(refresh_errors) or None
        if refreshed:
            try:
                FEEDS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp_path = FEEDS_CACHE_PATH.with_suffix(FEEDS_CACHE_PATH.suffix + ".tmp")
                with _FEED_REFRESH_LOCK:
                    temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    temp_path.replace(FEEDS_CACHE_PATH)
            except OSError as exc:
                refresh_error = f"cache write failed: {type(exc).__name__}"
    else:
        refresh_error = "running Sentinel/Pulse agents were not provided to the feed refresh action"
        if FEEDS_CACHE_PATH.exists():
            try:
                data = json.loads(FEEDS_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
    raw_news = (data.get("sentinel") or {}).get("world_news") or data.get("sentinel_news") or []
    raw_social = (data.get("pulse") or {}).get("social_posts") or data.get("pulse_social") or []
    news = enrich_sentinel_news_items(raw_news)
    social = enrich_pulse_social_items(raw_social)
    completed_selected = [name for name in selected] if agent_registry is not None else []
    completed_count = sum(source_results.get(name, {}).get("status") in {"refreshed", "completed_empty"}
                          for name in completed_selected)
    status = (
        "unavailable" if not refreshed and not data else
        "partial" if completed_selected and completed_count < len(completed_selected) and completed_count > 0 else
        "refreshed" if refreshed else "cached_unverified"
    )
    return {
        "status": status,
        "agent": agent_id,
        "updated_at": data.get("updated_at") or data.get("fetched_at") or None,
        "refresh_error": refresh_error,
        "news_count": len(news),
        "social_count": len(social),
        "source_results": source_results,
        "sentinel_news": news[:18],
        "pulse_social": social[:18],
        "human_summary": (
            "Collector refresh completed. Per-agent freshness and source status are included; cached rows are not labeled as refreshed."
            if status == "refreshed" else
            "Only some requested collectors completed. Per-agent source status shows what refreshed and what remained unavailable."
            if status == "partial" else
            f"Live feed refresh is unavailable{': ' + refresh_error if refresh_error else ''}. "
            + (f"Showing {len(news)} news and {len(social)} social items from a cache; freshness is not verified." if data else "No feed items are available.")
        ),
    }


def execute_feed_refresh(agent_id: str = "all", agent_registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Prevent concurrent UI scans from duplicating collector work."""
    if not _FEED_COLLECTION_LOCK.acquire(blocking=False):
        return {"status": "busy", "agent": agent_id,
                "human_summary": "A Sentinel/Pulse refresh is already running. No duplicate collection was started."}
    try:
        return _execute_feed_refresh(agent_id, agent_registry)
    finally:
        _FEED_COLLECTION_LOCK.release()


# ──────────────────────────────────────────────────────────────────────
# ORACLE READ-ONLY SNAPSHOT READERS
# ──────────────────────────────────────────────────────────────────────

ORACLE_MEMORY_DIR = ROOT_DIR / "Oracle" / "memory"
BROKER_SNAPSHOT_MAX_AGE_SECONDS = 90
BROKER_HISTORY_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MARKET_DATA_FRESH_MAX_AGE_SECONDS = 36 * 60 * 60


def _read_broker_snapshot(broker: str) -> Dict[str, Any]:
    path = ORACLE_MEMORY_DIR / f"{broker}_dashboard_snapshot.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _snapshot_is_fresh(snapshot: Dict[str, Any], field: str, max_age: int = BROKER_SNAPSHOT_MAX_AGE_SECONDS) -> bool:
    stamp = _finite_number(snapshot.get(field))
    if stamp is None:
        return False
    age = time.time() - stamp
    return 0 <= age <= max_age


def _read_mtf_dataset(path: Path) -> Dict[str, Any]:
    global _MTF_CACHE_PATH, _MTF_CACHE_MTIME_NS, _MTF_CACHE_DATA
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _MTF_CACHE_LOCK:
        if path == _MTF_CACHE_PATH and mtime == _MTF_CACHE_MTIME_NS:
            return _MTF_CACHE_DATA
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        _MTF_CACHE_PATH, _MTF_CACHE_MTIME_NS, _MTF_CACHE_DATA = path, mtime, payload
        return payload


def _aggregate_m15_to_m45(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate only complete, UTC-aligned 3-candle windows; never fill missing bars."""
    buckets: Dict[int, List[tuple[int, Dict[str, Any]]]] = {}
    for row in rows:
        stamp = _finite_number(row.get("unix_sec"))
        if stamp is None and row.get("ts"):
            try:
                parsed = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                stamp = parsed.timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
        if stamp is None:
            continue
        start = int(stamp)
        bucket = (start // 2700) * 2700
        buckets.setdefault(bucket, []).append((start, row))
    aggregated: List[Dict[str, Any]] = []
    for bucket, entries in sorted(buckets.items()):
        entries.sort(key=lambda item: item[0])
        if [stamp for stamp, _ in entries] != [bucket, bucket + 900, bucket + 1800]:
            continue
        source_bars = [row for _, row in entries]
        try:
            opens = [_finite_number(row.get("open")) for row in source_bars]
            highs = [_finite_number(row.get("high")) for row in source_bars]
            lows = [_finite_number(row.get("low")) for row in source_bars]
            closes = [_finite_number(row.get("close")) for row in source_bars]
            if any(value is None for values in (opens, highs, lows, closes) for value in values):
                continue
            aggregated.append({
                "unix_sec": bucket,
                "ts": datetime.fromtimestamp(bucket, timezone.utc).isoformat().replace("+00:00", "Z"),
                "open": opens[0], "high": max(highs), "low": min(lows), "close": closes[-1],
                "volume": sum(_finite_number(row.get("volume")) or 0.0 for row in source_bars),
                "session": source_bars[-1].get("session"),
            })
        except (TypeError, ValueError, OverflowError):
            continue
    return aggregated


def _verified_positions(snapshot: Dict[str, Any], broker: str, *, allow_stale: bool = False) -> List[Dict[str, Any]]:
    if (snapshot.get("source") not in {"authenticated_ctrader_open_api", "authenticated_mt5_terminal"}
            or snapshot.get("account_type") != "demo"):
        return []
    position_stamp = _finite_number(snapshot.get("positions_updated_at_unix"))
    if position_stamp is None:
        return []
    fresh = _snapshot_is_fresh(snapshot, "positions_updated_at_unix")
    if not fresh and (not allow_stale or time.time() - position_stamp > BROKER_HISTORY_MAX_AGE_SECONDS):
        return []
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        return []
    rows: List[Dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, dict) or not position.get("symbol"):
            continue
        ticket = position.get("ticket", position.get("position_id", position.get("id")))
        direction = str(position.get("direction") or position.get("side") or "").upper()
        direction = {"BUY": "BUY", "SELL": "SELL", "LONG": "BUY", "SHORT": "SELL"}.get(direction, "UNKNOWN")
        pnl = position.get("floating_pnl")
        if broker == "mt5" and pnl is None:
            pnl = position.get("profit")
        # Some cTrader snapshots have historically written a synthetic zero when no
        # mark-to-market value was returned. Only expose P/L when the broker supplied it.
        rows.append({
            "trade_id": str(ticket) if ticket is not None else "",
            "broker": broker,
            "broker_badge": "MetaTrader 5 (MT5)" if broker == "mt5" else "cTrader Demo",
            "symbol": str(position["symbol"]).upper(),
            "direction": direction,
            "status": "OPEN" if fresh and snapshot.get("connected") is True else "LAST_KNOWN",
            "volume": _finite_number(position.get("volume", position.get("trade_data", {}).get("volume") if isinstance(position.get("trade_data"), dict) else None)),
            "entry": _finite_number(position.get("entry_price", position.get("price_open", position.get("entry")))),
            "current_price": _finite_number(position.get("current_price", position.get("price_current"))),
            "sl": _finite_number(position.get("stop_loss", position.get("sl"))),
            "tp": _finite_number(position.get("take_profit", position.get("tp"))),
            "floating_pnl_usd": _finite_number(pnl),
            "currency": snapshot.get("currency"),
            "snapshot_updated_at_unix": _finite_number(snapshot.get("positions_updated_at_unix")),
            "snapshot_status": "fresh" if fresh and snapshot.get("connected") is True else "stale",
            "data_source": snapshot.get("source"),
        })
    return rows


def get_live_open_trades(broker: str = "all") -> List[Dict[str, Any]]:
    selected = str(broker or "all").strip().lower()
    brokers = ("mt5", "ctrader") if selected == "all" else (selected,)
    if any(key not in {"mt5", "ctrader"} for key in brokers):
        return []
    rows: List[Dict[str, Any]] = []
    for key in brokers:
        rows.extend(_verified_positions(_read_broker_snapshot(key), key, allow_stale=True))
    return rows


def get_oracle_dashboard_data(broker: str = "all") -> Dict[str, Any]:
    """Build the Oracle dashboard only from recent authenticated broker snapshots."""
    selected = str(broker or "all").strip().lower()
    if selected not in {"all", "mt5", "ctrader"}:
        selected = "all"
    keys = ("mt5", "ctrader") if selected == "all" else (selected,)
    now = time.time()
    summaries: Dict[str, Dict[str, Any]] = {}
    closed_trades: List[Dict[str, Any]] = []
    account_freshness: List[bool] = []
    position_freshness: List[bool] = []
    history_freshness: List[bool] = []
    for key in keys:
        snap = _read_broker_snapshot(key)
        authenticated_demo = (
            snap.get("source") in {"authenticated_ctrader_open_api", "authenticated_mt5_terminal"}
            and snap.get("account_type") == "demo" and snap.get("connected") is True
            and snap.get("account_info_available") is True
        )
        account_fresh = authenticated_demo and _snapshot_is_fresh(snap, "account_updated_at_unix")
        positions_fresh = authenticated_demo and _snapshot_is_fresh(snap, "positions_updated_at_unix")
        hist_stamp = _finite_number(snap.get("history_updated_at_unix"))
        hist_age = now - hist_stamp if hist_stamp is not None else None
        history_fresh = bool(authenticated_demo and snap.get("history_status") == "complete"
                             and hist_age is not None and 0 <= hist_age <= BROKER_HISTORY_MAX_AGE_SECONDS)
        account_freshness.append(bool(account_fresh))
        position_freshness.append(bool(positions_fresh))
        history_freshness.append(bool(history_fresh))
        balance = _finite_number(snap.get("balance")) if account_fresh else None
        equity = _finite_number(snap.get("equity")) if account_fresh else None
        positions = _verified_positions(snap, key, allow_stale=True)
        history = snap.get("closed_trades") if history_fresh and isinstance(snap.get("closed_trades"), list) else []
        for row in history:
            if isinstance(row, dict):
                closed_trades.append({**row, "broker": key})
        summaries[key] = {
            "name": "MetaTrader 5 (MT5)" if key == "mt5" else "cTrader Account",
            "account_mode": "Demo" if authenticated_demo else "Unavailable",
            "connected": bool(account_fresh),
            "balance": balance,
            "equity": equity,
            "currency": snap.get("currency") if account_fresh else None,
            "open_position_count": len(positions) if positions_fresh else None,
            "last_known_open_position_count": len(positions) if positions else (0 if positions_fresh else None),
            "positions_status": "fresh" if positions_fresh else "unavailable_or_stale",
            "history_status": "complete" if history_fresh else "unavailable_or_stale",
            "source": snap.get("source") if authenticated_demo else None,
            "account_updated_at_unix": _finite_number(snap.get("account_updated_at_unix")) if account_fresh else None,
            "positions_updated_at_unix": _finite_number(snap.get("positions_updated_at_unix")) if positions_fresh else None,
            "history_updated_at_unix": hist_stamp if history_fresh else None,
            "closed_trade_count": len(history) if history_fresh else None,
        }

    positions_status = "fresh" if all(position_freshness) else ("partial" if any(position_freshness) else "unavailable_or_stale")
    history_status = "complete" if all(history_freshness) else ("partial" if any(history_freshness) else "unavailable_or_stale")
    account_status = "fresh" if all(account_freshness) else ("partial" if any(account_freshness) else "unavailable_or_stale")
    # The position ledger may retain stale rows for operator context. Only rows
    # from a fresh authenticated snapshot are current open positions and may
    # contribute to live counts or active chart overlays.
    open_rows = get_live_open_trades(selected)
    verified_open_rows = [row for row in open_rows if row.get("status") == "OPEN"]
    last_known_rows = [row for row in open_rows if row.get("status") != "OPEN"]
    history_metrics_available = history_status == "complete"
    pnl_values = [_finite_number(item.get("net_pnl")) for item in closed_trades]
    valid_pnls = [value for value in pnl_values if value is not None]
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today_rows = [item for item in closed_trades if (_finite_number(item.get("closed_at_unix")) or 0) >= today_start]
    today_pnls = [value for value in (_finite_number(item.get("net_pnl")) for item in today_rows) if value is not None]
    wins = sum(1 for value in valid_pnls if value > 0)
    portfolio_value = sum(_finite_number(summaries[k].get("equity")) or 0 for k in keys) if all(account_freshness) else None
    overview = "Authenticated demo broker snapshots are fresh." if account_status == "fresh" else "Broker data is partially available or stale; unavailable values are omitted."
    return {
        "status": "ok" if account_status != "unavailable_or_stale" or positions_status != "unavailable_or_stale" else "unavailable",
        "broker": selected,
        "active_broker": selected,
        "brokers": summaries,
        "overview": overview,
        "live_open_trades": open_rows,
        "closed_trades": closed_trades if history_status in {"complete", "partial"} else [],
        "account_status": account_status,
        "positions_status": positions_status,
        "history_status": history_status,
        "updated_at_unix": max((row.get("positions_updated_at_unix") or 0 for row in summaries.values()), default=0) or None,
        "pnl_today": sum(today_pnls) if history_metrics_available else None,
        "win_rate": (wins / len(valid_pnls) * 100.0) if history_metrics_available and valid_pnls else None,
        "open_trades": len(verified_open_rows) if positions_status != "unavailable_or_stale" else None,
        "last_known_position_count": len(last_known_rows),
        "signals_today": None,
        "portfolio_value": portfolio_value,
        "max_drawdown": None,
        "metrics": {
            "pnl_today": sum(today_pnls) if history_metrics_available else None,
            "win_rate": (wins / len(valid_pnls) * 100.0) if history_metrics_available and valid_pnls else None,
            "open_trades": len(verified_open_rows) if positions_status != "unavailable_or_stale" else None,
            "last_known_position_count": len(last_known_rows),
            "signals_today": None,
            "portfolio_value": portfolio_value,
            "max_drawdown": None,
        },
        "deltas": {},
    }


def get_oracle_chart_data(symbol: str = "EURUSD", timeframe: str = "M15", broker: str = "all") -> Dict[str, Any]:
    """Return persisted OHLC bars without filling missing timeframes or fabricating quotes."""
    sym, tf = str(symbol or "EURUSD").upper().strip(), str(timeframe or "M15").upper().strip()
    path = MTF_DIR / sym / "latest.json"
    dataset = _read_mtf_dataset(path)
    series = dataset.get("series") if isinstance(dataset.get("series"), dict) else {}
    raw_bars = series.get(tf) if isinstance(series, dict) else None
    derived_m45 = tf == "M45" and (not isinstance(raw_bars, list) or not raw_bars)
    if derived_m45:
        source_bars = series.get("M15") if isinstance(series, dict) else None
        raw_bars = _aggregate_m15_to_m45(source_bars if isinstance(source_bars, list) else [])
    raw_bars = raw_bars if isinstance(raw_bars, list) else []
    bars: List[Dict[str, Any]] = []
    for bar in raw_bars[-250:]:
        if not isinstance(bar, dict):
            continue
        values = {name: _finite_number(bar.get(name)) for name in ("open", "high", "low", "close", "volume")}
        if any(values[name] is None for name in ("open", "high", "low", "close")):
            continue
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]) or values["high"] < values["low"]:
            continue
        bars.append({"time": str(bar.get("ts") or ""), **values})
    fetched = _finite_number(dataset.get("fetched_at_unix"))
    age = time.time() - fetched if fetched is not None else None
    data_status = "unavailable" if not bars or age is None or age < 0 else ("fresh" if age <= MARKET_DATA_FRESH_MAX_AGE_SECONDS else "stale")
    last = raw_bars[-1] if raw_bars and isinstance(raw_bars[-1], dict) else {}
    dashboard = get_oracle_dashboard_data(broker)
    all_open_trades = get_live_open_trades(broker)
    return {
        "status": "ok" if bars else "unavailable",
        "symbol": sym,
        "timeframe": tf,
        "broker": broker,
        "bars": bars,
        "candles": bars,
        "data_status": data_status,
        "data_source": (f"{dataset.get('source')}:aggregated_M15_to_M45" if dataset and derived_m45
                        else dataset.get("source") if dataset else None),
        "updated_at_unix": fetched,
        "last_price": _finite_number(last.get("close")),
        "live_bid": None,
        "live_ask": None,
        "spread_pips": None,
        "session": last.get("session"),
        "regime": last.get("regime"),
        "ema9": None if derived_m45 else _finite_number(last.get("ema9")),
        "ema21": None if derived_m45 else _finite_number(last.get("ema21")),
        "ema50": None if derived_m45 else _finite_number(last.get("ema50")),
        "rsi14": None if derived_m45 else _finite_number(last.get("rsi14")),
        "atr14": None if derived_m45 else _finite_number(last.get("atr14")),
        # Only current authenticated positions are drawn over OHLC bars. Stale
        # snapshots remain visible in the side panel as last-known observations.
        "open_trades": [row for row in all_open_trades if row.get("symbol") == sym and row.get("status") == "OPEN"]
                        if dashboard.get("positions_status") in {"fresh", "partial"} else [],
        "all_open_trades": all_open_trades,
        "positions_status": dashboard.get("positions_status", "unavailable_or_stale"),
    }


def execute_oracle_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed for legacy synthetic execution actions; use local run controls instead."""
    action = str(payload.get("action") or "").strip().lower()
    return {
        "status": "unavailable",
        "action": action or "unsupported",
        "message": "Manual simulated open, close, and broker-cycle actions are disabled. Use the local Oracle run controls; they start the configured demo trader and do not synthesize broker state.",
        "open_trades": [],
    }


def get_telegram_signal_feed(limit: int = 100) -> Dict[str, Any]:
    """Read actual notifier delivery events; never reconstruct signals from trades."""
    path = ORACLE_MEMORY_DIR / "logs" / "telegram_signal_ledger.jsonl"
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    if not path.is_file():
        return {"status": "unavailable", "items": [], "count": 0,
                "message": "Telegram delivery ledger is not present."}
    items: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                stamp = _finite_number(row.get("timestamp_unix", row.get("sent_at_unix", row.get("created_at_unix"))))
                message = row.get("message", row.get("text", row.get("alert")))
                status = row.get("status", row.get("delivery_status"))
                if not isinstance(message, str) or not status:
                    continue
                items.append({"timestamp_unix": stamp, "status": str(status), "message": message,
                              "correlation_id": row.get("correlation_id", row.get("id")),
                              "source": row.get("source", "telegram_signal_ledger")})
    except OSError as exc:
        return {"status": "unavailable", "items": [], "count": 0,
                "message": f"Telegram delivery ledger could not be read: {type(exc).__name__}."}
    items.sort(key=lambda item: item.get("timestamp_unix") or 0, reverse=True)
    return {"status": "ok", "items": items[:limit], "count": min(len(items), limit),
            "source": "Oracle Telegram delivery ledger"}


# ──────────────────────────────────────────────────────────────────────
# 3. FORGE: PLAIN-ENGLISH STRATEGY TRAINING & CANDIDATE HUMANIZER
# ──────────────────────────────────────────────────────────────────────

FAMILY_PLAIN_NAMES: Dict[str, str] = {
    "SCALP_PRECISION": "SCALPING",
    "INTRADAY_CONTINUATION": "DAY_TRADING",
    "SWING_PULLBACK": "SWING_TRADING",
    "RANGE_MEAN_REVERSION": "RANGE_SPECIALIST",
}
STYLE_TO_FAMILY = {value: key for key, value in FAMILY_PLAIN_NAMES.items()}


def _humanize_forge_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Expose only recorded candidate metrics; absent evaluation fields stay unavailable."""
    out = dict(candidate)
    report = candidate.get("lockbox_holdout_report") or {}
    metrics = report.get("candidate_metrics") or candidate.get("validation") or {}
    trades = metrics.get("trades", metrics.get("total_trades"))
    raw_wr = metrics.get("win_rate_pct", metrics.get("win_rate"))
    if raw_wr is not None:
        try:
            raw_wr = float(raw_wr)
            raw_wr = raw_wr * 100.0 if 0 <= raw_wr <= 1 else raw_wr
            win_label = f"{raw_wr:.1f}% win rate" + (f" ({trades} holdout trades)" if trades is not None else "")
        except (TypeError, ValueError):
            win_label = "Unavailable"
    else:
        win_label = "Unavailable · no holdout win-rate record"
    pf = metrics.get("profit_factor")
    dd = metrics.get("max_drawdown_pct")
    out["plain_family_name"] = str(candidate.get("trader_style") or candidate.get("strategy_family") or "Strategy candidate")
    out["plain_state_badge"] = str(candidate.get("lifecycle_state") or "State unavailable")
    out["plain_win_loss_summary"] = win_label
    out["plain_profit_summary"] = f"Profit factor {pf}" if pf is not None else "Unavailable · no holdout profit factor"
    out["plain_safety_summary"] = f"Maximum drawdown {dd}%" if dd is not None else "Unavailable · no recorded risk metric"
    out["plain_story"] = str(candidate.get("strategy_description") or candidate.get("hypothesis_statement") or "No strategy description is recorded.")
    return out


FORGE_CANDIDATES_DIR = RESEARCH_DIR / "forge_candidates"


def _load_forge_candidates_store() -> List[Dict[str, Any]]:
    """Load Forge's Chronicle-owned candidate artifacts without creating sample candidates."""
    if not FORGE_CANDIDATES_DIR.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in FORGE_CANDIDATES_DIR.glob("cand-v2-*.json"):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(candidate, dict) or not candidate.get("candidate_id") or not candidate.get("symbol"):
            continue
        records.append(candidate)
    records.sort(key=lambda row: float(row.get("updated_at_unix") or row.get("created_at_unix") or 0), reverse=True)
    return records


def _write_candidate_artifact(path: Path, candidate: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(candidate, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _forge_blocked(action: str, reason: str) -> Dict[str, Any]:
    return {"status": "blocked", "action": action, "message": reason,
            "plain_english_report": {"title": "Forge action blocked", "headline": reason,
                                     "what_happened": "No candidate state or broker state was changed.",
                                     "key_numbers": [], "next_step_tip": "Check the required data and governed workflow, then retry."}}


def execute_forge_action(action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run real Forge V2 research operations; never synthesize candidate metrics or approvals."""
    payload = payload if isinstance(payload, dict) else {}
    act = (action or "status").lower().strip()
    candidates = _load_forge_candidates_store()
    if act == "status":
        return {"status": "ok" if candidates else "unavailable",
                "candidates": [_humanize_forge_candidate(row) for row in candidates],
                "candidate_count": len(candidates),
                "message": "Chronicle candidate artifacts loaded." if candidates else "No Forge candidate artifacts are recorded in Chronicle."}

    if act in {"discover_v2", "pilot_run", "surgical_retrain"}:
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol or len(symbol) > 32 or not all(ch.isalnum() or ch in "._-" for ch in symbol):
            return _forge_blocked(act, "A valid symbol is required; Forge will not substitute a default symbol.")
        requested = str(payload.get("strategy_family") or payload.get("family") or "").strip().upper()
        if act == "surgical_retrain":
            candidate_id = str(payload.get("candidate_id") or "").strip()
            base = next((row for row in candidates if row.get("candidate_id") == candidate_id), None)
            if not base:
                return _forge_blocked(act, "The requested Chronicle candidate was not found.")
            symbol = str(base["symbol"]).upper()
            style = str(base.get("trader_style") or "").upper()
            styles = [style] if style in STYLE_TO_FAMILY.values() else []
        elif requested in FAMILY_PLAIN_NAMES:
            styles = [FAMILY_PLAIN_NAMES[requested]]
        elif requested in STYLE_TO_FAMILY:
            styles = [requested]
        elif requested in {"", "ALL"}:
            styles = list(STYLE_TO_FAMILY.values())
        else:
            return _forge_blocked(act, "Unknown training style. Choose a supported Forge strategy family.")
        if not styles:
            return _forge_blocked(act, "The selected candidate has no recognized training style.")

        from Forge.core.mtf_institutional_engine import MultiTimeframeInstitutionalEngine
        engine = MultiTimeframeInstitutionalEngine(ROOT_DIR)
        before: Dict[str, Dict[str, Any]] = {}
        if act == "surgical_retrain":
            for style in styles:
                cid = f"cand-v2-{symbol.lower()}-{style.lower()}"
                path = FORGE_CANDIDATES_DIR / f"{cid}.json"
                if path.exists():
                    try:
                        existing = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(existing, dict):
                            before[cid] = existing
                    except (OSError, ValueError):
                        pass
        result = engine.train_and_certify_symbol_strategies(symbol, styles=styles, force_refresh_data=bool(payload.get("force_refresh_data", False)))
        if not isinstance(result, dict) or result.get("status") != "complete":
            return {"status": str((result or {}).get("status") or "blocked"), "result": result,
                    "plain_english_report": {"title": f"Forge review for {symbol}",
                                             "headline": str((result or {}).get("message") or "Forge could not complete this evaluation."),
                                             "what_happened": "No synthetic result was substituted.", "key_numbers": []}}
        versions_saved = 0
        for candidate_id, prior in before.items():
            path = FORGE_CANDIDATES_DIR / f"{candidate_id}.json"
            try:
                updated = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(updated, dict):
                continue
            history = list(prior.get("previous_versions") or [])
            previous_copy = {key: value for key, value in prior.items() if key != "previous_versions"}
            if previous_copy.get("dataset_fingerprint") != updated.get("dataset_fingerprint") or previous_copy.get("updated_at_unix") != updated.get("updated_at_unix"):
                history.append(previous_copy)
            updated["previous_versions"] = history[-10:]
            _write_candidate_artifact(path, updated)
            versions_saved += 1
        metrics = result.get("strategies_certified") or {}
        summary_lines = []
        for style, item in metrics.items():
            summary_lines.append(f"{style}: {item.get('lifecycle_state', 'state unavailable')}; cost gate {item.get('cost_integrity_status', 'unavailable')}; holdout trades {item.get('lockbox_trades', 'unavailable')}; win rate {item.get('lockbox_win_rate_pct', 'unavailable')}%.")
        report = {"title": f"Forge evaluation completed for {symbol}",
                  "headline": f"The institutional Forge pipeline returned {len(metrics)} strategy result(s).",
                  "what_happened": "\n".join(summary_lines) or "The trainer returned no strategy result rows.",
                  "key_numbers": [{"label": "Symbol", "value": symbol},
                                  {"label": "Styles evaluated", "value": str(len(metrics))},
                                  {"label": "Prior versions preserved", "value": str(versions_saved)}],
                  "next_step_tip": "Review the saved Chronicle candidate record and its validation gates before any promotion decision."}
        return {**result, "status": "complete", "candidates": [_humanize_forge_candidate(row) for row in _load_forge_candidates_store()],
                "plain_english_report": report}

    if act == "shadow_run":
        candidate_id = str(payload.get("candidate_id") or "").strip()
        candidate = next((row for row in candidates if row.get("candidate_id") == candidate_id), None)
        if not candidate:
            return _forge_blocked(act, "A recorded Chronicle candidate is required before a shadow evaluation.")
        from Forge.core.shadow_runner import ForgeShadowRunner
        style = str(candidate.get("trader_style") or "").upper()
        if style not in STYLE_TO_FAMILY.values():
            return _forge_blocked(act, "The selected candidate has no recognized strategy style.")
        runner = ForgeShadowRunner(ROOT_DIR, paper_only=True)
        result = runner.run_shadow_session(str(candidate["symbol"]).upper(), trader_style=style, lookback_bars=260, refresh_live=True)
        return {**result, "candidate_id": candidate_id,
                "plain_english_report": {"title": "Forge shadow evaluation",
                                         "headline": str(result.get("status") or "Evaluation returned"),
                                         "what_happened": str(result.get("message") or "Results are the actual Forge shadow-run output."),
                                         "key_numbers": []}}

    if act in {"transition_candidate", "rollback_candidate"}:
        return _forge_blocked(act, "Candidate lifecycle changes require the Forge and Aegis governed promotion workflow. The dashboard cannot directly approve a strategy for Oracle.")
    if act == "register_cost_profile":
        return _forge_blocked(act, "Cost profiles must come from verified broker observations or an explicitly reviewed profile; this action does not write assumptions.")
    if act in {"sync_catalog", "flush_outbox"}:
        return _forge_blocked(act, "No verified catalog/outbox delivery acknowledgement is available, so Forge will not report a successful sync.")
    if act == "get_candidate":
        candidate_id = str(payload.get("candidate_id") or "").strip()
        candidate = next((row for row in candidates if row.get("candidate_id") == candidate_id), None)
        return {"status": "ok", "candidate": _humanize_forge_candidate(candidate)} if candidate else {"status": "not_found", "message": "Candidate not found in Chronicle."}
    return {"status": "unavailable", "message": f"Unsupported Forge action: {act}"}


# ──────────────────────────────────────────────────────────────────────
# 4. CHRONICLE, AEGIS & GENESIS: INTERACTIVE FRONTEND ACTIONS
# ──────────────────────────────────────────────────────────────────────

def execute_specialist_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute interactive frontend actions for Chronicle, Aegis, and Genesis:
    - agent='chronicle', action='add_memory' (title, note, category)
    - agent='aegis', action='test_trade_safety' (symbol, direction, entry, sl, tp, volume)
    - agent='genesis', action='create_blueprint' (market, style, idea_name)
    """
    agent = str(payload.get("agent") or "").lower().strip()

    if agent == "chronicle":
        note = str(payload.get("note") or "").strip()
        title = str(payload.get("title") or note[:100]).strip()
        category = str(payload.get("category") or "Unclassified").strip()
        if not title or not note:
            return {"status": "blocked", "agent": "chronicle", "message": "A title and note are required; no default event is recorded."}
        journal_path = RESEARCH_DIR / "chronicle_user_notes.jsonl"
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "note_id": f"CHR-{int(time.time()) % 100000}",
            "title": title,
            "category": category,
            "note": note,
            "timestamp": _utc_now(),
        }
        with open(journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return {
            "status": "recorded",
            "agent": "chronicle",
            "entry": entry,
            "human_summary": f"Appended to Chronicle's local user-note log as {entry['note_id']} at {entry['timestamp']}. This is not confirmation of indexing, replication, or semantic verification.",
        }

    if agent == "aegis":
        from Nexus.core.institutional_contracts import evaluate_directional_trade_risk_policy

        sym = str(payload.get("symbol") or "").upper().strip()
        direction = str(payload.get("direction") or "BUY").upper().strip()
        try:
            entry = float(payload["entry"])
            sl = float(payload.get("sl", payload.get("stop_loss")))
            tp = float(payload.get("tp", payload.get("take_profit")))
            atr = float(payload["atr"]) if payload.get("atr") is not None else None
            if not all(math.isfinite(v) and v > 0 for v in (entry, sl, tp)) or (atr is not None and (not math.isfinite(atr) or atr <= 0)):
                raise ValueError("Prices and optional ATR must be finite positive numbers")
        except (KeyError, TypeError, ValueError):
            return {"status": "blocked", "agent": "aegis", "message": "Provide finite positive entry, stop-loss, take-profit, and optional ATR values. No example prices are substituted."}
        policy = evaluate_directional_trade_risk_policy(side=direction, entry_price=entry,
                                                       stop_loss=sl, take_profit=tp, atr=atr)
        approved = bool(policy.get("valid"))
        verdict = "PASS · risk_policy.v2 geometry" if approved else "BLOCK · risk_policy.v2 geometry"
        explanation = "; ".join(policy.get("violations") or []) if not approved else "The supplied prices pass the directional geometry checks in risk_policy.v2. This is not a complete account-level or execution authorization."
        return {
            "status": "ok",
            "agent": "aegis",
            "approved": approved,
            "verdict": verdict,
            "symbol": sym,
            "direction": direction,
            "policy": policy,
            "reward_to_risk": policy.get("reward_to_risk_r"),
            "human_summary": explanation,
        }

    if agent == "genesis":
        return {"status": "unavailable", "agent": "genesis",
                "message": "Blueprint creation is not connected to Genesis's governed proposal and stress-test pipeline. No strategy or test result was fabricated."}

    return {"status": "unavailable", "message": f"No specialist action is registered for {agent or 'the requested agent'}."}
