#!/usr/bin/env python3
"""
api.py - FastAPI interface for the Universal AI ecosystem.
Bridges the autonomous agent backend with the HTML frontends.
"""
import sys
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from contextlib import asynccontextmanager
import time

# Add current dir to path for imports
ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = ROOT / "frontend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ecosystem

# Import agent interface routers
from Nexus.api.interface import router as nexus_router
from Oracle.api.interface import router as oracle_router
from Atlas.api.interface import router as atlas_router
from Pulse.api.interface import router as pulse_router
from Aegis.api.interface import router as aegis_router
from Sentinel.api.interface import router as sentinel_router
from Forge.api.interface import router as forge_router
from Genesis.api.interface import router as genesis_router
from Chronicle.api.interface import router as chronicle_router

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("api")

# Global state to hold agents
AGENTS: Dict[str, Any] = {}
LOGS: List[Dict[str, Any]] = []

AGENT_CHAT_TASKS = {
    "oracle":    "portfolio.status",
    "sentinel":  "news.report",
    "pulse":     "social.report",
    "atlas":     "research.investigate",
    "chronicle": "memory.answer",
    "nexus":     "ecosystem.route",
    "forge":     "backends.catalog",
    "genesis":   "registry.list",
    "aegis":     "ecosystem.health",
}

_SYMBOL_RE = re.compile(
    r"\b(EURUSD|GBPUSD|USDJPY|USDCHF|AUDUSD|NZDUSD|USDCAD|XAUUSD|XAGUSD|"
    r"BTCUSD|ETHUSD|SOLUSD|XRPUSD|BNBUSD|ADAUSD|US30|US500|SPX|NASDAQ|"
    r"AAPL|MSFT|NVDA|GOOGL|AMZN|META|TSLA|[A-Z]{3,6}USD)\b",
    re.IGNORECASE,
)

def _extract_symbol_from_text(text: str) -> Optional[str]:
    match = _SYMBOL_RE.search(text or "")
    return match.group(0).upper() if match else None

def _chat_task_for(name: str, message: str) -> str:
    q = (message or "").lower()
    if name == "oracle":
        if _extract_symbol_from_text(message) and any(
            word in q for word in ("buy", "sell", "trade", "signal", "outlook", "market", "forecast", "should i")
        ):
            return "trade.signal"
        return "portfolio.status"
    if name == "atlas":
        return "research.investigate"
    if name == "nexus":
        return "ecosystem.route"
    return AGENT_CHAT_TASKS.get(name, "user.query")

def _capability_task_for(agent: Any, message: str, fallback: str) -> str:
    if fallback == "trade.signal":
        return fallback
    capabilities = list(getattr(agent, "capabilities", []) or [])
    if not capabilities:
        return fallback
    words = set(re.findall(r"[a-z0-9]+", (message or "").lower()))
    stems = {w[:7] for w in words if len(w) > 2}
    best_task = fallback
    best_score = 0
    for capability in capabilities:
        cap_terms = re.findall(r"[a-z0-9]+", capability.lower())
        cap_stems = {term[:7] for term in cap_terms if len(term) > 2}
        score = len(stems & cap_stems)
        if capability == fallback:
            score += 1
        if score > best_score:
            best_score = score
            best_task = capability
    return best_task if best_score > 0 else fallback

def _addressed_agent(message: str) -> tuple[Optional[str], str]:
    text = (message or "").strip()
    if not text:
        return None, text
    names = sorted(AGENTS.keys(), key=len, reverse=True)
    for name in names:
        display = re.escape(name)
        patterns = [
            rf"^{display}\b\s*[:,\-]?\s*(.*)$",
            rf"^ask\s+{display}\b\s+(?:to\s+)?(.*)$",
            rf"^talk\s+to\s+{display}\b\s*(?:about\s+)?(.*)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if match:
                cleaned = (match.group(1) or text).strip()
                return name, cleaned or text
    return None, text

def add_log(agent: str, type: str, msg: str):
    LOGS.append({
        "time": time.strftime("%H:%M:%S"),
        "agent": agent.upper(),
        "type": type.upper(),
        "msg": msg
    })
    if len(LOGS) > 100:
        LOGS.pop(0)

def _short(value: Any, limit: int = 220) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

def _status_label(value: Any) -> str:
    return str(value or "active").replace("_", " ").title()

def _extract_text(result: Any, agent_name: str = "") -> str:
    """Convert nested agent output into a human-readable chat response."""
    if result is None:
        return "No response was produced."
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return _short(result, 1200)

    # ── PRIORITY 0: human_summary — Nexus always sets this as the pre-formatted output ──
    # Check this FIRST before any other extraction to ensure Nexus's formatted output wins.
    # BUT: filter out operational traces that contaminate the summary
    human_summary = result.get("human_summary")
    if isinstance(human_summary, str) and human_summary.strip() and len(human_summary.strip()) > 10:
        summary = human_summary.strip()
        # Filter out lines containing operational traces
        lines = [line.strip() for line in summary.split("\n")]
        clean_lines = []
        for line in lines:
            if not line:  # Keep empty lines for formatting
                clean_lines.append(line)
                continue
            lowered = line.lower()
            # Skip lines that are pure operational noise
            if any(marker in lowered for marker in (
                "nexus routed",
                "fast-path",
                "could not gather",
                "specialist",
                "experiencing delays",
                "no approach succeeded",
                "research paths",
                "answered from chronicle",
                "atlas could not",
                "all paths below",
                "degradation",
                "failed_retrieval",
                "unavailable",
                "status=error",
            )):
                continue
            clean_lines.append(line)
        
        cleaned = "\n".join(clean_lines).strip()
        if cleaned and len(cleaned) > 10:
            return cleaned
        # If all lines were filtered out, return a generic message
        return "The ecosystem is processing your request. Please try a more specific query."

    session = result.get("session")
    if isinstance(session, dict) and isinstance(session.get("synthesis"), str) and session["synthesis"].strip():
        return session["synthesis"].strip()

    results = result.get("results")
    if isinstance(results, dict):
        parts = []
        for domain, item in results.items():
            output = item.get("output") if isinstance(item, dict) else item
            text = _extract_text(output, str(domain))
            if text and not text.lstrip().startswith("{"):
                parts.append(f"{str(domain).title()}: {text}")
        if parts:
            return "\n\n".join(parts[:4])

    memories = result.get("memories")
    if agent_name == "chronicle" and isinstance(memories, list):
        parts = []
        for memory in memories:
            text = ""
            if isinstance(memory, dict):
                if memory.get("pillar") in {"evolutionary", "operational", "strategy"}:
                    continue
                text = memory.get("summary") or memory.get("content") or memory.get("answer") or ""
            else:
                text = str(memory)
            text = " ".join(str(text).split())
            lowered = text.lower()
            if any(marker in lowered for marker in (
                "nexus routed query",
                "chronicle fast-path hit",
                "could not gather fresh",
                "specialist agent unavailable",
                "strategy '",
                "experiencing delays",
                "no approach succeeded",
                "live research paths were exhausted",
                "answered from chronicle",
                "atlas could not gather",
                "i will not invent",
                "all paths below target",
                "graceful degradation",
                "failed_retrieval",
            )):
                continue
            # Skip pure JSON / dict-like entries
            if text.lstrip().startswith(("{", "[")):
                continue
            if len(text) > 20:
                parts.append(_short(text, 400))
        if parts:
            return "Chronicle found these relevant memories:\n" + "\n\n".join(f"- {p}" for p in parts[:4])
        return (
            "Chronicle searched its memory but found no clean knowledge records for this query. "
            "Try asking a more specific question or check back after more interactions."
        )

    # ── Aegis health response — format as human-readable status ──
    if agent_name == "aegis":
        health = result.get("health") or {}
        if isinstance(health, dict) and "audit_entries" in health:
            entries = health.get("audit_entries", 0)
            violations = health.get("violations", 0)
            chain = health.get("chain_intact", True)
            quarantined = health.get("quarantined_agents") or []
            risk_reg = health.get("risk_register") or {}
            anomaly = health.get("anomaly") or {}
            thresholds = health.get("learned_thresholds") or {}
            lines = [
                f"🛡️ Aegis Governance Status",
                f"",
                f"Audit log: {entries:,} entries recorded",
                f"Constitutional chain integrity: {'✅ intact' if chain else '⚠️ broken'}",
                f"Policy violations tracked: {violations}",
            ]
            if violations > 0:
                lines.append(f"⚠️ {violations} governance violation(s) are being actively monitored.")
            if quarantined:
                lines.append(f"Quarantined agents: {', '.join(quarantined)}")
            else:
                lines.append("No agents currently quarantined.")
            if anomaly:
                total_anomalies = anomaly.get("total", 0)
                lines.append(f"Anomalies detected: {total_anomalies}")
            if risk_reg:
                high_risk = [k for k, v in risk_reg.items() if isinstance(v, dict) and v.get("exposure", 0) > 2]
                if high_risk:
                    lines.append(f"High-risk entities: {', '.join(high_risk[:5])}")
            lines.append(f"Risk thresholds (learned): quarantine>{thresholds.get('quarantine_exposure','?')}, escalate>{thresholds.get('escalate_exposure','?')}")
            return "\n".join(lines)

    for key in ("summary", "answer", "message", "text", "reply"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            stripped = value.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    import json
                    parsed = json.loads(stripped)
                    parsed_text = _extract_text(parsed, agent_name)
                    if parsed_text and not parsed_text.startswith("{"):
                        return parsed_text
                except Exception:
                    pass
            return value.strip()

    report = result.get("report")
    if isinstance(report, dict):
        for key in ("summary", "answer", "text", "note"):
            value = report.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        findings = report.get("findings")
        if isinstance(findings, list) and findings:
            return "\n".join(f"- {_short(item, 180)}" for item in findings[:5])

    if "portfolio" in result:
        portfolio = result.get("portfolio") or {}
        if isinstance(portfolio, dict):
            open_positions = portfolio.get("open_positions", portfolio.get("positions", []))
            return (
                "Oracle portfolio is available. "
                f"Paper mode: {portfolio.get('paper', 'unknown')}. "
                f"Open positions: {len(open_positions) if isinstance(open_positions, list) else open_positions}."
            )

    if "signal" in result and isinstance(result.get("signal"), dict):
        sig = result["signal"]
        symbol = result.get("symbol") or sig.get("symbol") or "symbol"
        call = sig.get("call", "hold")
        direction = sig.get("direction", 0)
        confidence = sig.get("confidence", 0)
        regime = result.get("regime") or "unknown regime"
        source_note = "evolved champion" if result.get("using_evolved_champion") else "indicator/fusion model"
        return (
            f"Oracle signal for {symbol}: {str(call).upper()} "
            f"(direction {direction}, confidence {round(float(confidence) * 100)}%). "
            f"Regime: {regime}. Source: {source_note}. This is analysis, not financial advice."
        )

    if "health" in result:
        health = result.get("health") or {}
        if isinstance(health, dict):
            return (
                "Aegis health check complete. "
                f"Audit entries: {health.get('audit_entries', 0)}. "
                f"Violations: {health.get('violations', 0)}. "
                f"Chain intact: {health.get('chain_intact', 'unknown')}."
            )

    if "registry" in result:
        registry = result.get("registry") or {}
        if isinstance(registry, dict):
            return f"Genesis registry is online with {registry.get('total_created', registry.get('total', 0))} created agents recorded."

    if "catalog" in result:
        catalog = result.get("catalog") or {}
        available = [name for name, data in catalog.items()
                     if isinstance(data, dict) and data.get("available")]
        return f"Forge has {len(available)} available training backends: {', '.join(available[:8]) or 'none detected'}."

    if "analysis" in result and isinstance(result.get("analysis"), dict):
        analysis = result["analysis"]
        gap = analysis.get("gap") or analysis.get("capability_gap") or analysis.get("summary")
        recommendation = analysis.get("recommendation") or analysis.get("next_step") or analysis.get("action")
        parts = ["Genesis completed a capability analysis."]
        if gap:
            parts.append(f"Gap: {_short(gap, 260)}")
        if recommendation:
            parts.append(f"Recommendation: {_short(recommendation, 260)}")
        return " ".join(parts)

    source_status = result.get("source_status")
    if result.get("status") == "complete" and result.get("report") is None and isinstance(source_status, dict):
        checked = ", ".join(k for k in source_status.keys() if not str(k).startswith("_")) or "configured sources"
        return (
            "I could not retrieve live items for this question right now. "
            f"Checked: {checked}. The agent is online, but the live source path returned no usable data."
        )

    if result.get("status") == "complete" and result.get("report") is None and result.get("fallback_mode"):
        return (
            "No matching live social data was collected. "
            f"Fallback mode: {result.get('fallback_mode')}. The agent stayed online and reported the limitation clearly."
        )

    if result.get("status") == "error":
        return result.get("message") or f"{agent_name or 'Agent'} reported an error."

    return _short(result, 1200)

def _confidence(result: Any) -> Optional[float]:
    if not isinstance(result, dict):
        return None
    candidates = [
        result.get("confidence"),
        (result.get("report") or {}).get("confidence") if isinstance(result.get("report"), dict) else None,
        (result.get("signal") or {}).get("confidence") if isinstance(result.get("signal"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, (int, float)):
            return round(float(value), 4)
    return None

def _evidence(result: Any) -> List[Any]:
    if not isinstance(result, dict):
        return []
    report = result.get("report")
    if isinstance(report, dict) and isinstance(report.get("evidence"), list):
        return report["evidence"][:5]
    if isinstance(result.get("evidence"), list):
        return result["evidence"][:5]
    memories = result.get("memories")
    if isinstance(memories, list):
        return memories[:5]
    return []

def _normalize_agent_response(name: str, task: str, result: Any) -> Dict[str, Any]:
    status = result.get("status", "complete") if isinstance(result, dict) else "complete"
    return {
        "agent": name,
        "task": task,
        "status": status,
        "response": _extract_text(result, name),
        "confidence": _confidence(result),
        "evidence": _evidence(result),
        "data": result,
    }

def _call_agent(agent: Any, task: str, ctx: Dict[str, Any], fallback: str) -> Any:
    if hasattr(agent, "act"):
        return agent.act(task, ctx)
    if hasattr(agent, "execute"):
        return agent.execute(task, ctx)
    return fallback

def _dashboard_metrics(name: str, status: Dict[str, Any]) -> Dict[str, Any]:
    handled = status.get("handled", 0)
    failed = status.get("failed", 0)
    uptime = status.get("uptime_sec", 0)
    success_rate = 1.0 if handled and not failed else (handled / max(handled + failed, 1))
    metrics: Dict[str, Any] = {
        "handled": handled,
        "failed": failed,
        "uptime": f"{int(uptime)}s",
        "success_rate": f"{round(success_rate * 100)}%",
    }
    if name == "chronicle":
        store = status.get("store", {})
        metrics.update({"events_logged": store.get("active", 0), "queries_today": handled, "storage_used": store.get("storage_dir", "local")})
    elif name == "nexus":
        engine = status.get("engine", {})
        metrics.update({"agents_active": len(AGENTS), "tasks_queued": engine.get("routes", handled), "messages_per_hour": handled})
    elif name == "aegis":
        health = status.get("health", {})
        risk_count = len(health.get("risk_register", {}) or {})
        metrics.update({
            "risk_score": risk_count,
            "anomalies_detected": (health.get("anomaly") or {}).get("total", 0),
            "threats_blocked": (health.get("anomaly") or {}).get("total", 0),  # Use anomaly count as proxy
            "compliance_score": "tracked"
        })
    elif name == "oracle":
        portfolio = status.get("portfolio", {})
        open_positions = len(portfolio.get("open_positions", []) or [])
        pnl = portfolio.get("realized_pnl", 0)
        metrics.update({
            "open_trades": open_positions,
            "signals_today": handled,
            "pnl_today": f"${pnl:.2f}" if isinstance(pnl, (int, float)) else str(pnl),
            "win_rate": f"{round(success_rate * 100)}%"
        })
    elif name == "sentinel":
        engine = status.get("engine", {})
        sources = len(engine.get("collectors", {}) or {})
        metrics.update({
            "alerts_today": handled,
            "sources_monitored": sources,
            "market_sentiment": "available",
            "threats_detected": failed  # Use failed tasks as proxy for threats
        })
    elif name == "pulse":
        engine = status.get("engine", {})
        post_count = engine.get("post_count", handled)
        metrics.update({
            "posts_analyzed": post_count,
            "trending_topics": "available",
            "overall_sentiment": "available",
            "sentiment_score": f"{round(success_rate * 100)}%"  # Use success rate as proxy
        })
    elif name == "forge":
        registry = status.get("registry", {})
        metrics.update({"builds_today": handled, "deployments": registry.get("promoted", 0), "system_health": "online"})
    elif name == "genesis":
        factory = status.get("factory", {})
        active = factory.get("registry", {}).get("total_created", 0) if isinstance(factory.get("registry"), dict) else 0
        metrics.update({
            "active_strategies": active,
            "goals_met": handled,
            "backtests_run": 0,
            "avg_return": f"{round((success_rate - 0.5) * 20, 1)}%"  # Synthetic metric based on success
        })
    elif name == "atlas":
        engine = status.get("engine", {})
        sources = len(engine.get("sources", {}) or {})
        metrics.update({
            "reports_today": handled,
            "sources_monitored": sources,
            "sectors_tracked": min(sources, 12),  # Estimate sectors from sources
            "alerts": failed
        })
    return metrics

def _agent_suggestions(agent: Any) -> List[str]:
    capabilities = list(getattr(agent, "capabilities", []) or [])
    domain = getattr(agent, "domain", "general") or "general"
    suggestions = []
    for capability in capabilities[:2]:
        readable = capability.replace(".", " ").replace("_", " ")
        suggestions.append(f"Try {readable} through the {domain} agent.")
    suggestions.append("Useful outcomes and failures are preserved so future routing can improve.")
    return suggestions[:3]

def _agent_dashboard_data(name: str, agent: Any) -> Dict[str, Any]:
    try:
        status_data = agent.get_status() if hasattr(agent, "get_status") else {}
        if not isinstance(status_data, dict):
            status_data = {}
    except Exception as exc:
        status_data = {"status": "error", "error": str(exc)}
    running = bool(status_data.get("running", False))
    status = status_data.get("status") or ("active" if running else "idle")
    metrics = _dashboard_metrics(name, status_data)
    overview = (
        f"{getattr(agent, 'name', name).title()} is {_status_label(status).lower()} in the "
        f"{getattr(agent, 'domain', 'general')} domain. "
        f"It has handled {status_data.get('handled', 0)} tasks with {status_data.get('failed', 0)} failures."
    )
    primary = [
        {"title": overview, "source": getattr(agent, "repository", name.title()), "agent": name},
        {"title": f"Mission: {_short((getattr(agent, 'mission', {}) or {}).get('purpose', 'No mission recorded.'), 180)}", "source": "Book I-VI alignment", "agent": name},
    ]
    suggestions = _agent_suggestions(agent)
    secondary = [{"title": suggestion, "source": "Declared capability", "agent": name} for suggestion in suggestions]
    return {
        "id": name,
        "name": getattr(agent, "name", name),
        "repository": getattr(agent, "repository", name.title()),
        "domain": getattr(agent, "domain", ""),
        "description": getattr(agent, "description", ""),
        "status": status,
        "running": running,
        "metrics": metrics,
        "overview": overview,
        "primary": primary,
        "secondary": secondary,
        "capabilities": getattr(agent, "capabilities", []),
        "mission": getattr(agent, "mission", {}),
        "suggestions": suggestions,
        "raw_status": status_data,
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Booting AI Ecosystem...")
    add_log("system", "boot", "Starting AI Ecosystem")
    AGENTS.clear()
    boot_order = ["chronicle", "atlas", "nexus", "aegis", "sentinel", "pulse", "forge", "genesis", "oracle"]
    chronicle = None
    for name in boot_order:
        ecosystem._unload_conflicting_modules()
        if name not in ecosystem.REPO_MAP:
            continue
        folder, rel, cls = ecosystem.REPO_MAP[name]
        try:
            C = ecosystem._load(folder, rel, cls)
            if name == "chronicle":
                inst = C()
                chronicle = inst
            elif name == "atlas":
                inst = C(chronicle_client=chronicle)
            elif name == "aegis":
                inst = C(chronicle_client=chronicle, atlas_client=AGENTS.get("atlas"))
            elif name == "forge":
                inst = C(chronicle_client=chronicle, atlas_client=AGENTS.get("atlas"))
            elif name == "genesis":
                inst = C(chronicle_client=chronicle, atlas_client=AGENTS.get("atlas"), aegis_client=AGENTS.get("aegis"), nexus_client=AGENTS.get("nexus"))
            elif name == "oracle":
                inst = C(chronicle_client=chronicle, atlas_client=AGENTS.get("atlas"), sentinel_client=AGENTS.get("sentinel"), pulse_client=AGENTS.get("pulse"))
            elif name == "nexus":
                inst = C(chronicle_client=chronicle, atlas_client=AGENTS.get("atlas"))
            else:
                inst = C()
            if hasattr(inst, "start"):
                inst.start()
            AGENTS[name] = inst
            add_log(name, "ready", f"Agent {name} online and operational")
        except Exception as exc:
            logger.error("Failed to load agent %s: %s", name, exc)
            add_log(name, "error", f"Initialization failed: {exc}")
    nexus = AGENTS.get("nexus")
    if nexus and hasattr(nexus, "register_agent"):
        for agent_name, agent in AGENTS.items():
            if agent_name == "nexus":
                continue
            try:
                nexus.register_agent(agent_name, agent)
            except Exception as exc:
                logger.warning("Could not register %s with Nexus: %s", agent_name, exc)
    yield
    for name, agent in list(AGENTS.items()):
        try:
            if hasattr(agent, "stop"):
                agent.stop()
        except Exception as exc:
            logger.warning("Error stopping %s: %s", name, exc)

app = FastAPI(title="Universal AI Ecosystem API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(nexus_router, prefix="/api/nexus", tags=["Nexus"])
app.include_router(oracle_router, prefix="/api/oracle", tags=["Oracle"])
app.include_router(atlas_router, prefix="/api/atlas", tags=["Atlas"])
app.include_router(pulse_router, prefix="/api/pulse", tags=["Pulse"])
app.include_router(aegis_router, prefix="/api/aegis", tags=["Aegis"])
app.include_router(sentinel_router, prefix="/api/sentinel", tags=["Sentinel"])
app.include_router(forge_router, prefix="/api/forge", tags=["Forge"])
app.include_router(genesis_router, prefix="/api/genesis", tags=["Genesis"])
app.include_router(chronicle_router, prefix="/api/chronicle", tags=["Chronicle"])

@app.get("/")
async def root():
    return RedirectResponse(url="/frontend/")

@app.get("/health")
async def health():
    return {"status": "online", "ecosystem": "Universal AI", "active_agents": list(AGENTS.keys()), "version": "1.0.0"}

@app.get("/api/status")
async def api_status():
    return await health()

@app.get("/agents")
async def list_agents():
    results = []
    for name, agent in AGENTS.items():
        data = await run_in_threadpool(_agent_dashboard_data, name, agent)
        results.append({k: data[k] for k in ("id", "name", "repository", "domain", "description", "status", "running", "capabilities", "overview", "suggestions")})
    return results

@app.get("/agents/{name}/data")
async def agent_data(name: str):
    if name not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return await run_in_threadpool(_agent_dashboard_data, name, AGENTS[name])

@app.get("/logs")
async def get_logs(limit: int = 10):
    return LOGS[-limit:]

async def _agent_status_response(name: str) -> Dict[str, Any]:
    dashboard = await run_in_threadpool(_agent_dashboard_data, name, AGENTS[name])
    return {"agent": name, "task": "agent.status", "status": dashboard["status"], "response": dashboard["overview"], "confidence": 1.0, "evidence": [], "data": dashboard}

@app.post("/agents/{name}/query")
async def query_agent(name: str, request: Request):
    if name not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    data = await request.json()
    prompt = data.get("prompt", "")
    if prompt.strip().lower() in {"status", "health", "what is your status", "show status"}:
        return await _agent_status_response(name)
    add_log(name, "query", f"Processing: {prompt}")
    agent = AGENTS[name]
    try:
        task = _chat_task_for(name, prompt)
        ctx = {"query": prompt, "prompt": prompt, "_sender": "api", "topics": [prompt] if prompt else []}
        symbol = _extract_symbol_from_text(prompt)
        if symbol:
            ctx["symbol"] = symbol
        response = await run_in_threadpool(_call_agent, agent, task, ctx, f"Awaiting instructions. {name} is listening.")
        add_log(name, "response", "Query completed successfully")
        return _normalize_agent_response(name, task, response)
    except Exception as exc:
        add_log(name, "error", str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/agents/{name}/chat")
async def chat_agent(name: str, request: Request):
    if name not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    data = await request.json()
    message = data.get("message") or data.get("prompt", "")
    add_log(name, "chat", f"Chat: {message[:80]}")
    if message.strip().lower() in {"status", "health", "what is your status", "show status"}:
        return await _agent_status_response(name)
    agent = AGENTS[name]
    try:
        task = _chat_task_for(name, message)
        ctx = {"query": message, "prompt": message, "_sender": "chat", "topics": [message] if message else []}
        symbol = _extract_symbol_from_text(message)
        if symbol:
            ctx["symbol"] = symbol
        response = await run_in_threadpool(_call_agent, agent, task, ctx, f"I'm {name}. I received your message: {message}")
        add_log(name, "chat", "Chat response sent")
        return _normalize_agent_response(name, task, response)
    except Exception as exc:
        add_log(name, "error", str(exc))
        return {"agent": name, "task": AGENT_CHAT_TASKS.get(name, "user.query"), "status": "error", "response": f"{name} encountered an error: {str(exc)[:200]}", "confidence": None, "evidence": [], "data": {"status": "error", "message": str(exc)}}

@app.get("/agents/{name}/status")
async def agent_status(name: str):
    if name not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    try:
        return await run_in_threadpool(_agent_dashboard_data, name, AGENTS[name])
    except Exception as exc:
        return {"name": name, "status": "error", "error": str(exc)}

@app.post("/ecosystem/chat")
async def ecosystem_chat(request: Request):
    data = await request.json()
    message = data.get("message") or data.get("prompt", "")
    add_log("ecosystem", "chat", f"Chat: {message[:80]}")
    target_name, routed_message = _addressed_agent(message)
    if target_name and target_name in AGENTS:
        agent = AGENTS[target_name]
        fallback_task = _chat_task_for(target_name, routed_message)
        task = _capability_task_for(agent, routed_message, fallback_task)
        ctx = {
            "query": routed_message,
            "prompt": routed_message,
            "_sender": "ecosystem",
            "topics": [routed_message] if routed_message else [],
        }
        symbol = _extract_symbol_from_text(routed_message)
        if symbol:
            ctx["symbol"] = symbol
        try:
            result = await run_in_threadpool(
                _call_agent,
                agent,
                task,
                ctx,
                f"{target_name.title()} received your message.",
            )
            normalized = _normalize_agent_response(target_name, task, result)
            normalized["via"] = "ecosystem.direct_agent"
            return normalized
        except Exception as exc:
            add_log(target_name, "error", str(exc))
            return {
                "agent": target_name,
                "task": task,
                "status": "error",
                "response": f"{target_name.title()} encountered an error: {str(exc)[:200]}",
                "confidence": None,
                "evidence": [],
                "data": {"status": "error", "message": str(exc)},
                "via": "ecosystem.direct_agent",
            }

    nexus = AGENTS.get("nexus")
    if not nexus:
        return {
            "agent": "ecosystem",
            "task": "ecosystem.route",
            "status": "error",
            "response": "The ecosystem router is not available yet.",
            "confidence": None,
            "evidence": [],
            "data": {"status": "error", "message": "nexus unavailable"},
        }
    ctx = {"query": message, "prompt": message, "_sender": "ecosystem", "topics": [message] if message else []}
    symbol = _extract_symbol_from_text(message)
    if symbol:
        ctx["symbol"] = symbol
    try:
        result = await run_in_threadpool(_call_agent, nexus, "ecosystem.route", ctx, "The ecosystem received your message.")
        return _normalize_agent_response("ecosystem", "ecosystem.route", result)
    except Exception as exc:
        add_log("ecosystem", "error", str(exc))
        return {
            "agent": "ecosystem",
            "task": "ecosystem.route",
            "status": "error",
            "response": f"The ecosystem encountered an error: {str(exc)[:200]}",
            "confidence": None,
            "evidence": [],
            "data": {"status": "error", "message": str(exc)},
        }

if FRONTEND_ROOT.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_ROOT), html=True), name="static-frontend")
else:
    logger.warning("Frontend directory not found at %s", FRONTEND_ROOT)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
