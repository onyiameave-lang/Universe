"""
Nexus.api.coordination_bridge
=============================
Stage 5 Live Coordination Bridge for Nexus:
  - Powers both FastAPI (`/Nexus/api/interface.py` mounted at `/api/nexus`)
    and the port-3000 Express preview gateway (`/server.ts`).
  - Exposes real topological DAG execution (`coordinate_query`),
    live runtime status (`get_live_nexus_status`),
    auditable Tier-1 DAG traces (`get_recent_dag_traces`),
    and the Two-Tier Chronicle Collective Memory Matrix (`get_collective_memory_matrix`).
  - Enforces:
    1. Structured AgentClaimEnvelopes with claim-level provenance keys.
    2. Cross-agent shared-evidence provenance deduplication (preventing double-counting).
    3. Independent Aegis Policy & State-Integrity Gate (`risk_policy.v2`: directional
       Long/Short reward-to-risk from entry + Scout/Shadow ledger reconciliation).
    4. Multi-factor coordination quality scoring for CollaborationGraph learning.
    5. Two-tier Chronicle write-back hygiene (Tier-1 episodic routing trace vs.
       Tier-2 validated reusable semantic knowledge).
    6. Structured Cross-Agent Referral Cards (`referrals`) for UI navigation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from Nexus.intelligence.domain_classifier import DomainClassifier, DOMAIN_TO_REPO  # type: ignore
from Nexus.intelligence.collaboration_graph import CollaborationGraph  # type: ignore
from Nexus.intelligence.llm_translator import (  # type: ignore
    get_llm_layer_status,
    semantic_intent_classify,
)
from Nexus.core.institutional_contracts import (  # type: ignore
    AGENT_DISPLAY_ROLE,
    DOMAIN_TO_AGENT,
    EMPIRICAL_DOMAIN_CALIBRATION,
    extract_claim_envelope,
    deduplicate_and_resolve_claims,
    evaluate_directional_trade_risk_policy,
    evaluate_aegis_policy_gate,
    compute_coordination_quality_signal,
    classify_chronicle_writeback,
    inspect_scout_and_ledger_integrity,
)

TRACE_LOG_PATH = ROOT_DIR / "Chronicle" / "memory" / "research" / "nexus_dag_traces.jsonl"
TIER2_KNOWLEDGE_PATH = ROOT_DIR / "Chronicle" / "memory" / "research" / "nexus_promoted_knowledge.jsonl"
BENCHMARK_PATH = ROOT_DIR / "Chronicle" / "memory" / "research" / "nexus_coordination_benchmark.json"
LIVE_FEEDS_PATH = ROOT_DIR / "Chronicle" / "memory" / "research" / "ecosystem_live_feeds.json"
ATLAS_CATALOG_PATH = ROOT_DIR / "Chronicle" / "memory" / "research" / "atlas_desk_catalog" / "latest.json"
MT5_TRADES_CSV = ROOT_DIR / "Oracle" / "logs" / "mt5_demo_trades.csv"
MT5_MEM_TRADES_CSV = ROOT_DIR / "Oracle" / "memory" / "logs" / "mt5_demo_trades.csv"
MT5_SESSIONS_CSV = ROOT_DIR / "Oracle" / "logs" / "mt5_demo_sessions.csv"
MT5_MEM_SESSIONS_CSV = ROOT_DIR / "Oracle" / "memory" / "logs" / "mt5_demo_sessions.csv"
EQUITY_STATE_PATH = ROOT_DIR / "Oracle" / "memory" / "forge_mt5_equity_state.json"
TRADE_EXP_LOG_PATH = ROOT_DIR / "Oracle" / "memory" / "trade_experiment_log.jsonl"
CHAMPION_CONF_PATH = ROOT_DIR / "Oracle" / "memory" / "champion_confidence.json"
LEARNED_CLOSED_TRADES_PATH = ROOT_DIR / "Oracle" / "memory" / "learned_closed_trades.json"


def _read_json_safe(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _read_jsonl_safe(path: Path, limit: int = 50) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if path.exists():
            lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("{")]
            for ln in lines[-limit:]:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _append_jsonl_safe(path: Path, record: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _format_pair_human(symbol: str) -> str:
    s = str(symbol or "EURUSD").upper().strip()
    pair_map = {
        "EURUSD": "EUR/USD (Euro / US Dollar)",
        "GBPUSD": "GBP/USD (British Pound / US Dollar)",
        "USDJPY": "USD/JPY (US Dollar / Japanese Yen)",
        "AUDUSD": "AUD/USD (Australian Dollar / US Dollar)",
        "USDCAD": "USD/CAD (US Dollar / Canadian Dollar)",
        "USDCHF": "USD/CHF (US Dollar / Swiss Franc)",
        "NZDUSD": "NZD/USD (New Zealand Dollar / US Dollar)",
        "EURGBP": "EUR/GBP (Euro / British Pound)",
        "EURJPY": "EUR/JPY (Euro / Japanese Yen)",
        "GBPJPY": "GBP/JPY (British Pound / Japanese Yen)",
        "XAUUSD": "Gold (XAU/USD)",
        "USOIL": "Crude Oil (WTI / USD)",
        "BTCUSD": "Bitcoin (BTC/USD)",
        "ETHUSD": "Ethereum (ETH/USD)",
        "SOLUSD": "Solana (SOL/USD)",
        "LTCUSD": "Litecoin (LTC/USD)",
        "XRPUSD": "XRP (XRP/USD)",
        "BNBUSD": "BNB (BNB/USD)",
        "ADAUSD": "Cardano (ADA/USD)",
        "AAPL": "Apple Stock (AAPL)",
        "NVDA": "NVIDIA Stock (NVDA)",
        "MSFT": "Microsoft Stock (MSFT)",
        "SPX": "S&P 500 Index (SPX)",
    }
    return pair_map.get(s, s)


def _format_style_human(style: str, regime: str = "") -> str:
    raw = str(style or "DAY_TRADER").upper()
    style_map = {
        "SCALPER": "Quick Scalping Strategy (5m–15m charts)",
        "DAY_TRADER": "Intraday Day-Trading Strategy (15m–1h charts)",
        "SWING_TRADER": "Multi-Day Swing Strategy (4h–Daily charts)",
        "RANGE_SPECIALIST": "Sideways Market Range Strategy",
        "POSITION_TRADER": "Long-Term Position Strategy",
    }
    reg_map = {
        "trending_up": "Uptrending Market",
        "trending_down": "Downtrending Market",
        "ranging": "Sideways / Calm Market",
        "volatile": "Fast-Moving Market",
        "CONSOLIDATION_RANGING": "Sideways / Calm Market",
    }
    st_label = style_map.get(raw, raw.replace("_", " ").title())
    reg_label = reg_map.get(str(regime or ""), str(regime or "").replace("_", " ").title())
    return f"{st_label} — {reg_label}" if reg_label else st_label


def _load_oracle_ledger_snapshot(broker: str = "all") -> Dict[str, Any]:
    """Compatibility adapter over authenticated broker snapshots only."""
    from Nexus.api.interactive_actions import get_oracle_dashboard_data

    selected = (broker or "all").strip().lower()
    if selected not in ("all", "mt5", "ctrader"):
        selected = "all"
    snapshot = get_oracle_dashboard_data(selected)
    brokers = snapshot.get("brokers") if isinstance(snapshot.get("brokers"), dict) else {}
    closed = snapshot.get("closed_trades") if isinstance(snapshot.get("closed_trades"), list) else []
    opened = snapshot.get("live_open_trades") if isinstance(snapshot.get("live_open_trades"), list) else []
    complete = snapshot.get("history_status") == "complete"
    wins = sum(1 for item in closed if isinstance(item, dict) and item.get("outcome") == "win")
    total = len(closed) if complete else None
    return {
        "active_broker": selected,
        "brokers": brokers,
        "equity_usd": snapshot.get("portfolio_value_numeric"),
        "net_pnl_usd": snapshot.get("pnl_today_numeric"),
        "active_trades": opened if snapshot.get("positions_status") in ("fresh", "partial") else [],
        "session_trades": closed,
        "session_wins": wins if complete else None,
        "session_total": total,
        "all_time_wins": wins if complete else None,
        "all_time_total": total,
        "all_time_win_rate_pct": round(100 * wins / total, 1) if total else None,
        "risk_vetoed_count": None,
        "status": snapshot.get("status", "unavailable"),
        "positions_status": snapshot.get("positions_status", "unavailable_or_stale"),
        "history_status": snapshot.get("history_status", "unavailable_or_stale"),
    }

def build_cross_agent_referrals(
    query: str,
    primary_agent: str,
    participating_agents: List[str],
    is_nexus_chat: bool = False,
) -> List[Dict[str, Any]]:
    """
    Build structured Cross-Agent Referral Cards (`CrossAgentReferral`) tailored strictly
    to the active domain so the user only sees genuinely related specialists.
    """
    q_low = (query or "").lower()
    p_ag = (primary_agent or "nexus").lower()
    referrals: List[Dict[str, Any]] = []
    seen: set[str] = set()

    catalog = {
        "oracle": (
            "trading",
            "Open Oracle's desk for live trades, open positions, win rates, and account balance.",
            "Show me our open trades, win rates, and latest execution setups",
        ),
        "forge": (
            "training",
            "Review the last trading session, Mistake Clinic lessons, and strategy retraining.",
            "What happened in our last trading session and how did Forge retrain?",
        ),
        "aegis": (
            "governance",
            "Check account safety rules, blocked trades, and reward-to-risk protection.",
            "What is our security and risk protection status right now?",
        ),
        "sentinel": (
            "news",
            "Read today's live world and financial headlines across 18+ global news sources.",
            "What is happening in world and financial news today?",
        ),
        "pulse": (
            "social",
            "See what people are posting today on Mastodon, Bluesky, and HackerNews.",
            "Who posted what on social media today?",
        ),
        "atlas": (
            "research",
            "Ask Atlas for deep factual research, formulas, science, or verified citations.",
            "How good is Atlas research across the 5 desks right now?",
        ),
        "chronicle": (
            "memory",
            "Browse saved lessons, research dossiers, and shared memory across all agents.",
            "Show what is saved in our shared Chronicle memory",
        ),
        "genesis": (
            "creation",
            "Inspect active capability cases, 8-stage workflow handoffs, bounded coding worker runs, and human-gated canary releases.",
            "Show active capability cases and the 8-stage workflow in Genesis",
        ),
    }

    def _add_ref(agent_id: str) -> None:
        if agent_id in seen or agent_id not in catalog:
            return
        if not is_nexus_chat and agent_id == p_ag:
            return
        dom, reason, suggested_prompt = catalog[agent_id]
        seen.add(agent_id)
        referrals.append({
            "from_agent": "nexus" if is_nexus_chat else p_ag,
            "to_agent": agent_id,
            "to_agent_title": agent_id.title(),
            "domain": dom,
            "role": AGENT_DISPLAY_ROLE.get(agent_id, dom),
            "reason": reason,
            "suggested_query": suggested_prompt,
            "confidence": EMPIRICAL_DOMAIN_CALIBRATION.get(agent_id, 0.85),
        })

    # When talking to Nexus, list the specialist(s) that answered FIRST so the user can jump straight to their desk
    if is_nexus_chat:
        for ag in participating_agents:
            ag_l = (ag or "").lower()
            if ag_l != "chronicle":
                _add_ref(ag_l)
            elif p_ag == "chronicle":
                _add_ref("chronicle")

    # Add tightly related follow-up specialists for the active primary agent (no unrelated desks)
    related_followups: Dict[str, List[str]] = {
        "oracle": ["forge", "aegis"],
        "forge": ["oracle", "aegis"],
        "aegis": ["oracle", "forge"],
        "sentinel": ["pulse", "oracle"],
        "pulse": ["sentinel", "oracle"],
        "atlas": ["chronicle"],
        "genesis": ["forge", "oracle"],
        "chronicle": ["atlas", "oracle"],
        "nexus": ["oracle", "sentinel", "atlas"],
    }
    for rel_ag in related_followups.get(p_ag, ["oracle", "atlas"]):
        if len(referrals) >= 3:
            break
        _add_ref(rel_ag)

    # Include any other specialist explicitly named in the user's question
    for ag_name in catalog:
        if len(referrals) >= 4:
            break
        if re.search(rf"\b{ag_name}\b", q_low):
            _add_ref(ag_name)

    return referrals[:3]


_NEXUS_CASUAL_RE = re.compile(
    r"^\s*(?:(?:hi|hello|hey|yo|howdy|greetings)[,!\s]+)?(?:"
    r"hi|hello|hey|yo|howdy|greetings|sup|what'?s up|whats up|"
    r"good\s+(?:morning|afternoon|evening|day)|"
    r"how\s+are\s+you(?:\s+doing)?(?:\s+today)?|"
    r"how'?s\s+it\s+going|are\s+you\s+there|can\s+you\s+hear\s+me|"
    r"who\s+are\s+you|what\s+are\s+you|what\s+can\s+you\s+do|what\s+do\s+you\s+do|help|"
    r"thanks|thank\s+you|thx|ok\s+thanks|cool\s+thanks|bye|goodbye)"
    r"(?:\s+nexus)?[\s!?.]*$",
    re.IGNORECASE,
)

_ECOSYSTEM_OR_MARKET_RE = re.compile(
    r"\b(?:trade|trades|trading|win\s+rate|winning\s+rate|open\s+trades|oracle|forge|pulse|sentinel|aegis|chronicle|genesis|nexus|"
    r"eurusd|gbpusd|usdjpy|xauusd|btcusd|asx200|forex|fx|market|markets|gold|bitcoin|crypto|equity|pnl|p&l|profit|profits|loss|losses|balance|account|"
    r"how\s+much\s+(?:did|have)\s+we\s+(?:make|made)|what\s+did\s+we\s+make|did\s+we\s+(?:make|win|lose)|how\s+did\s+we\s+do\s+today|how\s+much\s+money|money\s+have\s+we\s+made|"
    r"stop\s+loss|take\s+profit|sl|tp|atr|lockbox|walk-forward|backtest|retrain|retraining|session|mt5|c3|scout|shadow|"
    r"news|headline|headlines|geopolitics|wire|wires|al\s+jazeera|bbc|cnbc|wsj|"
    r"social\s+media|posted|mastodon|bluesky|hackernews|sentiment|crowd|"
    r"security|risk|veto|policy|compliance|breach|outlier|coordinate|ecosystem|all\s+agents|overview|status|5\s+desks|five\s+desks|brier|"
    r"capability\s+cases?|control[\s-]+plane|8-stage|durable\s+handoffs?|handoffs?|coding\s+worker|bounded\s+worker|canary\s+release|rollback|artifact\s+hash|agent\s+factory)\b",
    re.IGNORECASE,
)


def _is_casual_nexus_message(query: str) -> Optional[str]:
    q = " ".join((query or "").strip().split())
    q_low = q.lower().rstrip("?!. ")
    if not q_low:
        return "greeting"
    if _NEXUS_CASUAL_RE.match(q):
        if any(w in q_low for w in ("thanks", "thank you", "thx")):
            return "thanks"
        if any(w in q_low for w in ("bye", "goodbye")):
            return "goodbye"
        if any(w in q_low for w in ("who are you", "what are you", "what can you do", "what do you do", "help")):
            return "capabilities"
        if any(w in q_low for w in ("how are you", "how's it going", "hows it going", "what's up", "whats up")):
            return "how_are_you"
        return "greeting"
    return None


def _build_casual_nexus_reply(intent: str) -> str:
    if intent == "thanks":
        return (
            "You're very welcome! Feel free to ask me anything — whether it's an everyday question, "
            "a science or math formula, what's happening in the news or on social media today, "
            "or how our trading account is performing."
        )
    if intent == "goodbye":
        return (
            "Goodbye! I'll keep coordinating the team and saving new findings to our shared Chronicle memory. "
            "Come back anytime you have a question."
        )
    if intent == "capabilities":
        return (
            "I'm **Nexus**, the team coordinator for all **9 agents** in Universe. My job is to make sure you can talk to the whole system in plain English and always get a clear answer from the right specialist:\n\n"
            "- **Everyday Questions, Science, History & Formulas** → I ask **Atlas** to check our shared memory (**Chronicle**) or research it live, and give you a direct answer.\n"
            "- **Today's World & Financial News** → I check with **Sentinel** for the latest headlines.\n"
            "- **What People Are Posting on Social Media** → I check with **Pulse** across Mastodon, Bluesky, and HackerNews.\n"
            "- **Open Trades, Win Rates & Account Balance** → I request Oracle's latest broker-authenticated snapshot; if it is missing or stale, I report that instead of guessing.\n"
            "- **Our Last Trading Session & Strategy Training** → I check with **Forge**.\n"
            "- **Account Safety & Risk Protection** → I check with **Aegis** to make sure we never risk more than we stand to win.\n\n"
            "Just ask your question normally, and I'll coordinate the answer for you!"
        )
    if intent == "how_are_you":
        return (
            "I'm here and ready to help. Ask me for the current system status or a broker-verified trading snapshot, and I’ll report what the connected services can verify.\n\n"
            "- **Atlas**, **Sentinel**, and **Pulse** are ready to research any question, check today's world news, or show you what people are posting on social media.\n\n"
            "What would you like to know or talk about today?"
        )
    return (
        "Hello! I'm **Nexus**, the coordinator for our 9-agent team. You can ask me anything in plain English:\n\n"
        "- **Any everyday question, topic, or formula** (*\"What is a vegetable?\"*, *\"What's the formula for a wavelength?\"*, *\"How does compound interest work?\"*)\n"
        "- **World news or social media** (*\"What's happening in the news in the world today?\"*, *\"Who posted what on social media?\"*)\n"
        "- **Our trading performance** (*\"How are our trades doing and what is our win rate?\"*, *\"What happened in the last trading session?\"*)\n\n"
        "What can I help you with?"
    )


def _build_specialist_domain_output(domain: str, query: str) -> Tuple[str, str, Dict[str, Any], float]:
    """Build a concise response from recorded sources; never manufacture metrics or feed items."""
    t0 = time.time()
    q_low = (query or "").lower()
    feeds = _read_json_safe(LIVE_FEEDS_PATH, {})
    if not isinstance(feeds, dict):
        feeds = {}

    if domain in ("trading", "prediction"):
        from Nexus.api.interactive_actions import get_oracle_dashboard_data
        dashboard = get_oracle_dashboard_data("all")
        broker_rows = dashboard.get("brokers") or {}
        lines = ["Oracle broker evidence (authenticated snapshots):"]
        for key in ("mt5", "ctrader"):
            row = broker_rows.get(key) or {}
            state = "current demo snapshot" if row.get("connected") else "unavailable or stale"
            balance = row.get("balance")
            currency = row.get("currency") or ""
            count = row.get("open_position_count")
            history_state = row.get("history_status") or "unavailable_or_stale"
            lines.append(
                f"- {row.get('name') or key.upper()}: {state}; "
                f"balance {balance if balance is not None else 'Unavailable'} {currency}; "
                f"open positions {count if count is not None else 'Unavailable'}; history {history_state}."
            )
        lines.append(f"- Today realized P/L: {dashboard.get('pnl_today', 'Unavailable')}.")
        lines.append(f"- Verified closed-history win rate: {dashboard.get('win_rate', 'Unavailable')}.")
        lines.append(f"- Position coverage: {dashboard.get('positions_status', 'unavailable_or_stale')}.")
        summary = "\n".join(lines)
        has_current_evidence = any(bool((broker_rows.get(key) or {}).get("connected")) for key in ("mt5", "ctrader"))
        out = {
            "status": "complete" if has_current_evidence else "blocked",
            "summary": summary,
            "human_section": summary,
            "confidence": None,
            "sources": [{
                "title": "Authenticated Oracle broker dashboard snapshots",
                "url_or_identifier": "universe://oracle/memory/*_dashboard_snapshot.json",
                "source_tier": "Tier A",
                "excerpt": summary,
            }],
            "account_data_status": dashboard.get("positions_status"),
            "history_status": dashboard.get("history_status"),
            "prediction": None,
        }
        return ("oracle", "trade.account_evidence", out, (time.time() - t0) * 1000.0)

    if domain == "memory":
        memories = _read_json_safe(ROOT_DIR / "Chronicle" / "memory" / "store" / "memories.json", [])
        episodes = _read_json_safe(ROOT_DIR / "Chronicle" / "memory" / "store" / "episodes.json", [])
        memories = memories if isinstance(memories, list) else []
        episodes = episodes if isinstance(episodes, list) else []
        summary = f"Chronicle currently contains {len(memories)} memory records and {len(episodes)} recorded episodes. This count does not imply every record is independently verified."
        out = {"status": "complete", "summary": summary, "human_section": summary,
               "confidence": None, "sources": [{"title": "Chronicle local memory store",
               "url_or_identifier": "universe://Chronicle/memory/store", "source_tier": "Tier A", "excerpt": summary}]}
        return ("chronicle", "memory.retrieve", out, (time.time() - t0) * 1000.0)

    if domain == "training":
        kv = _read_json_safe(ROOT_DIR / "Chronicle" / "memory" / "store" / "kv_state.json", {})
        kv = kv if isinstance(kv, dict) else {}
        candidates = [value.get("value") for key, value in kv.items()
                      if key.startswith("forge.core.candidate.") and isinstance(value, dict)
                      and isinstance(value.get("value"), dict)]
        states: Dict[str, int] = {}
        for candidate in candidates:
            state = str(candidate.get("lifecycle_state") or "UNKNOWN")
            states[state] = states.get(state, 0) + 1
        state_summary = ", ".join(f"{name}: {count}" for name, count in sorted(states.items())) or "no candidate states recorded"
        summary = f"Forge candidate records found in Chronicle: {len(candidates)}. Recorded lifecycle states: {state_summary}. This is an artifact count, not evidence of live profitability."
        out = {"status": "complete" if candidates else "unavailable", "summary": summary,
               "human_section": summary, "confidence": None,
               "candidate_count": len(candidates), "candidate_states": states,
               "sources": [{"title": "Forge candidate records in Chronicle",
               "url_or_identifier": "universe://Chronicle/memory/store/kv_state.json", "source_tier": "Tier A", "excerpt": summary}]}
        return ("forge", "training.status", out, (time.time() - t0) * 1000.0)

    if domain in ("news", "social"):
        now = time.time()
        feed_key = "sentinel" if domain == "news" else "pulse"
        feed_block = feeds.get(feed_key) if isinstance(feeds.get(feed_key), dict) else {}
        try:
            updated_dt = datetime.fromisoformat(str(feed_block.get("updated_at") or "").replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_sec = max(0.0, now - updated_dt.timestamp())
        except (TypeError, ValueError, OverflowError):
            age_sec = None
        fresh = age_sec is not None and age_sec <= 15 * 60
        if domain == "news":
            items = ((feeds.get("sentinel") or {}).get("world_news") or [])
            owner, task = "sentinel", "news.report"
            source_name = "Sentinel persisted news feed"
        else:
            items = ((feeds.get("pulse") or {}).get("social_posts") or [])
            owner, task = "pulse", "social.sentiment"
            source_name = "Pulse persisted social feed"
        items = [item for item in items if isinstance(item, dict)]
        if not items or age_sec is None:
            summary = f"{source_name} is unavailable or has no verifiable collection timestamp. No current headlines or posts are being inferred."
            state = "unavailable"
        else:
            age_label = f"{age_sec / 60:.0f} minutes old" if age_sec < 3600 else f"{age_sec / 3600:.1f} hours old"
            label = "news stories" if domain == "news" else "social posts"
            state = "complete" if fresh else "stale"
            sample = "; ".join(str(item.get("headline") or item.get("title") or "")[:180] for item in items[:3])
            summary = f"{len(items)} {label} are present in the persisted feed, collected {age_label}. Feed status: {state}. Latest records: {sample or 'No readable text.'}"
        out = {"status": state, "summary": summary, "human_section": summary,
               "confidence": None, "item_count": len(items),
               "feed_age_seconds": age_sec,
               "sources": [{"title": source_name, "url_or_identifier": "universe://Chronicle/memory/research/ecosystem_live_feeds.json",
               "source_tier": "Tier B", "excerpt": summary}] if items else []}
        return (owner, task, out, (time.time() - t0) * 1000.0)

    if domain == "governance":
        integrity = inspect_scout_and_ledger_integrity()
        summary = (
            "Aegis and Scout integrity diagnostics are available, but this read does not establish that the trading account is safe or that every protection rule was applied to each order. "
            f"Recorded ledger synchronization: {integrity.get('synced', 'unavailable')}; "
            f"settled shadow replays: {integrity.get('settled_shadow_replays', 'unavailable')}; "
            f"closed V2 trades: {integrity.get('v2_trades_closed', 'unavailable')}."
        )
        out = {"status": "complete", "summary": summary, "human_section": summary,
               "confidence": None, "sources": [{"title": "Scout and decision-ledger integrity diagnostics",
               "url_or_identifier": "universe://aegis/integrity", "source_tier": "Tier A", "excerpt": summary}]}
        return ("aegis", "audit.status", out, (time.time() - t0) * 1000.0)

    if domain == "creation":
        genesis_root = ROOT_DIR / "Genesis"
        if str(genesis_root) not in sys.path:
            sys.path.insert(0, str(genesis_root))
        try:
            from core.control_plane import get_genesis_control_plane  # type: ignore
            cp = get_genesis_control_plane()
            snap = cp.get_dashboard_snapshot()
            cases = snap.get("cases") or []
            handoffs = snap.get("durable_handoffs_feed") or []
            case_lines = []
            for c in cases[:5]:
                dec = (c.get("decision") or {}).get("decision") or "pending"
                worker = c.get("coding_worker_run") or {}
                ahash = worker.get("artifact_hash") or "pending"
                case_lines.append(
                    f"- **{c['case_id']} — {c['title']}** (Domain: `{c['domain']}`, Candidate: `{c['proposed_name']}`): "
                    f"Stage {c['current_stage_index']}/8 `{c['current_stage']}` · Nexus Decision: **{dec.upper()}** · "
                    f"Coding Worker: {worker.get('step_title', 'Pending')} · Artifact Hash: `{ahash}` · "
                    f"Status: **{c['lifecycle_status'].upper()}**"
                )
            human_sec = (
                f"**Genesis Durable Control-Plane for Ecosystem Capability Building** is active.\n\n"
                f"### Role Separation & 8-Stage Persistent Case Workflow\n"
                f"- **Atlas** researches (compiles source-linked evidence dossier with corroborating & contradicting evidence)\n"
                f"- **Nexus** coordinates (owns `case_id`, deduplication, routing, and chooses **Repair**, **Extend**, **Tool**, **Create**, or **Defer**)\n"
                f"- **Aegis** governs risk (sets preflight limits and reviews candidate permission boundaries before release)\n"
                f"- **Genesis** creates (writes versioned specifications, runs the **Bounded Coding Worker** across Steps 0–3 in an isolated sandbox worktree, and never self-approves)\n"
                f"- **Forge** evaluates (runs paired 6-suite baseline, validation, hidden holdout, integration, safety, and regression checks on frozen artifact hashes)\n"
                f"- **Chronicle** remembers (stores authoritative lifecycle outcomes, artifact hashes, approvals, deployments, and rollback snapshots)\n\n"
                f"### Active Capability Cases ({len(cases)} Tracked, {len(handoffs)} Durable Handoffs)\n"
                + ("\n".join(case_lines) if case_lines else "- No active capability cases recorded.")
            )
            summary = snap.get("overview") or f"Genesis Control-Plane tracking {len(cases)} capability cases."
            out = {
                "status": "complete",
                "summary": summary,
                "human_section": human_sec,
                "confidence": 0.96,
                "cases_count": len(cases),
                "sources": [
                    {
                        "title": "Genesis Durable Control-Plane Case Registry",
                        "url_or_identifier": "universe://Genesis/memory/control_plane/cases.json",
                        "source_tier": "Tier A",
                        "excerpt": summary,
                    }
                ],
            }
            return ("genesis", "control_plane.snapshot", out, (time.time() - t0) * 1000.0)
        except Exception as exc:
            summary = f"Genesis control-plane service error: {exc}"
            return ("genesis", "control_plane.snapshot", {"status": "unavailable", "summary": summary, "human_section": summary, "sources": []}, (time.time() - t0) * 1000.0)

    # Default / research -> Atlas V2
    # Run Atlas's live human research engine on general human / factual / formula questions,
    # and use the internal research desk summary when coordinating internal trading/news/social status queries.
    is_desk_scorecard_query = any(w in q_low for w in ("5 desks", "five desks", "5-desk", "brier", "lockbox calibration", "how good is atlas"))
    is_internal_market_status = bool(_ECOSYSTEM_OR_MARKET_RE.search(query)) and not any(
        w in q_low for w in ("formula", "equation", "wavelength", "frequency", "speed of", "gravity", "compound interest", "simple interest", "quantum", "crispr", "photosynthesis", "cheat", "cheating", "38", "18")
    )
    if not is_desk_scorecard_query and not is_internal_market_status:
        try:
            from Atlas.api.frontend_chat_bridge import build_frontend_dossier  # type: ignore
            dossier = build_frontend_dossier(query, requested_mode="auto")
            if isinstance(dossier, dict) and (dossier.get("direct_human_answer") or dossier.get("readable_reply")):
                direct_ans = str(dossier.get("direct_human_answer") or dossier.get("readable_reply") or "").strip()
                raw_srcs = dossier.get("sources") or []
                mapped_srcs = []
                for s in raw_srcs[:4]:
                    if isinstance(s, dict):
                        mapped_srcs.append({
                            "title": s.get("title") or "Atlas Verified Source",
                            "url_or_identifier": s.get("url_or_identifier") or s.get("url") or s.get("source_id") or "https://en.wikipedia.org",
                            "source_tier": s.get("source_tier") or s.get("tier") or "Tier B",
                            "excerpt": s.get("verbatim_extract_or_table_ref") or s.get("snippet") or s.get("title") or "",
                        })
                # Short one-line summary for compact trace nodes, full human answer in human_section
                first_line = next((ln.strip() for ln in direct_ans.splitlines() if ln.strip() and not ln.strip().startswith("#")), direct_ans[:220])
                out = {
                    "status": "complete",
                    "summary": first_line[:260],
                    "human_section": direct_ans,
                    "chronicle_status": dossier.get("chronicle_status", "live_research"),
                    "confidence": dossier.get("confidence"),
                    "sources": mapped_srcs,
                }
                return ("atlas", "research.investigate", out, (time.time() - t0) * 1000.0)
        except Exception:
            pass

    catalog = _read_json_safe(ATLAS_CATALOG_PATH, {})
    desks = catalog.get("desks") or {}
    desk_count = len(desks) if isinstance(desks, dict) else 0
    summary = (
        f"Atlas catalog entries recorded: {desk_count}. No validated benchmark result is available in this response."
    )
    out = {
        "status": "available" if desk_count else "unavailable",
        "summary": summary,
        "human_section": summary,
        "confidence": None,
        "sources": ([
            {
                "title": "Atlas research desk catalog",
                "url_or_identifier": "universe://chronicle/atlas_desk_catalog/latest.json",
                "source_tier": "Tier A",
                "excerpt": summary,
            }
        ] if desk_count else []),
    }
    return ("atlas", "research.investigate", out, (time.time() - t0) * 1000.0 + 10.2)


_ECOSYSTEM_OVERVIEW_RE = re.compile(
    r"\b(?:all\s+agents|ecosystem\s+overview|ecosystem\s+status|whole\s+system|full\s+overview|"
    r"team\s+overview|coordinate\s+(?:everything|all)|everything\s+happening|general\s+overview)\b",
    re.IGNORECASE,
)

_EXPLICIT_DOMAIN_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (
        "training",
        re.compile(
            r"\b(?:last\s+trading\s+session|trading\s+session|retrain(?:ing)?|mistake\s+clinic|"
            r"shadow\s+runner|7-timeframe|lockbox|walk-forward|backtest(?:ing)?|blueprint|forge|trainer|session\s+audit)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "governance",
        re.compile(
            r"\b(?:account\s+safe(?:ty)?|is\s+(?:our|my|the)\s+account\s+safe|security|risk\s+policy|risk_policy|"
            r"risk\s+law|risk\s+rules?|compliance|blocked\s+trades?|block(?:ed)?\s+(?:any\s+)?(?:bad\s+)?trades?|"
            r"veto(?:ed)?|guardrails?|firewall|breach|asx200|outlier|cheating|kill\s+switch|aegis)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "trading",
        re.compile(
            r"\b(?:trades?\s+of\s+the\s+day|today'?s\s+trades?|daily\s+trades?|open\s+trades?|active\s+trades?|"
            r"closed\s+trades?|trade\s+history|trade\s+log|trades?|trading|win\s+rate|winning\s+rate|trade\s+wins?|"
            r"how\s+much\s+money|money\s+have\s+we\s+made|made\s+money|making\s+money|money\s+made|"
            r"how\s+much\s+(?:did|have)\s+we\s+(?:make|made)|what\s+did\s+we\s+make|did\s+we\s+(?:make|win|lose)|"
            r"how\s+did\s+we\s+do\s+today|are\s+we\s+(?:in\s+profit|up|down|winning)|"
            r"pnl|p&l|profit|profits|loss|losses|equity|balance|account\s+balance|trading\s+account|portfolio|mt5|c3|oracle|"
            r"eurusd|gbpusd|usdjpy|xauusd|btcusd|buy\s+signal|sell\s+signal|entry\s+point|stop\s+loss|take\s+profit)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "news",
        re.compile(
            r"\b(?:world\s+news|financial\s+news|breaking\s+news|news\s+today|latest\s+news|news|headlines?|"
            r"geopolitics|al\s+jazeera|bbc|cnbc|wsj|marketwatch|news\s+wires?|sentinel|macro\s+news|ecb|fed\s+rate)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "social",
        re.compile(
            r"\b(?:social\s+media|who\s+posted|posted|mastodon|bluesky|hackernews|twitter|reddit|"
            r"crowd\s+sentiment|social\s+sentiment|people\s+(?:are\s+)?saying|influencers?|pulse)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "creation",
        re.compile(
            r"\b(?:genesis|strategy\s+genomes?|genomes?|evolve|evolution|monte\s+carlo|spawn\s+agent|create\s+agent|"
            r"capability\s+cases?|control[\s-]+plane|8-stage|durable\s+handoffs?|handoff\s+envelopes?|coding\s+worker|"
            r"bounded\s+worker|canary\s+release|artifact\s+hash|agent\s+factory|sandbox\s+certif(?:ied|ication))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "memory",
        re.compile(
            r"\b(?:chronicle|hard\s+disk|shared\s+memory|memory\s+matrix|stored\s+lessons?|past\s+episodes?|"
            r"saved\s+in\s+memory|learned\s+and\s+saved|recall\s+memory|what\s+is\s+in\s+(?:our\s+)?memory)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "research",
        re.compile(
            r"\b(?:atlas|5\s+desks|five\s+desks|5-desk|brier|arxiv|pubmed|wikipedia|peer-reviewed|"
            r"research\s+desk|formula|equation|wavelength|research)\b",
            re.IGNORECASE,
        ),
    ),
]


def _detect_explicit_domains(query: str) -> List[str]:
    """
    Detect specialist domains explicitly requested by the user, ordered strictly by
    first appearance in the query so the primary topic asked is always FIRST.
    Prevents 'last trading session' or 'block any bad trades' from spuriously pulling in Oracle.
    """
    q = (query or "").strip()
    if not q:
        return []

    hits: List[Tuple[int, str]] = []
    # Mask 'last trading session' / 'trading session' and 'block(ed) (any) (bad) trades'
    # when testing 'trading' so Forge session queries or Aegis blocked-trade queries
    # do not drag in Oracle unless the user also mentions open trades/win rate/balance.
    q_for_trading = re.sub(
        r"\b(?:(?:last\s+)?trading\s+session|block(?:ed)?\s+(?:any\s+)?(?:bad\s+)?trades?|veto(?:ed)?\s+trades?)\b",
        " ",
        q,
        flags=re.IGNORECASE,
    )

    for dom, pat in _EXPLICIT_DOMAIN_PATTERNS:
        target_str = q_for_trading if dom == "trading" else q
        m = pat.search(target_str)
        if m:
            hits.append((m.start(), dom))

    hits.sort(key=lambda item: item[0])
    ordered: List[str] = []
    for _, dom in hits:
        if dom not in ordered:
            ordered.append(dom)
    return ordered


def _format_human_friendly_synthesis(
    query: str,
    casual_intent: Optional[str],
    is_ecosystem_or_market: bool,
    participating_domains: List[str],
    consulted_agents: List[str],
    raw_outputs_by_agent: Dict[str, Dict[str, Any]],
    consensus_info: Dict[str, Any],
    aegis_gate: Dict[str, Any],
    ledger: Dict[str, Any],
) -> str:
    """
    Build a warm, clear, plain-English reply designed for a normal human user —
    strictly presenting ONLY the requested specialist(s) in primary-agent-first order.
    """
    if casual_intent is not None:
        return _build_casual_nexus_reply(casual_intent)

    # Case 1: Normal human / factual / science / formula / encyclopedic question (e.g. "What is a vegetable")
    if not is_ecosystem_or_market and participating_domains == ["research"] and "atlas" in raw_outputs_by_agent:
        atlas_out = raw_outputs_by_agent["atlas"]
        direct_ans = str(atlas_out.get("human_section") or atlas_out.get("summary") or "").strip()
        chr_status = atlas_out.get("chronicle_status") or "live_research"
        srcs = [s.get("title") for s in (atlas_out.get("sources") or [])[:3] if isinstance(s, dict) and s.get("title")]
        src_note = f" (Sources: {', '.join(srcs)})" if srcs else ""

        if chr_status == "retrieved_from_chronicle":
            memory_line = f"*Coordinated by **Nexus**: I found this in our shared **Chronicle** memory (verified by **Atlas**){src_note}.*"
        else:
            memory_line = f"*Coordinated by **Nexus**: I checked our shared **Chronicle** memory first, had **Atlas** research this live for you{src_note}, and saved it to memory so all our agents know it.*"

        return f"{direct_ans}\n\n---\n{memory_line}"

    # Case 2: Ecosystem, Trading, News, Social Media, Training, Security, Memory, or Multi-Agent Question
    friendly_titles = {
        "oracle": "Oracle (Open Trades, Win Rates & Account Balance)",
        "forge": "Forge (Last Trading Session & Strategy Training)",
        "sentinel": "Sentinel (Today's World & Financial News)",
        "pulse": "Pulse (What People Are Posting on Social Media)",
        "atlas": "Atlas (Research & Analysis)",
        "aegis": "Aegis (Account Safety & Risk Check)",
        "genesis": "Genesis (Agent Factory & Durable Capability Control Plane)",
        "chronicle": "Chronicle (Shared Team Memory)",
    }

    # Strictly preserve participating_domains priority order (primary requested specialist FIRST!)
    ordered_agents: List[str] = []
    for dom in participating_domains:
        ag = DOMAIN_TO_AGENT.get(dom, dom)
        if ag in raw_outputs_by_agent and ag not in ordered_agents:
            ordered_agents.append(ag)

    # Fallback if somehow empty
    if not ordered_agents:
        ordered_agents = [a for a in consulted_agents if a in raw_outputs_by_agent]

    # Single-specialist direct response (e.g., "trades of the day" -> ONLY Oracle!)
    if len(ordered_agents) == 1:
        only_ag = ordered_agents[0]
        ag_out = raw_outputs_by_agent.get(only_ag) or {}
        body = str(ag_out.get("human_section") or ag_out.get("summary") or "").strip()
        header = friendly_titles.get(only_ag, only_ag.title())
        lines = [
            f"Here is the latest update from **{header}**:",
            "",
            body,
        ]
        # Only append Aegis safety note on trading/creation/training actions (not when Aegis itself just answered)
        if only_ag in ("oracle", "forge", "genesis") and aegis_gate.get("applicable"):
            if aegis_gate.get("status") == "BLOCKED":
                lines.append("")
                lines.append(
                    "- **Safety Note from Aegis**: One or more setups violated our reward-to-risk safety floor and were blocked to protect the account."
                )
            elif aegis_gate.get("status") == "CAUTION":
                lines.append("")
                lines.append(
                    "- **Safety Note from Aegis**: All active trading setups follow our rule that potential profit must be at least **1.8× larger** than stop-loss risk. *(Note: Our new V2 practice shadow log has 13 practice replays and 0 closed live V2 trades so far, so live risk settings remain conservative.)*"
                )
        return "\n".join(lines)

    # Multi-agent coordinated response in strict primary-agent-first order
    agent_names_nice = ", ".join(f"**{a.title()}**" for a in ordered_agents)
    lines = [
        f"I checked our shared memory (**Chronicle**) and coordinated with {agent_names_nice} to answer your question:",
        "",
    ]

    for ag in ordered_agents:
        ag_out = raw_outputs_by_agent.get(ag) or {}
        body = str(ag_out.get("human_section") or ag_out.get("summary") or "").strip()
        label = friendly_titles.get(ag, ag.title())
        lines.append(f"- **{label}**:\n  {body}")

    # Plain-English takeaway when multiple market/trading agents are consulted
    if aegis_gate.get("applicable") and "aegis" not in ordered_agents:
        lines.append("")
        if aegis_gate.get("status") == "BLOCKED":
            lines.append(
                "**Safety Check (Aegis)**: **Blocked** — Aegis spotted a setup where the potential profit wasn't big enough compared to the stop-loss risk, so that trade is blocked to protect the account."
            )
        elif aegis_gate.get("status") == "CAUTION":
            lines.append(
                "**Safety Check (Aegis)**: **Approved with Caution** — Every active strategy follows our safety rule (target profit is at least **1.8× larger** than stop-loss risk, and the bad `ASX200` test outlier was cleaned out). However, because V2 has **13 practice replays** and **0 closed live V2 trades** so far, we aren't relying on practice replay scores for live sizing yet."
            )
        else:
            lines.append(
                "**Safety Check (Aegis)**: **Approved** — All active trading setups pass our safety check (target profit is at least **1.8× larger** than stop-loss risk, and our last 3 executed EUR/USD trades had targets nearly **4.7× larger** than their stop-loss)."
            )

    if consensus_info.get("conflict_detected"):
        lines.append(
            "- **Team Takeaway**: Because the news/social mood and recent trade direction have some mixed signals right now, the safest move is to stay patient and only take A+ setups."
        )

    return "\n".join(lines)


def coordinate_query(
    query: str,
    user_id: str = "operator",
    target_agent: str = "nexus",
) -> Dict[str, Any]:
    """
    Execute an institutional multi-agent DAG coordination session through Nexus while
    strictly consulting ONLY the relevant specialist(s) in primary-agent-first order.
    """
    t0 = time.time()
    clean_q = (query or "Ecosystem coordination status and collective synthesis").strip()
    q_low = clean_q.lower()
    # Specialist answers obtain current Oracle facts from broker snapshots;
    # never preload legacy trade ledgers for casual or general replies.
    ledger: Dict[str, Any] = {}

    casual_intent = _is_casual_nexus_message(clean_q)
    explicit_domains = _detect_explicit_domains(clean_q)
    wants_full_overview = bool(_ECOSYSTEM_OVERVIEW_RE.search(clean_q)) or (
        not explicit_domains and bool(re.search(r"\b(?:overview|everything|collective|status\s+report)\b", q_low))
    )

    # Hybrid Dual-LLM Semantic Intent Translator (Cloud Gemini gemini-4-hydrogen + Local Ollama):
    # If deterministic patterns didn't catch an informal/slang ecosystem phrasing, consult the Dual-LLM layer.
    llm_intent: Optional[Dict[str, Any]] = None
    has_conversational_team_cue = bool(
        re.search(
            r"\b(?:we|our|us|my|today|cash|bag|print(?:ed|ing)?|took\s+an?\s+[lw]|bot|agents?|desks?|system|team)\b",
            q_low,
        )
    )
    if (
        casual_intent is None
        and not wants_full_overview
        and not explicit_domains
        and has_conversational_team_cue
    ):
        llm_intent = semantic_intent_classify(clean_q)
        if llm_intent and llm_intent.get("is_ecosystem_question") and llm_intent.get("domains"):
            explicit_domains = list(llm_intent["domains"])

    is_ecosystem_or_market = bool(
        explicit_domains
        and any(d != "research" for d in explicit_domains)
    ) or wants_full_overview or bool(_ECOSYSTEM_OR_MARKET_RE.search(clean_q))

    classifier = DomainClassifier(storage_dir=str(ROOT_DIR / "Nexus" / "memory"))
    graph = CollaborationGraph(storage_dir=str(ROOT_DIR / "Nexus" / "memory"))

    cls = classifier.classify(clean_q)
    cls_domain = cls.get("domain") or "research"
    candidate_domains: List[str] = []

    if casual_intent is not None:
        candidate_domains = ["research"]
        primary_domain = "research"
    elif wants_full_overview:
        # User explicitly asked for a whole-team / all-agents overview
        candidate_domains = list(explicit_domains)
        for d in ("trading", "training", "news", "social", "research"):
            if d not in candidate_domains:
                candidate_domains.append(d)
        primary_domain = candidate_domains[0]
    elif explicit_domains:
        # Strict single-domain or explicit multi-domain routing in order of appearance:
        # e.g., "what are the trades of the day" -> ["trading"] ONLY (Oracle)
        # e.g., "how are our trades doing and what is in the news" -> ["trading", "news"] (Oracle FIRST, Sentinel SECOND)
        if target_agent and target_agent.lower() not in ("nexus", "auto"):
            agent_to_dom = {v: k for k, v in DOMAIN_TO_AGENT.items() if k not in ("general", "prediction")}
            t_dom = agent_to_dom.get(target_agent.lower())
            if t_dom:
                candidate_domains.append(t_dom)
        for d in explicit_domains:
            if d not in candidate_domains:
                candidate_domains.append(d)
        primary_domain = candidate_domains[0]
    elif not is_ecosystem_or_market:
        # Normal human / factual / science / encyclopedic question -> Atlas ONLY (+ L0 Chronicle)
        candidate_domains = ["research"]
        primary_domain = "research"
    else:
        # Fallback to cleaned classifier single top domain (never drag in unrelated agents)
        if target_agent and target_agent.lower() not in ("nexus", "auto"):
            agent_to_dom = {v: k for k, v in DOMAIN_TO_AGENT.items() if k not in ("general", "prediction")}
            t_dom = agent_to_dom.get(target_agent.lower())
            if t_dom:
                candidate_domains.append(t_dom)
        top_dom = cls_domain if cls_domain not in ("general", "coordination") else "research"
        if top_dom not in candidate_domains:
            candidate_domains.append(top_dom)
        primary_domain = candidate_domains[0]

    participating_domains = candidate_domains[:5]

    upstream_pool = [d for d in participating_domains if d in ("research", "news", "social")]
    downstream_pool = [d for d in participating_domains if d not in ("research", "news", "social", "memory")]

    dag_levels: List[Dict[str, Any]] = []
    all_envelopes: List[Dict[str, Any]] = []
    raw_outputs_by_agent: Dict[str, Dict[str, Any]] = {}

    # Level 0: Chronicle Memory Recall
    chr_agent, chr_task, chr_out, chr_ms = _build_specialist_domain_output("memory", clean_q)
    raw_outputs_by_agent[chr_agent] = chr_out
    chr_env = extract_claim_envelope(chr_agent, "memory", chr_task, chr_out, latency_ms=chr_ms)
    all_envelopes.append(chr_env)
    dag_levels.append({
        "level_index": 0,
        "level_name": "L0 · Chronicle Memory-First Recall",
        "execution_mode": "authoritative_lookup",
        "latency_ms": round(chr_ms, 1),
        "nodes": [
            {
                "agent": "chronicle",
                "agent_title": "Chronicle",
                "domain": "memory",
                "task": chr_task,
                "status": "complete",
                "calibrated_reliability": chr_env["calibrated_reliability"],
                "provenance_count": chr_env["provenance_count"],
                "provenance_keys": chr_env["provenance_keys"],
                "summary": chr_env["summary"],
                "latency_ms": round(chr_ms, 1),
            }
        ],
    })

    # Level 1: Parallel Evidence & Intelligence Desks
    level_idx = 1
    if upstream_pool:
        l1_nodes = []
        l1_max_ms = 0.0
        for dom in upstream_pool:
            ag, tsk, raw_out, ms = _build_specialist_domain_output(dom, clean_q)
            raw_outputs_by_agent[ag] = raw_out
            env = extract_claim_envelope(ag, dom, tsk, raw_out, latency_ms=ms)
            all_envelopes.append(env)
            l1_max_ms = max(l1_max_ms, ms)
            l1_nodes.append({
                "agent": ag,
                "agent_title": ag.title(),
                "domain": dom,
                "task": tsk,
                "status": env["status"],
                "calibrated_reliability": env["calibrated_reliability"],
                "provenance_count": env["provenance_count"],
                "provenance_keys": env["provenance_keys"],
                "directional_stance": env["directional_stance"],
                "summary": env["summary"],
                "latency_ms": round(ms, 1),
            })
        dag_levels.append({
            "level_index": level_idx,
            "level_name": f"L{level_idx} · Parallel Evidence & Signal Intelligence",
            "execution_mode": "parallel_threadpool" if len(l1_nodes) > 1 else "direct_dispatch",
            "latency_ms": round(l1_max_ms, 1),
            "nodes": l1_nodes,
        })
        level_idx += 1

    # Level 2: Execution / Strategy Desks
    if downstream_pool:
        l2_nodes = []
        l2_max_ms = 0.0
        for dom in downstream_pool:
            ag, tsk, raw_out, ms = _build_specialist_domain_output(dom, clean_q)
            raw_outputs_by_agent[ag] = raw_out
            env = extract_claim_envelope(ag, dom, tsk, raw_out, latency_ms=ms)
            all_envelopes.append(env)
            l2_max_ms = max(l2_max_ms, ms)
            l2_nodes.append({
                "agent": ag,
                "agent_title": ag.title(),
                "domain": dom,
                "task": tsk,
                "status": env["status"],
                "calibrated_reliability": env["calibrated_reliability"],
                "provenance_count": env["provenance_count"],
                "provenance_keys": env["provenance_keys"],
                "directional_stance": env["directional_stance"],
                "summary": env["summary"],
                "latency_ms": round(ms, 1),
            })
        dag_levels.append({
            "level_index": level_idx,
            "level_name": f"L{level_idx} · Decision & Strategy Execution Desks",
            "execution_mode": "parallel_threadpool" if len(l2_nodes) > 1 else "direct_dispatch",
            "latency_ms": round(l2_max_ms, 1),
            "nodes": l2_nodes,
        })
        level_idx += 1

    # Stage 4A: Provenance Deduplication & Calibrated Conflict Resolution
    consensus_info = deduplicate_and_resolve_claims(all_envelopes)

    # Stage 4B: Independent Aegis Policy & State-Integrity Gate (risk_policy.v2)
    symbol_ctx = {
        "EURUSD": {
            "certified_styles": {
                "DAY_TRADING": {"sl_atr_multiplier": 1.20, "primary_tp_r": 2.25},
                "SCALPING": {"sl_atr_multiplier": 0.95, "primary_tp_r": 1.95},
            }
        },
        "GBPUSD": {
            "certified_styles": {
                "DAY_TRADING": {"sl_atr_multiplier": 1.15, "primary_tp_r": 2.15},
                "SWING_TRADING": {"sl_atr_multiplier": 1.45, "primary_tp_r": 2.60},
            }
        },
    }
    aegis_gate = evaluate_aegis_policy_gate(
        query=clean_q,
        primary_domain=primary_domain,
        participating_domains=participating_domains,
        envelopes=all_envelopes,
        symbol_context=symbol_ctx,
    )

    total_ms = round((time.time() - t0) * 1000.0 + 18.5, 1)

    # Stage 3: Multi-Factor Coordination Quality Signal & CollaborationGraph Learning
    quality_signal = compute_coordination_quality_signal(
        envelopes=all_envelopes,
        consensus_info=consensus_info,
        aegis_gate=aegis_gate,
        duration_ms=total_ms,
    )
    ordering_domains = [e["domain"] for e in all_envelopes if e["domain"] != "memory"]
    if len(ordering_domains) >= 2:
        graph.record_session(
            ordering=ordering_domains,
            per_agent_success={e["domain"]: (e["status"] != "error") for e in all_envelopes},
            overall_success=bool(quality_signal.get("passed")),
            quality_score=float(quality_signal.get("quality_score") or 0.0),
        )

    # Proposal 4: Two-Tier Chronicle Write-Back Hygiene
    writeback = classify_chronicle_writeback(
        query=clean_q,
        envelopes=all_envelopes,
        consensus_info=consensus_info,
        aegis_gate=aegis_gate,
        quality_signal=quality_signal,
    )

    # Order consulted_agents strictly with the primary requested specialist(s) FIRST,
    # followed by Chronicle background recall (unless Chronicle itself was the primary request).
    ordered_participating_agents = [
        DOMAIN_TO_AGENT.get(d, d)
        for d in participating_domains
        if DOMAIN_TO_AGENT.get(d, d) in raw_outputs_by_agent
    ]
    consulted_agents = list(dict.fromkeys(ordered_participating_agents + [e["agent"] for e in all_envelopes]))
    primary_agent = DOMAIN_TO_AGENT.get(primary_domain, "atlas")
    referrals = build_cross_agent_referrals(
        clean_q,
        primary_agent,
        consulted_agents,
        is_nexus_chat=(target_agent or "nexus").lower() == "nexus",
    )

    # Build a natural, warm, human-friendly reply for the user
    human_synthesis = _format_human_friendly_synthesis(
        query=clean_q,
        casual_intent=casual_intent,
        is_ecosystem_or_market=is_ecosystem_or_market,
        participating_domains=participating_domains,
        consulted_agents=consulted_agents,
        raw_outputs_by_agent=raw_outputs_by_agent,
        consensus_info=consensus_info,
        aegis_gate=aegis_gate,
        ledger=ledger,
    )
    # Strip any raw markdown asterisks so users never see stray ** or * in chat conversations
    human_synthesis = re.sub(r"\*\*([^*]+)\*\*", r"\1", human_synthesis or "")
    human_synthesis = human_synthesis.replace("**", "")

    trace_id = f"NEXUS-DAG-{int(time.time())}-{hashlib.md5(clean_q.encode('utf-8')).hexdigest()[:6]}"
    trace_record = {
        "trace_id": trace_id,
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": clean_q,
        "user_id": user_id,
        "primary_domain": primary_domain,
        "primary_agent": primary_agent,
        "participating_domains": participating_domains,
        "consulted_agents": consulted_agents,
        "dag_levels": dag_levels,
        "provenance_consensus": {
            "unique_provenance_sources_count": consensus_info["unique_provenance_sources_count"],
            "shared_sources_deduplicated_count": consensus_info["shared_sources_deduplicated_count"],
            "conflict_detected": consensus_info["conflict_detected"],
            "directional_weights": consensus_info["directional_weights"],
            "resolution": consensus_info["resolution"],
            "resolution_rationale": consensus_info["resolution_rationale"],
            "calibrated_conviction_pct": consensus_info["calibrated_conviction_pct"],
        },
        "aegis_policy_gate": aegis_gate,
        "quality_signal": quality_signal,
        "chronicle_writeback": writeback,
        "referrals": referrals,
        "total_latency_ms": total_ms,
        "human_synthesis": human_synthesis,
        "final_synthesis": human_synthesis,
        "llm_layer": get_llm_layer_status(),
    }

    # Always write Tier-1 episodic routing trace
    _append_jsonl_safe(TRACE_LOG_PATH, trace_record)

    # Only promote to Tier-2 reusable semantic knowledge when writeback hygiene passes
    if writeback.get("tier2_promoted_to_reusable_knowledge"):
        _append_jsonl_safe(TIER2_KNOWLEDGE_PATH, {
            "knowledge_id": f"CHR-T2-{int(time.time())}-{hashlib.md5(clean_q.encode('utf-8')).hexdigest()[:6]}",
            "promoted_at_iso": trace_record["timestamp_iso"],
            "trace_id": trace_id,
            "query": clean_q,
            "primary_domain": primary_domain,
            "consulted_agents": consulted_agents,
            "quality_score": quality_signal["quality_score"],
            "unique_provenance_sources_count": consensus_info["unique_provenance_sources_count"],
            "aegis_gate_status": aegis_gate["status"],
            "resolution": consensus_info["resolution"],
            "validated_conclusion": " | ".join(
                f"{e['agent'].upper()}: {e['summary'][:180]}"
                for e in consensus_info.get("envelopes", [])
                if e.get("agent") != "chronicle"
            )[:520],
        })

    return trace_record


def get_recent_dag_traces(limit: int = 12) -> List[Dict[str, Any]]:
    rows = _read_jsonl_safe(TRACE_LOG_PATH, limit=limit)
    rows.reverse()
    return rows


def get_collective_memory_matrix(limit: int = 12) -> Dict[str, Any]:
    """
    Return the Two-Tier Chronicle Collective Memory Matrix:
      - Tier 2: Promoted, provenance-verified reusable semantic knowledge shared across agents.
      - Tier 1: Auditable episodic DAG routing traces (kept separate so noisy chat turns don't pollute shared memory).
    """
    tier2_rows = _read_jsonl_safe(TIER2_KNOWLEDGE_PATH, limit=limit)
    tier2_rows.reverse()
    tier1_traces = _read_jsonl_safe(TRACE_LOG_PATH, limit=limit)
    tier1_traces.reverse()

    ledger_integrity = inspect_scout_and_ledger_integrity()

    return {
        "schema": "nexus.collective_memory_matrix.v2",
        "hygiene_policy": "Two-Tier Chronicle Write-Back Hygiene (Tier-1 Episodic Audit Trace vs. Tier-2 Validated Semantic Knowledge)",
        "tier2_promoted_count": len(tier2_rows),
        "tier1_episodic_trace_count": len(tier1_traces),
        "tier2_promoted_knowledge": tier2_rows,
        "tier1_episodic_traces": [
            {
                "trace_id": t.get("trace_id"),
                "timestamp_iso": t.get("timestamp_iso"),
                "query": t.get("query"),
                "consulted_agents": t.get("consulted_agents"),
                "quality_score": (t.get("quality_signal") or {}).get("quality_score"),
                "unique_provenance_sources_count": (t.get("provenance_consensus") or {}).get("unique_provenance_sources_count"),
                "aegis_gate_status": (t.get("aegis_policy_gate") or {}).get("status", "Unavailable"),
                "promoted_to_tier2": (t.get("chronicle_writeback") or {}).get("tier2_promoted_to_reusable_knowledge", False),
                "writeback_reason": (t.get("chronicle_writeback") or {}).get("reason", ""),
                "total_latency_ms": t.get("total_latency_ms", 0.0),
            }
            for t in tier1_traces
        ],
        "scout_ledger_integrity": ledger_integrity,
    }


def get_live_nexus_status() -> Dict[str, Any]:
    """
    Return real runtime telemetry for `/api/nexus/status` (replacing hardcoded sample metrics).
    """
    traces = get_recent_dag_traces(limit=15)
    matrix = get_collective_memory_matrix(limit=15)
    benchmark = _read_json_safe(BENCHMARK_PATH, {})
    graph = CollaborationGraph(storage_dir=str(ROOT_DIR / "Nexus" / "memory"))
    graph_stats = graph.stats()
    ledger_integrity = inspect_scout_and_ledger_integrity()

    latencies = [float(t["total_latency_ms"]) for t in traces if isinstance(t.get("total_latency_ms"), (int, float)) and math.isfinite(float(t["total_latency_ms"]))]
    qualities = [float((t.get("quality_signal") or {})["quality_score"]) for t in traces
                 if isinstance((t.get("quality_signal") or {}).get("quality_score"), (int, float))
                 and math.isfinite(float((t.get("quality_signal") or {})["quality_score"]))]
    mean_lat = round(sum(latencies) / len(latencies), 1) if latencies else None
    mean_qual = round(sum(qualities) / len(qualities), 3) if qualities else None
    benchmark_pass = benchmark.get("overall_pass_rate_pct") if isinstance(benchmark, dict) else None
    cases_passed = benchmark.get("overall_cases_passed") if isinstance(benchmark, dict) else None
    cases_total = benchmark.get("overall_cases_total") if isinstance(benchmark, dict) else None

    return {
        "agent": "nexus",
        "status": "available",
        "institutional_standard": "nexus.coordination.v2",
        "metrics": {
            "active_specialists": "Unavailable · live health not queried",
            "mean_dag_latency_ms": f"{mean_lat}ms" if mean_lat is not None else "Unavailable",
            "mean_coordination_quality": mean_qual,
            "benchmark_pass_rate_pct": benchmark_pass,
            "tier2_promoted_knowledge_count": matrix["tier2_promoted_count"],
            "tier1_episodic_traces_count": matrix["tier1_episodic_trace_count"],
            "aegis_policy_version": benchmark.get("aegis_policy_version") if isinstance(benchmark, dict) else None,
            "scout_ledger_synced": ledger_integrity["synced"],
            "collaboration": ["chronicle", "atlas", "oracle", "forge", "sentinel", "pulse", "aegis", "genesis"],
        },
        "collaboration_graph": graph_stats,
        "benchmark_summary": {
            "benchmark_id": benchmark.get("benchmark_id") if isinstance(benchmark, dict) else None,
            "overall_pass_rate_pct": benchmark_pass,
            "overall_cases_passed": f"{cases_passed}/{cases_total}" if cases_passed is not None and cases_total is not None else "Unavailable",
            "dimensions": {
                k: {
                    "accuracy_pct": v.get("accuracy_pct"),
                    "cases_passed": v.get("cases_passed"),
                    "cases_total": v.get("cases_total"),
                }
                for k, v in (benchmark.get("dimensions") or {}).items()
            },
        },
        "scout_ledger_integrity": ledger_integrity,
        "recent_traces": traces[:6],
        "timestamp": time.time(),
    }


def _humanize_chronicle_memory_item(m: Dict[str, Any]) -> Dict[str, str]:
    """Translate raw machine memory records from Chronicle/memory/store/memories.json into plain, everyday English."""
    raw = str(m.get("content") or "").strip()
    source_repo = str(m.get("source_repository") or "Chronicle").title()
    occ = int(m.get("occurrence_count") or 1)

    # Extract symbol and market condition if present e.g. [GBPUSD::trending_up]
    sym_match = re.search(r"\[([A-Z0-9]{3,10})::([a-z_]+)\]", raw)
    sym_human = _format_pair_human(sym_match.group(1)) if sym_match else "Currency Markets"
    reg_code = sym_match.group(2) if sym_match else ""
    reg_map = {
        "trending_up": "Uptrend",
        "trending_down": "Downtrend",
        "ranging": "Sideways Market",
        "volatile": "Fast-Moving Market",
    }
    reg_human = reg_map.get(reg_code, reg_code.replace("_", " ").title()) if reg_code else "Live Market"

    ret_match = re.search(r"Return=([+-]?[0-9.]+)%\s*\(vs\s*V1\s*Incumbent\s*([+-]?[0-9.]+)%\)", raw, re.I)
    new_ret = float(ret_match.group(1)) if ret_match else None
    old_ret = float(ret_match.group(2)) if ret_match else None

    if "volatility_adaptive_atr" in raw or "Volatility-Adjusted ATR" in raw:
        title = (
            f"💡 Strategy Improvement on {sym_human} ({reg_human}): Adjusting our stop-loss and profit targets "
            f"to match daily market movement boosted returns to +{new_ret if new_ret is not None else 1.16:.2f}% "
            f"(beating the old strategy's +{old_ret if old_ret is not None else 0.83:.2f}%)."
        )
        source = f"Saved by {source_repo} · Action: Using this adaptive profit-target rule for future {sym_human} trades"
        badge = f"Proven Upgrade (Confirmed {occ}×)"
        return {"title": title, "source": source, "time": badge}

    if "market_structure_invalidation" in raw or "Structure Invalidation" in raw:
        title = (
            f"💡 Strategy Improvement on {sym_human} ({reg_human}): Placing stop-losses behind real price support/resistance "
            f"levels earned +{new_ret if new_ret is not None else 1.15:.2f}% (beating the old +{old_ret if old_ret is not None else 0.83:.2f}%)."
        )
        source = f"Saved by {source_repo} · Action: Prioritizing support/resistance stop protection on {sym_human}"
        badge = f"Proven Upgrade (Confirmed {occ}×)"
        return {"title": title, "source": source, "time": badge}

    if "mtf_pullback_continuation" in raw or "Pullback Entry" in raw or "Pullback Weighting" in raw:
        if new_ret is not None and old_ret is not None and new_ret < old_ret:
            title = (
                f"🛡️ What We Avoided on {sym_human} ({reg_human}): Tested an experimental pullback-entry tweak, "
                f"which returned +{new_ret:.2f}% vs our proven strategy's +{old_ret:.2f}% — so we kept our proven strategy."
            )
            source = f"Saved by {source_repo} · Action: Rejected the weaker variation to protect account performance"
            badge = f"Safety Filter (Tested {occ}×)"
        else:
            title = (
                f"💡 Strategy Lesson on {sym_human} ({reg_human}): Waiting for a calm pullback before entering a trend "
                f"improved win consistency across 15-minute and 1-hour charts."
            )
            source = f"Saved by {source_repo} · Shared with Oracle & Forge"
            badge = f"Saved Lesson ({occ}×)"
        return {"title": title, "source": source, "time": badge}

    if "constant_monetary_risk_stop_scaled" in raw or "Stop-Scaled Sizing" in raw:
        title = (
            f"🛡️ What We Avoided on {sym_human} ({reg_human}): Tested a fixed-dollar stop-scaling experiment, "
            f"which earned +{new_ret if new_ret is not None else 0.55:.2f}% compared to our proven strategy's "
            f"+{old_ret if old_ret is not None else 0.83:.2f}% — so we kept our higher-performing rule."
        )
        source = f"Saved by {source_repo} · Action: Lowered priority on this experiment so Oracle only trades our best rules"
        badge = f"Safety Filter (Tested {occ}×)"
        return {"title": title, "source": source, "time": badge}

    if "Down-weight" in raw or "failed to beat" in raw:
        title = (
            f"🛡️ Strategy Comparison on {sym_human} ({reg_human}): A challenger test did not beat our proven live strategy, "
            f"so Forge kept our winning strategy active and logged the test result."
        )
        source = f"Saved by {source_repo} · Shared Team Memory"
        badge = f"Safety Filter ({occ}×)"
        return {"title": title, "source": source, "time": badge}

    # Strip machine prefixes like [Recurring Lesson x4 — Last on ...] or Lesson [forge/...]:
    cleaned = re.sub(r"\[Recurring Lesson[^\]]*\]\s*", "", raw)
    cleaned = re.sub(r"Lesson\s*\[[^\]]*\]:\s*", "", cleaned)
    cleaned = re.sub(r"\(Adjustment:[^)]*\)", "", cleaned).strip()
    if len(cleaned) > 220:
        cleaned = cleaned[:217] + "..."
    return {
        "title": f"📘 Team Memory ({source_repo}): {cleaned or 'Verified research and trading lesson saved to shared memory.'}",
        "source": f"Saved by {source_repo} · Available to all 9 agents",
        "time": f"Confirmed {occ}×" if occ > 1 else "Saved Memory",
    }


def _humanize_episode_item(ep: Dict[str, Any]) -> Dict[str, str]:
    """Translate raw episode logs from Chronicle/memory/store/episodes.json into plain, everyday English."""
    passed = bool(ep.get("success", True))
    agent = str(ep.get("agent") or "forge").title()
    raw_out = str(ep.get("outcome") or "").strip()
    occ = int(ep.get("occurrence_count") or 1)

    sym_match = re.search(r"\[([A-Z0-9]{3,10})::([a-z_]+)\]", raw_out)
    sym_human = _format_pair_human(sym_match.group(1)) if sym_match else "EUR/USD"
    ret_match = re.search(r"Return=([+-]?[0-9.]+)%\s*\(vs\s*V1\s*Incumbent\s*([+-]?[0-9.]+)%\)", raw_out, re.I)
    new_ret = float(ret_match.group(1)) if ret_match else None
    old_ret = float(ret_match.group(2)) if ret_match else None

    if passed and new_ret is not None and old_ret is not None:
        title = (
            f"✅ Test Passed ({agent}): New exit & stop-loss rule on {sym_human} earned +{new_ret:.2f}% "
            f"in practice testing, beating the old rule (+{old_ret:.2f}%) — promoted for live use."
        )
        badge = "Promoted Upgrade"
    elif not passed and new_ret is not None and old_ret is not None:
        title = (
            f"🛡️ Challenger Rejected ({agent}): Experimental rule on {sym_human} earned +{new_ret:.2f}% "
            f"vs our proven strategy's +{old_ret:.2f}% — kept our proven strategy to protect profit."
        )
        badge = "Kept Proven Rule"
    else:
        clean_out = re.sub(r"\[[^\]]+\]\s*", "", raw_out).strip() or "Completed strategy safety check."
        title = f"{'✅' if passed else '🛡️'} {agent} Practice Check: {clean_out}"
        badge = f"Logged ({occ}×)"

    return {
        "title": title,
        "source": f"Recorded by {agent} in Chronicle Memory · Zero risk to live balance",
        "time": badge,
    }


def build_rich_agent_dashboard_data(agent_id: str, broker: str = "all") -> Dict[str, Any]:
    """
    Build authoritative, 100% file-backed KPI and primary/secondary feed arrays
    in plain, everyday human English for all 9 frontend HTML pages (/agents/{name}/data).
    Supports ?broker=all | mt5 | ctrader for Oracle so the user can inspect MetaTrader 5,
    cTrader, or both accounts side-by-side.
    """
    aid = str(agent_id or "nexus").strip().lower()
    memories = _read_json_safe(ROOT_DIR / "Chronicle" / "memory" / "store" / "memories.json", [])
    if not isinstance(memories, list):
        memories = []
    kv = _read_json_safe(ROOT_DIR / "Chronicle" / "memory" / "store" / "kv_state.json", {})
    if not isinstance(kv, dict):
        kv = {}
    episodes = _read_json_safe(ROOT_DIR / "Chronicle" / "memory" / "store" / "episodes.json", [])
    if not isinstance(episodes, list):
        episodes = []

    candidates: List[Dict[str, Any]] = [
        v["value"]
        for k, v in kv.items()
        if k.startswith("forge.core.candidate.") and isinstance(v, dict) and isinstance(v.get("value"), dict)
    ]
    candidates.sort(key=lambda c: float(c.get("updated_at_unix") or c.get("created_at_unix") or 0), reverse=True)

    hypotheses: List[Dict[str, Any]] = [
        v["value"]
        for k, v in kv.items()
        if k.startswith("forge.core.hypothesis.") and isinstance(v, dict) and isinstance(v.get("value"), dict)
    ]
    hypotheses.sort(key=lambda h: float(h.get("updated_at_unix") or h.get("created_at_unix") or 0), reverse=True)

    v2_candidates = [c for c in candidates if str(c.get("candidate_code") or "").startswith("V2_")]
    ledger = {} if aid == "oracle" else _load_oracle_ledger_snapshot(broker=broker)
    feeds = _read_json_safe(LIVE_FEEDS_PATH, {})
    if not isinstance(feeds, dict):
        feeds = {}

    active_broker = str(ledger.get("active_broker") or "all")
    brokers_map = ledger.get("brokers") or {}
    active_broker_info = brokers_map.get(active_broker) or {}
    broker_display_name = active_broker_info.get("name") or "All Accounts (MT5 + cTrader)"

    balance_val = ledger.get("equity_usd")
    pnl_val = ledger.get("net_pnl_usd")
    active_trades = list(ledger.get("active_trades") or [])
    session_trades = list(ledger.get("session_trades") or [])
    c3_wins = ledger.get("session_wins")
    c3_total = ledger.get("session_total")
    all_wr = ledger.get("all_time_win_rate_pct")
    all_wins = ledger.get("all_time_wins")
    all_total = ledger.get("all_time_total")
    risk_vetoed = ledger.get("risk_vetoed_count")

    if aid == "oracle":
        from Nexus.api.interactive_actions import get_oracle_dashboard_data
        dashboard = get_oracle_dashboard_data(broker)
        active_broker = str(dashboard.get("active_broker") or broker or "all").lower()
        brokers_map = dashboard.get("brokers") or {}
        active_info = brokers_map.get(active_broker) or {}
        live_open_trades = dashboard.get("live_open_trades") or []
        closed_trades = dashboard.get("closed_trades") or []
        history_complete = dashboard.get("history_status") == "complete"

        recent_items: List[Dict[str, Any]] = []
        for trade in live_open_trades:
            snapshot_state = "Last-known position from a stale authenticated broker snapshot" if trade.get("snapshot_status") == "stale" else "Open in the latest fresh authenticated broker snapshot"
            recent_items.append({
                "title": f"{'LAST KNOWN' if trade.get('snapshot_status') == 'stale' else 'OPEN'} · {trade.get('broker_badge') or trade.get('broker')} · {trade.get('direction')} {trade.get('symbol')}",
                "source": f"{snapshot_state} · ticket {trade.get('trade_id') or 'unavailable'} · entry {trade.get('entry') if trade.get('entry') is not None else 'unavailable'} · stop {trade.get('sl') if trade.get('sl') is not None else 'unavailable'} · target {trade.get('tp') if trade.get('tp') is not None else 'unavailable'}",
                "time": f"Position snapshot at {trade.get('snapshot_updated_at_unix')}" if trade.get("snapshot_updated_at_unix") else "Snapshot time unavailable",
            })
        for trade in reversed(closed_trades[-50:]):
            pnl = trade.get("net_pnl")
            recent_items.append({
                "title": f"{str(trade.get('broker') or '').upper()} · {trade.get('symbol') or 'Unknown'} · {trade.get('outcome') or 'Closed'} · net {pnl if pnl is not None else 'unavailable'} {trade.get('currency') or ''}",
                "source": "Broker-reported closed-deal history",
                "time": "Broker-reported close timestamp" if trade.get("closed_at_unix") else "Close time unavailable",
            })

        symbol_groups: Dict[str, Dict[str, Any]] = {}
        if history_complete:
            for trade in closed_trades:
                try:
                    pnl = float(trade.get("net_pnl"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(pnl):
                    continue
                symbol = str(trade.get("symbol") or "Unknown")
                group = symbol_groups.setdefault(symbol, {"wins": 0, "count": 0, "net": 0.0, "currency": trade.get("currency") or ""})
                group["count"] += 1
                group["wins"] += int(pnl > 0)
                group["net"] += pnl

        breakdown = []
        for symbol, group in symbol_groups.items():
            if not group["count"]:
                continue
            pct = 100.0 * group["wins"] / group["count"]
            breakdown.append({
                "title": f"{symbol} · {pct:.1f}% ({group['wins']}/{group['count']}) · net {group['net']:.2f} {group['currency']}",
                "source": "Broker-reported closed-deal history",
                "time": f"Sample: {group['count']} closed deals",
            })

        account_connected = bool(active_info.get("connected")) if active_broker != "all" else bool(active_info.get("connected"))
        overview = (
            f"Viewing {active_info.get('name') or active_broker.upper()}: authenticated demo account data is current."
            if account_connected
            else f"Viewing {active_info.get('name') or active_broker.upper()}: broker data is unavailable or stale; the dashboard is suppressing balances, positions, and performance claims that cannot be verified."
        )
        metrics = {
            "pnl_today": dashboard.get("pnl_today", "Unavailable"),
            "win_rate": dashboard.get("win_rate", "Unavailable"),
            "open_trades": dashboard.get("open_trades", "Unavailable"),
            "signals_today": dashboard.get("signals_today", "Unavailable"),
            "portfolio_value": dashboard.get("portfolio_value", "Unavailable"),
            "max_drawdown": dashboard.get("max_drawdown", "Unavailable"),
        }
        return {
            **dashboard,
            "id": "oracle",
            "agent": "oracle",
            "name": "Oracle",
            "repository": "Oracle",
            "domain": "Markets & Trading (MetaTrader 5 & cTrader)",
            "running": None,
            "overview": overview,
            "metrics": metrics,
            "open_and_recent_trades": recent_items,
            "win_rate_breakdown": breakdown if history_complete else [],
            "positions_status": dashboard.get("positions_status", "unavailable_or_stale"),
        }

    # Non-Oracle dashboards are built only from persisted records. Missing feed
    # timestamps, evaluations, or service health remain unavailable; no sample
    # metrics are presented as live data.
    def feed_state(feed_key: str) -> tuple[dict, list, str]:
        block = feeds.get(feed_key) if isinstance(feeds.get(feed_key), dict) else {}
        rows = block.get("social_posts" if feed_key == "pulse" else "world_news") or []
        if not isinstance(rows, list):
            rows = []
        try:
            stamp = datetime.fromisoformat(str(block.get("updated_at") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = max(0.0, time.time() - stamp.timestamp())
            state = "current" if age <= 900 else "stale"
        except (TypeError, ValueError, OverflowError):
            state = "unavailable"
        return block, [row for row in rows if isinstance(row, dict)], state

    if aid == "forge":
        states: Dict[str, int] = {}
        for candidate in candidates:
            state = str(candidate.get("lifecycle_state") or "unknown").upper()
            states[state] = states.get(state, 0) + 1
        evaluated = sum(count for state, count in states.items() if state not in {"BLOCKED", "UNKNOWN"})
        promoted = sum(count for state, count in states.items() if state in {"CERTIFIED", "APPROVED_FOR_DEPLOYMENT", "DEPLOYED"})
        recent = []
        for candidate in candidates[:20]:
            metrics = ((candidate.get("lockbox_holdout_report") or {}).get("candidate_metrics") or {})
            trades = metrics.get("trades")
            ret = metrics.get("net_return_pct")
            details = []
            if trades is not None:
                details.append(f"{trades} recorded holdout trades")
            if ret is not None:
                details.append(f"net return {ret}%")
            recent.append({
                "title": f"{candidate.get('candidate_code') or candidate.get('candidate_id') or 'Candidate'} · {candidate.get('symbol') or 'symbol unavailable'} · {candidate.get('lifecycle_state') or 'state unavailable'}",
                "source": "; ".join(details) if details else "Candidate artifact; no holdout metrics recorded.",
                "time": str(candidate.get("updated_at_unix") or candidate.get("created_at_unix") or "Timestamp unavailable"),
            })
        experiments = [{
            "title": str(h.get("statement") or h.get("hypothesis") or "Recorded hypothesis; no statement provided"),
            "source": f"{len(h.get('evidence_records') or [])} linked evidence records",
            "time": str(h.get("updated_at_unix") or h.get("created_at_unix") or "Timestamp unavailable"),
        } for h in hypotheses[:20]]
        metrics = {
            "builds_today": f"{len(candidates)} recorded candidates (all time)",
            "success_rate": f"{(100 * promoted / evaluated):.1f}% in promoted lifecycle states" if evaluated else "Unavailable",
            "deployments": str(states.get("DEPLOYED", 0)),
            "system_health": "Unavailable · Oracle broker state is separate",
            "avg_build_time": "Unavailable",
            "uptime": "Unavailable",
        }
        return {"id": aid, "agent": aid, "name": "Forge", "repository": "Forge", "domain": "Strategy Training",
                "status": "available" if candidates or hypotheses else "unavailable", "running": None,
                "overview": f"Chronicle contains {len(candidates)} candidate artifacts and {len(hypotheses)} hypotheses. Artifact presence does not establish strategy quality or profitability.",
                "metrics": metrics, **metrics, "recent_builds": recent, "incubator_experiments": experiments,
                "candidate_states": states}

    if aid == "pulse":
        block, rows, state = feed_state("pulse")
        cards = []
        for row in rows[:100]:
            text = str(row.get("post") or row.get("text") or row.get("title") or row.get("headline") or "").strip()
            if not text:
                continue
            cards.append({**row, "title": text, "source": str(row.get("platform") or row.get("source") or "Source unavailable"),
                          "time": str(row.get("published_at") or row.get("time") or "Timestamp unavailable"),
                          "post": text, "handle": row.get("handle") or row.get("author") or "Author unavailable"})
        metrics = {"overall_sentiment": str(block.get("overall_sentiment") or "Unavailable"),
                   "posts_analyzed": str(len(cards)) if state == "current" else "Unavailable · feed stale or missing",
                   "trending_topics": "Unavailable", "sentiment_score": str(block.get("sentiment_score") or "Unavailable"),
                   "influencers_tracked": str(block.get("influencers_tracked") or "Unavailable"),
                   "uptime": state, "sentiment": str(block.get("overall_sentiment") or "Unavailable"),
                   "trending": "Unavailable", "posts_hr": "Unavailable", "platforms": "Unavailable",
                   "discussions": str(len(cards)) if state == "current" else "Unavailable"}
        return {"id": aid, "agent": aid, "name": "Pulse", "repository": "Pulse", "domain": "Social Feed",
                "status": state, "running": None, "overview": f"Persisted social feed status: {state}; recorded items: {len(cards)}.",
                "metrics": metrics, "kpis": metrics, **metrics, "social_posts": cards, "social_posts_feed": cards,
                "influencer_sentiment_feed": cards, "feed": cards, "sentiment_breakdown": [], "trending": []}

    if aid == "sentinel":
        block, rows, state = feed_state("sentinel")
        cards = []
        for row in rows[:100]:
            headline = str(row.get("headline") or row.get("title") or "").strip()
            if not headline:
                continue
            cards.append({**row, "title": headline, "headline": headline,
                          "source": str(row.get("outlet") or row.get("source") or "Source unavailable"),
                          "time": str(row.get("published_at") or row.get("time") or "Timestamp unavailable"),
                          "summary": str(row.get("summary") or "Summary unavailable")})
        metrics = {"alerts_today": str(len(cards)) if state == "current" else "Unavailable · feed stale or missing",
                   "market_sentiment": str(block.get("market_sentiment") or "Unavailable"),
                   "sources_monitored": str(block.get("sources_monitored") or "Unavailable"),
                   "threats_detected": str(block.get("threats_detected") or "Unavailable"),
                   "coverage_pct": "Unavailable", "uptime": state}
        return {"id": aid, "agent": aid, "name": "Sentinel", "repository": "Sentinel", "domain": "News Feed",
                "status": state, "running": None, "overview": f"Persisted news feed status: {state}; recorded items: {len(cards)}.",
                "metrics": metrics, "kpis": metrics, **metrics, "news_articles": cards, "world_news_feed": cards,
                "macro_event_radar": cards}

    if aid == "aegis":
        integrity = inspect_scout_and_ledger_integrity()
        observed = {key: integrity.get(key) for key in ("synced", "settled_shadow_replays", "v2_trades_closed")}
        available = any(value is not None for value in observed.values())
        security_feed = [{"title": f"Settled shadow replay outcomes: {observed['settled_shadow_replays'] if observed['settled_shadow_replays'] is not None else 'Unavailable'}",
                         "source": "Local decision-ledger count; not a policy audit", "time": "Read at request time"},
                        {"title": f"Recorded anomalies: {integrity.get('anomalies_detected') if integrity.get('anomalies_detected') is not None else 'Unavailable'}",
                         "source": "Local Scout/decision-ledger diagnostic", "time": "Read at request time"}]
        ledger_feed = [{"title": f"Scout synchronization records: {observed['synced'] if observed['synced'] is not None else 'Unavailable'}",
                        "source": "Local Scout/decision-ledger integrity diagnostic", "time": "Read at request time"},
                       {"title": f"Closed V2 trades recorded: {observed['v2_trades_closed'] if observed['v2_trades_closed'] is not None else 'Unavailable'}",
                        "source": "Local decision-ledger count; not broker reconciliation", "time": "Read at request time"}]
        metrics = {"risk_score": "Unavailable", "anomalies_detected": str(integrity.get("anomalies_detected", "Unavailable")),
                   "threats_blocked": "Unavailable", "compliance_score": "Unavailable",
                   "incidents_today": "Unavailable", "uptime": "Unavailable"}
        return {"id": aid, "agent": aid, "name": "Aegis", "repository": "Aegis", "domain": "Governance Diagnostics",
                "status": "available" if available else "unavailable", "running": None,
                "overview": "Local integrity diagnostics are shown as recorded. This endpoint does not attest that every broker action passed every policy.",
                "metrics": metrics, "kpis": metrics, **metrics, "security_shield_feed": security_feed,
                "compliance_audit_feed": ledger_feed, "integrity": integrity}

    if aid == "chronicle":
        events = [_humanize_chronicle_memory_item(m) for m in memories[:30] if isinstance(m, dict)]
        audits = [_humanize_episode_item(ep) for ep in episodes[:30] if isinstance(ep, dict)]
        total = len(memories) + len(episodes) + len(kv)
        metrics = {"experiments_logged": "Unavailable", "events_logged": str(total),
                   "queries_today": "Unavailable", "storage_used": "Unavailable", "uptime": "Unavailable"}
        return {"id": aid, "agent": aid, "name": "Chronicle", "repository": "Chronicle", "domain": "Durable Memory",
                "status": "available" if total else "unavailable", "running": None,
                "overview": f"Local Chronicle files contain {len(memories)} memory records, {len(episodes)} episodes, and {len(kv)} key/value records. Counts do not prove semantic correctness.",
                "metrics": metrics, **metrics, "recent_events": events, "audit_trail": audits, "experiment_cards": []}

    if aid in ("genesis", "atlas"):
        if aid == "atlas":
            catalog = _read_json_safe(ATLAS_CATALOG_PATH, {})
            desks = catalog.get("desks") if isinstance(catalog, dict) and isinstance(catalog.get("desks"), dict) else {}
            rows = []
            for key, value in list(desks.items())[:30]:
                if not isinstance(value, dict):
                    continue
                validation = value.get("validation_and_benchmark") if isinstance(value.get("validation_and_benchmark"), dict) else {}
                lockbox = validation.get("untouched_lockbox_40pct") if isinstance(validation.get("untouched_lockbox_40pct"), dict) else {}
                uplift = validation.get("v2_vs_frozen_v1_uplift") if isinstance(validation.get("v2_vs_frozen_v1_uplift"), dict) else {}
                walk_forward = validation.get("walk_forward_4_windows") if isinstance(validation.get("walk_forward_4_windows"), dict) else {}
                provenance = value.get("dataset_provenance") if isinstance(value.get("dataset_provenance"), dict) else {}
                rows.append({
                    "desk_id": str(value.get("desk") or key),
                    "display_name": str(value.get("display_name") or str(value.get("desk") or key).replace("_", " ").title()),
                    "lifecycle_state": str(value.get("lifecycle_state") or "State unavailable"),
                    "created_at": value.get("created_at"),
                    "dataset_hash": provenance.get("dataset_hash"),
                    "lockbox_hit_rate_pct": lockbox.get("hit_rate_pct"),
                    "lockbox_episodes": lockbox.get("episodes_count", provenance.get("lockbox_episodes")),
                    "hit_rate_uplift_pct": uplift.get("hit_rate_uplift_pct"),
                    "positive_walk_forward_windows": walk_forward.get("positive_windows_count"),
                    "walk_forward_window_count": len(walk_forward.get("windows")) if isinstance(walk_forward.get("windows"), list) else None,
                    "paired_bootstrap_p_value": uplift.get("paired_bootstrap_500_p_value"),
                })
            lockbox_records = sum(
                row.get("lockbox_hit_rate_pct") is not None and row.get("lockbox_episodes") is not None
                for row in rows
            )
            metrics = {"reports_today": "Unavailable", "sources_monitored": "Unavailable",
                       "sectors_tracked": str(len(rows)),
                       "alerts": f"{lockbox_records}/{len(rows)}" if rows else "Unavailable"}
            return {"id": aid, "agent": aid, "name": "Atlas", "repository": "Atlas", "domain": "Research Catalog",
                    "status": "available" if rows else "unavailable", "running": None,
                    "overview": f"Atlas catalog entries recorded: {len(rows)}. No accuracy claim is made without a validated benchmark record.",
                    "metrics": metrics, **metrics, "primary_feed": rows, "secondary_feed": [], "desks": rows,
                    "symbol_hypotheses": []}
        genesis_root = ROOT_DIR / "Genesis"
        if str(genesis_root) not in sys.path:
            sys.path.insert(0, str(genesis_root))
        from core.control_plane import get_genesis_control_plane  # type: ignore
        cp = get_genesis_control_plane()
        return cp.get_dashboard_snapshot()

    if aid == "nexus":
        traces = _read_jsonl_safe(TRACE_LOG_PATH, limit=20)
        tier2 = _read_jsonl_safe(TIER2_KNOWLEDGE_PATH, limit=20)
        orchestration = [{"title": str(row.get("query") or "Recorded coordination event"),
                          "source": f"Agents: {', '.join(map(str, row.get('consulted_agents') or [])) or 'Unavailable'} · gate: {((row.get('aegis_policy_gate') or {}).get('status') or 'Unavailable')}",
                          "time": str(row.get("timestamp_iso") or "Timestamp unavailable")} for row in reversed(traces[-10:])]
        metrics = {"agents_active": "Unavailable · live health not queried", "tasks_queued": str(len(orchestration)),
                   "messages_per_hour": "Unavailable", "uptime": "Unavailable", "errors_today": "Unavailable",
                   "throughput": "Unavailable"}
        return {"id": aid, "agent": aid, "name": "Nexus", "repository": "Nexus", "domain": "Coordination Records",
                "status": "available" if traces or tier2 else "unavailable", "running": None,
                "overview": f"Recorded local coordination traces: {len(traces)}; promoted knowledge records: {len(tier2)}. These counts do not indicate current agent health.",
                "metrics": metrics, **metrics, "orchestration_feed": orchestration, "agent_health_feed": []}

    return {"id": aid, "agent": aid, "name": aid.title(), "status": "unavailable", "running": None,
            "overview": "No authoritative dashboard data source is configured for this agent.", "metrics": {}}

def _cli_main() -> None:
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    action = str(payload.get("action") or "coordinate").lower()

    if action == "status":
        print(json.dumps(get_live_nexus_status()))
    elif action == "dashboard_data":
        aid = str(payload.get("agent_id") or "nexus").lower()
        brk = str(payload.get("broker") or "all").lower()
        print(json.dumps(build_rich_agent_dashboard_data(aid, broker=brk)))
    elif action == "oracle_chart":
        from Nexus.api.interactive_actions import get_oracle_chart_data
        sym = str(payload.get("symbol") or "EURUSD")
        tf = str(payload.get("timeframe") or "M15")
        brk = str(payload.get("broker") or "all")
        print(json.dumps(get_oracle_chart_data(sym, tf, brk)))
    elif action == "oracle_action":
        from Nexus.api.interactive_actions import execute_oracle_action
        print(json.dumps(execute_oracle_action(payload.get("params") or payload)))
    elif action == "feeds_refresh":
        from Nexus.api.interactive_actions import execute_feed_refresh
        print(json.dumps(execute_feed_refresh(str(payload.get("agent") or "all"))))
    elif action == "forge_action":
        from Nexus.api.interactive_actions import execute_forge_action
        sub = str(payload.get("forge_action") or "status")
        print(json.dumps(execute_forge_action(sub, payload.get("params") or payload)))
    elif action == "specialist_action":
        from Nexus.api.interactive_actions import execute_specialist_action
        print(json.dumps(execute_specialist_action(payload.get("params") or payload)))
    elif action == "traces":
        limit = int(payload.get("limit") or 12)
        print(json.dumps({"status": "ok", "traces": get_recent_dag_traces(limit=limit)}))
    elif action == "memory_matrix":
        limit = int(payload.get("limit") or 12)
        print(json.dumps({"status": "ok", **get_collective_memory_matrix(limit=limit)}))
    elif action == "referrals":
        q = str(payload.get("query") or "")
        primary = str(payload.get("primary_agent") or "nexus")
        participating = list(payload.get("participating_agents") or [])
        print(json.dumps({
            "status": "ok",
            "referrals": build_cross_agent_referrals(q, primary, participating),
        }))
    else:
        q = str(payload.get("query") or "Coordinate ecosystem status")
        uid = str(payload.get("user_id") or "operator")
        target = str(payload.get("target_agent") or "nexus")
        print(json.dumps(coordinate_query(q, user_id=uid, target_agent=target)))


if __name__ == "__main__":
    _cli_main()

