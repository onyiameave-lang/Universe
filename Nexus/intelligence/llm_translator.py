"""
Nexus.intelligence.llm_translator
=================================
Hybrid Dual-LLM Understanding & Grounded Response Layer for Universe.

Supports two pluggable LLM backends on top of Universe's deterministic agents:
  1. Cloud Gemini Layer (`gemini-4-hydrogen`) via `GEMINI_API_KEY` or `NEXUS_GEMINI_KEY`
  2. Free Local Ollama Layer (`http://127.0.0.1:11434`) via `OLLAMA_HOST` & `OLLAMA_MODEL`
     (auto-detects any locally installed Ollama model such as `llama3.2`, `qwen2.5`, `llama3`, `mistral`)

Constitutional Grounding Law:
  - The LLM NEVER invents account balances, trade counts, win rates, R-multiples, or prices.
  - Step 1 (Intent & Domain Translation): Translates any free-form, colloquial, or complex human
    question into the exact specialist domain(s) (`trading`, `training`, `governance`, `news`,
    `social`, `memory`, `creation`, `research`) and intent topic.
  - Step 2 (Fact-Locked Natural Synthesis): Takes the real verified data returned by Oracle,
    Forge, Aegis, Sentinel, Pulse, Atlas, Genesis, and Chronicle, and phrases a clear, direct,
    human-friendly response that answers the user's exact question while keeping every number
    and fact 100% locked to the verified agent output.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

VALID_DOMAINS = (
    "trading",
    "training",
    "governance",
    "news",
    "social",
    "memory",
    "creation",
    "research",
)

DOMAIN_TO_AGENT = {
    "trading": "oracle",
    "prediction": "oracle",
    "training": "forge",
    "governance": "aegis",
    "news": "sentinel",
    "social": "pulse",
    "memory": "chronicle",
    "creation": "genesis",
    "research": "atlas",
}

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-4-hydrogen"
DEFAULT_OLLAMA_HOST = (os.getenv("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434").rstrip("/")

# Cached Ollama model discovery (refreshed every 30s)
_OLLAMA_CACHE: Dict[str, Any] = {
    "checked_at": 0.0,
    "available": False,
    "model": "",
    "models": [],
}


def _get_gemini_api_key() -> str:
    key = (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("NEXUS_GEMINI_KEY", "").strip()
    )
    if key in ("", "MY_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY"):
        return ""
    return key


def discover_local_ollama(force: bool = False) -> Dict[str, Any]:
    """
    Probe local Ollama server (`http://127.0.0.1:11434/api/tags`) with a fast timeout.
    Automatically selects `OLLAMA_MODEL` if configured, or the best installed local model.
    """
    now = time.time()
    if not force and (now - float(_OLLAMA_CACHE.get("checked_at", 0.0))) < 30.0:
        return dict(_OLLAMA_CACHE)

    configured_model = os.getenv("OLLAMA_MODEL", "").strip()
    req = urllib.request.Request(
        f"{DEFAULT_OLLAMA_HOST}/api/tags",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=0.65) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models_raw = data.get("models") or []
        names = [str(m.get("name") or "").strip() for m in models_raw if isinstance(m, dict) and m.get("name")]
        chosen = configured_model
        if not chosen and names:
            preferred_prefixes = ("llama3.2", "qwen2.5", "llama3.1", "llama3", "mistral", "gemma2", "phi3")
            for pref in preferred_prefixes:
                match = next((n for n in names if n.lower().startswith(pref)), None)
                if match:
                    chosen = match
                    break
            if not chosen:
                chosen = names[0]
        _OLLAMA_CACHE.update({
            "checked_at": now,
            "available": bool(chosen),
            "model": chosen,
            "models": names,
            "host": DEFAULT_OLLAMA_HOST,
        })
    except Exception:
        _OLLAMA_CACHE.update({
            "checked_at": now,
            "available": False,
            "model": configured_model,
            "models": [],
            "host": DEFAULT_OLLAMA_HOST,
        })
    return dict(_OLLAMA_CACHE)


def get_llm_layer_status() -> Dict[str, Any]:
    """Return live availability of both Cloud Gemini and Local Ollama layers."""
    gemini_key_present = bool(_get_gemini_api_key())
    ollama_info = discover_local_ollama()
    active_provider = (
        "gemini"
        if gemini_key_present
        else ("ollama" if ollama_info.get("available") else "deterministic_sovereign")
    )
    return {
        "active_provider": active_provider,
        "hybrid_mode": "dual_llm_plus_deterministic_grounding",
        "gemini": {
            "configured": gemini_key_present,
            "model": DEFAULT_GEMINI_MODEL,
        },
        "ollama": {
            "available": bool(ollama_info.get("available")),
            "host": DEFAULT_OLLAMA_HOST,
            "active_model": ollama_info.get("model") or "llama3.2",
            "installed_models": ollama_info.get("models") or [],
        },
    }


def _call_gemini_rest(
    prompt: str,
    system_instruction: str,
    json_output: bool = False,
    timeout_sec: float = 4.5,
) -> Optional[str]:
    api_key = _get_gemini_api_key()
    if not api_key:
        return None
    model = DEFAULT_GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload: Dict[str, Any] = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
    }
    if json_output:
        payload["generationConfig"] = {"responseMimeType": "application/json"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "aistudio-build",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        candidates = body.get("candidates") or []
        if candidates:
            parts = ((candidates[0].get("content") or {}).get("parts")) or []
            texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")]
            out = "".join(texts).strip()
            return out or None
    except Exception:
        return None
    return None


def _call_ollama_local(
    prompt: str,
    system_instruction: str,
    json_output: bool = False,
    timeout_sec: float = 4.5,
) -> Optional[str]:
    info = discover_local_ollama()
    if not info.get("available") or not info.get("model"):
        return None
    payload: Dict[str, Any] = {
        "model": info["model"],
        "stream": False,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
    }
    if json_output:
        payload["format"] = "json"

    req = urllib.request.Request(
        f"{DEFAULT_OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        msg = (body.get("message") or {}).get("content")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    except Exception:
        return None
    return None


def generate_with_dual_llm(
    prompt: str,
    system_instruction: str,
    json_output: bool = False,
    timeout_sec: float = 4.5,
) -> Tuple[Optional[str], str]:
    """
    Try Cloud Gemini (`gemini-4-hydrogen`) first or Local Ollama (`http://127.0.0.1:11434`)
    depending on `LLM_PROVIDER_ORDER` (defaults to Gemini -> Ollama -> Deterministic).
    Returns `(text_or_none, provider_used)`.
    """
    order_raw = os.getenv("LLM_PROVIDER_ORDER", "gemini,ollama").lower()
    prefer_ollama_first = order_raw.startswith("ollama")

    if prefer_ollama_first:
        ollama_txt = _call_ollama_local(prompt, system_instruction, json_output=json_output, timeout_sec=timeout_sec)
        if ollama_txt:
            return ollama_txt, "ollama"
        gem_txt = _call_gemini_rest(prompt, system_instruction, json_output=json_output, timeout_sec=timeout_sec)
        if gem_txt:
            return gem_txt, "gemini"
    else:
        gem_txt = _call_gemini_rest(prompt, system_instruction, json_output=json_output, timeout_sec=timeout_sec)
        if gem_txt:
            return gem_txt, "gemini"
        ollama_txt = _call_ollama_local(prompt, system_instruction, json_output=json_output, timeout_sec=timeout_sec)
        if ollama_txt:
            return ollama_txt, "ollama"

    return None, "deterministic_sovereign"


def semantic_intent_classify(query: str) -> Optional[Dict[str, Any]]:
    """
    Use the Dual-LLM layer (Gemini or Local Ollama) to understand informal, colloquial,
    or nuanced user phrasing and map it to Universe's specialist domains.
    """
    q = (query or "").strip()
    if not q or len(q) < 3:
        return None

    sys_inst = (
        "You are Nexus, the intent router for the 9-agent Universe AI ecosystem. "
        "Classify the user's message into JSON with exact keys:\n"
        "- \"domains\": array of 1 to 3 strings chosen ONLY from: "
        "[\"trading\", \"training\", \"governance\", \"news\", \"social\", \"memory\", \"creation\", \"research\"]\n"
        "  * \"trading\" (Oracle): today's trades, did we make money, profit/loss, P&L, account balance, win rate, open positions, EURUSD/GBPUSD/XAUUSD/USDJPY signals.\n"
        "  * \"training\" (Forge): last trading session review, Mistake Clinic, strategy retraining, 7-timeframe lockbox backtests.\n"
        "  * \"governance\" (Aegis): is our account safe, risk rules, Stop-Loss < Take-Profit law, blocked/vetoed bad trades.\n"
        "  * \"news\" (Sentinel): world news, financial headlines, macro events, geopolitics.\n"
        "  * \"social\" (Pulse): social media posts, what people/traders are saying on Mastodon/Bluesky/HackerNews, crowd sentiment.\n"
        "  * \"memory\" (Chronicle): what have we learned or saved in shared memory.\n"
        "  * \"creation\" (Genesis): evolving new strategy genomes or Monte Carlo blueprints.\n"
        "  * \"research\" (Atlas): general knowledge, science, history, formulas, equations, definitions, or how good Atlas's 5 research desks are.\n"
        "- \"intent_topic\": short snake_case description (e.g. \"daily_pnl_and_trades\", \"open_trades\", \"win_rate\", \"world_news\", \"general_research\")\n"
        "- \"is_ecosystem_question\": boolean (true if about our trading account, our agents, news, social sentiment, or memory; false if a general knowledge/science/history question)\n"
        "Return ONLY valid JSON."
    )
    raw, provider = generate_with_dual_llm(
        f"User question: {q}",
        sys_inst,
        json_output=True,
        timeout_sec=3.2,
    )
    if not raw:
        return None
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            return None
        doms = [
            d for d in (parsed.get("domains") or [])
            if isinstance(d, str) and d in VALID_DOMAINS
        ]
        if not doms:
            return None
        return {
            "domains": doms[:3],
            "intent_topic": str(parsed.get("intent_topic") or "general"),
            "is_ecosystem_question": bool(parsed.get("is_ecosystem_question", True)),
            "llm_provider": provider,
        }
    except Exception:
        return None
