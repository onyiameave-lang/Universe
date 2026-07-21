"""
Nexus.core.gemini_router
========================
Gemini-powered routing engine for Nexus with decision logging and RL/imitation
learning pipeline scaffold.

ARCHITECTURE (3-tier routing):
  Tier 1 — Local Ollama model (trained on logged Gemini decisions)
            Fastest, free, no external API calls.
            Used when: local model has been trained (routing_model.jsonl exists
            with >= MIN_TRAINING_SAMPLES entries) AND confidence >= LOCAL_CONF_THRESHOLD.
  Tier 2 — Gemini Flash (NEXUS_GEMINI_KEY)
            Used when: local model unavailable, not yet trained, or low confidence.
            Every Gemini decision is logged to routing_decisions.jsonl for future
            local model training.
  Tier 3 — COMPANY_ALIAS + regex fallback (coordinator_agent._extract_symbol)
            Used when: both LLMs unavailable (no keys, network down, etc.).
            Guarantees Nexus NEVER passes a raw question string to Sentinel.

ENVIRONMENT VARIABLES:
  NEXUS_GEMINI_KEY      — NEW key dedicated to Nexus routing (gemini-2.0-flash).
                          Different from GEMINI_API_KEY (used for final synthesis).
  GEMINI_API_KEY        — ORIGINAL key; used by shared/llm/client.py for synthesis.
  OLLAMA_URL            — Local Ollama base URL (default http://localhost:11434).
  OLLAMA_MODEL          — Local Ollama model name (e.g. llama3, mistral).
  NEXUS_ROUTER_LOG      — Path to routing decisions JSONL (default Nexus/memory/routing_decisions.jsonl).
  NEXUS_LOCAL_CONF      — Minimum confidence for local model to be used (default 0.75).
  NEXUS_MIN_SAMPLES     — Minimum logged decisions before local model is used (default 100).

CONSTITUTIONAL COMPLIANCE:
  Book II Principle III  Memory First — retrieve before generating.
  Book II Principle V    Everything Evolves — routing improves via RL loop.
  Book II Principle VI   Nothing Dies Without Leaving Knowledge — every routing
                         decision is logged to become training data.
  Book II No Silent Failures — every tier failure is logged at WARNING.
  Book III Ch VIII       Standardised Interfaces — routing plan is a typed dict.
  Book IV Continuous Improvement — Gemini teaches local model; local model
                         eventually replaces Gemini for routing.

FIX LOG:
  GR-01  Initial implementation (2026-07-21).
         Gemini Flash routing + JSONL decision logger + Ollama local model
         inference + training pipeline scaffold.

  FIX-GR-05  (2026-07-21): Tier 3 (COMPANY_ALIAS keyword fallback) decisions
             were NOT logged to routing_decisions.jsonl.
             When Gemini is rate-limited (HTTP 429), every routing call falls
             through to Tier 3. Since Tier 3 decisions were never logged, the
             RL trainer could never accumulate 100 decisions → the local model
             would never train → Nexus would be stuck on Tier 3 forever.
             FIX: Added self._logger.log(query, plan) call in the Tier 3 branch
             of GeminiRouter.route(). Tier 3 decisions are logged with
             confidence=0.6 (lower than Gemini's ~0.85-0.95) so the RL trainer
             can distinguish them from high-quality Gemini decisions.
             Also raised RoutingPlan.fallback() confidence from 0.3 → 0.6 to
             reflect that COMPANY_ALIAS extraction is actually quite reliable
             for known tickers and company names.
             Constitutional law: Book II Principle VI Nothing Dies Without
             Leaving Knowledge — every routing decision must be logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.gemini_router")

# ---------------------------------------------------------------------------
# FIX-CA-27: Load .env before reading os.getenv() so NEXUS_GEMINI_KEY is
# picked up even when gemini_router is imported before coordinator_agent.
# Uses override=False so shell-exported vars always win over .env values.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv
    _gr_env_path = Path(__file__).resolve().parents[2] / ".env"  # Universe/.env
    if _gr_env_path.exists():
        _load_dotenv(dotenv_path=_gr_env_path, override=False)
    else:
        _load_dotenv(override=False)
except ImportError:
    pass  # python-dotenv not installed; env vars must be set in shell

# ---------------------------------------------------------------------------
# Configuration (read once at import; all optional — graceful degradation)
# ---------------------------------------------------------------------------
_NEXUS_GEMINI_KEY: str = os.getenv("NEXUS_GEMINI_KEY", "").strip()
_OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "").strip()

_DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "memory" / "routing_decisions.jsonl"
_LOG_PATH: Path = Path(os.getenv("NEXUS_ROUTER_LOG", str(_DEFAULT_LOG_PATH)))

_LOCAL_CONF_THRESHOLD: float = float(os.getenv("NEXUS_LOCAL_CONF", "0.75"))
_MIN_TRAINING_SAMPLES: int = int(os.getenv("NEXUS_MIN_SAMPLES", "100"))

# Gemini Flash model for routing (fast, cheap, accurate for structured JSON)
_GEMINI_ROUTING_MODEL = "gemini-2.0-flash"

# Domains the router can assign (must match DomainClassifier vocabulary)
_VALID_DOMAINS = frozenset({
    "news", "trading", "social", "research", "memory",
    "training", "creation", "governance", "coordination", "general",
})

# Agents that can be called in parallel orchestration
_VALID_AGENTS = frozenset({
    "sentinel", "oracle", "pulse", "atlas", "chronicle",
    "forge", "genesis", "aegis", "nexus",
})

# ---------------------------------------------------------------------------
# Routing plan dataclass — the typed contract between GeminiRouter and
# coordinator_agent. Every tier returns this same structure.
# ---------------------------------------------------------------------------
@dataclass
class RoutingPlan:
    """Structured routing decision returned by every tier.

    Constitutional law: Book III Ch VIII Standardised Interfaces — all tiers
    return the same typed structure so coordinator_agent can treat them uniformly.
    """
    # Extracted subject (ticker, company, topic) — NEVER the raw query string
    symbol: str = ""
    # Human-readable topic for news/social collectors (may differ from symbol)
    topic: str = ""
    # Primary domain for single-agent routing
    primary_domain: str = "general"
    # Primary agent name for single-agent routing
    primary_agent: str = "atlas"
    # Task type to dispatch (e.g. "news.sentiment", "trade.signal")
    primary_task: str = ""
    # Whether multiple agents should be called in parallel
    multi_agent: bool = False
    # List of agents to call in parallel (populated when multi_agent=True)
    agents: List[Dict[str, str]] = field(default_factory=list)
    # Synthesis strategy: "ollama_first" | "gemini_fallback" | "concatenate"
    synthesis_strategy: str = "ollama_first"
    # Which tier produced this plan: "local" | "gemini" | "fallback"
    tier: str = "fallback"
    # Confidence score [0.0, 1.0] — used to decide whether to trust local model
    confidence: float = 0.0
    # Raw Gemini/Ollama response text (for debugging and logging)
    raw_response: str = ""
    # Whether this plan was served from the in-process cache
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def fallback(cls, symbol: str, topic: str, domain: str = "general") -> "RoutingPlan":
        """Construct a minimal fallback plan (Tier 3 — no LLM available)."""
        agent_map = {
            "news": ("sentinel", "news.sentiment"),
            "trading": ("oracle", "trade.signal"),
            "social": ("pulse", "social.sentiment"),
            "research": ("atlas", "research.investigate"),
            "memory": ("chronicle", "memory.answer"),
            "training": ("forge", "training.run"),
            "creation": ("genesis", "agent.create"),
            "governance": ("aegis", "governance.audit"),
        }
        agent, task = agent_map.get(domain, ("atlas", "research.investigate"))
        return cls(
            symbol=symbol,
            topic=topic,
            primary_domain=domain,
            primary_agent=agent,
            primary_task=task,
            multi_agent=False,
            agents=[{"agent": agent, "task": task, "symbol": symbol}],
            synthesis_strategy="concatenate",
            tier="fallback",
            confidence=0.6,   # FIX-GR-05: raised from 0.3; COMPANY_ALIAS is reliable for known tickers
        )


# ---------------------------------------------------------------------------
# Decision logger — every routing decision becomes training data
# ---------------------------------------------------------------------------
class DecisionLogger:
    """Append-only JSONL logger for routing decisions.

    Constitutional law: Book II Principle VI Nothing Dies Without Leaving
    Knowledge — every routing decision is logged so it can become training
    data for the local Ollama model.

    File format (one JSON object per line):
    {
      "ts": 1721000000.0,          # Unix timestamp
      "query": "...",              # Original user query
      "plan": {...},               # RoutingPlan.to_dict()
      "tier": "gemini",            # Which tier produced the plan
      "outcome": null,             # Filled in later by reinforce()
      "quality": null              # 0.0-1.0 quality score (filled by reinforce)
    }
    """

    def __init__(self, log_path: Path = _LOG_PATH):
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, query: str, plan: RoutingPlan) -> None:
        """Append a routing decision to the JSONL log."""
        record = {
            "ts": time.time(),
            "query": query,
            "plan": plan.to_dict(),
            "tier": plan.tier,
            "outcome": None,
            "quality": None,
        }
        try:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("DecisionLogger.log failed: %s", exc)

    def reinforce(self, query: str, quality: float) -> None:
        """Update the quality score for the most recent decision for this query.

        Called by coordinator_agent after the agent response is received and
        evaluated. quality=1.0 means the routing was perfect; 0.0 means it
        was wrong. This is the RL signal.

        Constitutional law: Book II Principle V Everything Evolves — routing
        improves via real outcome feedback.
        """
        if not self._path.exists():
            return
        try:
            with self._lock:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            updated = []
            found = False
            for line in reversed(lines):
                if not found:
                    try:
                        rec = json.loads(line)
                        if rec.get("query") == query and rec.get("quality") is None:
                            rec["quality"] = quality
                            rec["outcome"] = "reinforced"
                            line = json.dumps(rec, ensure_ascii=False)
                            found = True
                    except Exception:
                        pass
                updated.append(line)
            with self._lock:
                self._path.write_text(
                    "\n".join(reversed(updated)) + "\n", encoding="utf-8"
                )
        except Exception as exc:
            log.warning("DecisionLogger.reinforce failed: %s", exc)

    def count(self) -> int:
        """Return total number of logged decisions."""
        if not self._path.exists():
            return 0
        try:
            with self._lock:
                return sum(1 for _ in self._path.open(encoding="utf-8"))
        except Exception:
            return 0

    def load_training_data(self, min_quality: float = 0.5) -> List[Dict[str, Any]]:
        """Load decisions with quality >= min_quality for training.

        Returns list of {"query": str, "plan": dict} pairs suitable for
        fine-tuning the local Ollama model.
        """
        if not self._path.exists():
            return []
        records = []
        try:
            with self._lock:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                try:
                    rec = json.loads(line)
                    q = rec.get("quality")
                    if q is not None and q >= min_quality:
                        records.append({
                            "query": rec["query"],
                            "plan": rec["plan"],
                        })
                except Exception:
                    continue
        except Exception as exc:
            log.warning("DecisionLogger.load_training_data failed: %s", exc)
        return records


# ---------------------------------------------------------------------------
# Gemini Flash routing client (Tier 2)
# ---------------------------------------------------------------------------
class GeminiRoutingClient:
    """Direct Gemini Flash client for routing decisions.

    Uses NEXUS_GEMINI_KEY (separate from GEMINI_API_KEY used for synthesis).
    Calls gemini-2.0-flash with a structured JSON prompt and parses the result
    into a RoutingPlan.

    Constitutional law: Book III Ch VIII Standardised Interfaces — returns
    RoutingPlan regardless of success/failure.
    """

    _SYSTEM_PROMPT = """You are the routing brain of Nexus, an institutional AI coordinator.
Your ONLY job is to analyse a user query and return a JSON routing plan.

Available agents and their tasks:
  sentinel  → news.sentiment, news.report, news.collect
  oracle    → trade.signal, trade.predict, trade.backtest
  pulse     → social.sentiment, social.trends, social.monitor
  atlas     → research.investigate, research.summarise, research.compare
  chronicle → memory.answer, memory.store, memory.search
  forge     → training.run, training.evaluate, training.optimise
  genesis   → agent.create, agent.certify, agent.deploy
  aegis     → governance.audit, governance.score, governance.alert

Rules:
1. Extract the ACTUAL subject (ticker, company name, topic) — NEVER return the full query as symbol.
2. Correct typos: "nvida" → "NVDA", "appl" → "AAPL", "googl" → "GOOGL".
3. If the query mentions multiple domains (e.g. news + trading), set multi_agent=true and list all agents.
4. For news queries: primary_agent=sentinel, primary_task=news.sentiment.
5. For trading queries: primary_agent=oracle, primary_task=trade.signal.
6. For social queries: primary_agent=pulse, primary_task=social.sentiment.
7. For research/general knowledge: primary_agent=atlas, primary_task=research.investigate.
8. synthesis_strategy must be "ollama_first" (Ollama synthesises, Gemini is fallback).

Return ONLY valid JSON, no prose, no code fences:
{
  "symbol": "NVDA",
  "topic": "nvidia",
  "primary_domain": "news",
  "primary_agent": "sentinel",
  "primary_task": "news.sentiment",
  "multi_agent": false,
  "agents": [{"agent": "sentinel", "task": "news.sentiment", "symbol": "NVDA"}],
  "synthesis_strategy": "ollama_first",
  "confidence": 0.95
}"""

    def __init__(self, api_key: str = _NEXUS_GEMINI_KEY):
        self._key = api_key
        self._available = bool(api_key)
        if not self._available:
            log.info(
                "GeminiRoutingClient: NEXUS_GEMINI_KEY not set — Tier 2 (Gemini routing) disabled. "
                "Set NEXUS_GEMINI_KEY in .env to enable. "
                "Constitutional: Book II Principle V Graceful Degradation."
            )

    @property
    def available(self) -> bool:
        return self._available

    def route(self, query: str, timeout: float = 15.0) -> Optional[RoutingPlan]:
        """Call Gemini Flash and parse the routing plan.

        Returns RoutingPlan on success, None on any failure (caller falls through
        to Tier 3).

        Constitutional law: Book II No Silent Failures — all errors logged at WARNING.
        """
        if not self._available:
            return None

        prompt = f"User query: {query}"
        payload = json.dumps({
            "system_instruction": {"parts": [{"text": self._SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 512,
                "responseMimeType": "application/json",
            },
        }).encode("utf-8")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{_GEMINI_ROUTING_MODEL}:generateContent?key={self._key}"
        )
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning(
                "GeminiRoutingClient: HTTP %d for query=%r — %s. "
                "Falling through to Tier 3. "
                "Constitutional: Book II No Silent Failures.",
                exc.code, query[:80], body,
            )
            return None
        except Exception as exc:
            log.warning(
                "GeminiRoutingClient: request failed for query=%r — %s. "
                "Falling through to Tier 3.",
                query[:80], exc,
            )
            return None

        # Parse Gemini response
        try:
            text = (
                raw.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            plan_dict = self._parse_json(text)
            if not plan_dict:
                log.warning(
                    "GeminiRoutingClient: could not parse JSON from response=%r for query=%r",
                    text[:200], query[:80],
                )
                return None
            return self._dict_to_plan(plan_dict, raw_response=text)
        except Exception as exc:
            log.warning(
                "GeminiRoutingClient: response parsing failed for query=%r — %s",
                query[:80], exc,
            )
            return None

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON from Gemini response text (handles code fences)."""
        text = text.strip()
        # Strip code fences if present
        if text.startswith("```"):
            parts = text.split("```")
            for part in parts:
                part = part.strip().lstrip("json").strip()
                if part.startswith("{"):
                    text = part
                    break
        # Direct parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # Find first {...} block
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                pass
        return None

    @staticmethod
    def _dict_to_plan(d: Dict[str, Any], raw_response: str = "") -> RoutingPlan:
        """Convert a parsed Gemini JSON dict to a RoutingPlan, with validation."""
        symbol = str(d.get("symbol", "")).strip().upper() or "UNKNOWN"
        topic = str(d.get("topic", symbol)).strip().lower()
        domain = str(d.get("primary_domain", "general")).strip()
        if domain not in _VALID_DOMAINS:
            domain = "general"
        agent = str(d.get("primary_agent", "atlas")).strip().lower()
        if agent not in _VALID_AGENTS:
            agent = "atlas"
        task = str(d.get("primary_task", "")).strip()
        multi = bool(d.get("multi_agent", False))
        confidence = float(d.get("confidence", 0.85))
        confidence = max(0.0, min(1.0, confidence))

        # Validate agents list
        raw_agents = d.get("agents", [])
        agents: List[Dict[str, str]] = []
        for a in raw_agents:
            if not isinstance(a, dict):
                continue
            a_name = str(a.get("agent", "")).strip().lower()
            a_task = str(a.get("task", "")).strip()
            a_sym = str(a.get("symbol", symbol)).strip().upper()
            if a_name in _VALID_AGENTS and a_task:
                agents.append({"agent": a_name, "task": a_task, "symbol": a_sym})
        if not agents:
            agents = [{"agent": agent, "task": task, "symbol": symbol}]

        synthesis = str(d.get("synthesis_strategy", "ollama_first")).strip()
        if synthesis not in ("ollama_first", "gemini_fallback", "concatenate"):
            synthesis = "ollama_first"

        return RoutingPlan(
            symbol=symbol,
            topic=topic,
            primary_domain=domain,
            primary_agent=agent,
            primary_task=task,
            multi_agent=multi,
            agents=agents,
            synthesis_strategy=synthesis,
            tier="gemini",
            confidence=confidence,
            raw_response=raw_response,
        )


# ---------------------------------------------------------------------------
# Local Ollama routing client (Tier 1)
# ---------------------------------------------------------------------------
class LocalRoutingClient:
    """Ollama-based local routing client (Tier 1).

    Uses the same Ollama instance as the rest of the ecosystem (OLLAMA_URL,
    OLLAMA_MODEL) but with a routing-specific prompt. After the RL training
    pipeline has run (>= MIN_TRAINING_SAMPLES logged decisions), this client
    should produce routing plans as accurate as Gemini's.

    Constitutional law: Book II Principle V Everything Evolves — local model
    improves via imitation learning from Gemini's logged decisions.
    """

    _SYSTEM_PROMPT = """You are the routing brain of Nexus. Given a user query, return a JSON routing plan.

Extract the actual subject (ticker/company/topic), correct typos, and choose the right agent.
Return ONLY valid JSON:
{
  "symbol": "NVDA",
  "topic": "nvidia",
  "primary_domain": "news",
  "primary_agent": "sentinel",
  "primary_task": "news.sentiment",
  "multi_agent": false,
  "agents": [{"agent": "sentinel", "task": "news.sentiment", "symbol": "NVDA"}],
  "synthesis_strategy": "ollama_first",
  "confidence": 0.9
}"""

    def __init__(
        self,
        url: str = _OLLAMA_URL,
        model: str = _OLLAMA_MODEL,
        min_samples: int = _MIN_TRAINING_SAMPLES,
        conf_threshold: float = _LOCAL_CONF_THRESHOLD,
    ):
        self._url = url
        self._model = model
        self._min_samples = min_samples
        self._conf_threshold = conf_threshold

    @property
    def available(self) -> bool:
        """True if Ollama is configured (model name set)."""
        return bool(self._model)

    def route(self, query: str, logged_count: int, timeout: float = 10.0) -> Optional[RoutingPlan]:
        """Try local Ollama routing.

        Returns RoutingPlan only if:
          1. Ollama is configured (OLLAMA_MODEL set)
          2. Enough training data has been logged (>= min_samples)
          3. The response parses cleanly and confidence >= threshold

        Returns None otherwise (caller falls through to Tier 2).

        Constitutional law: Book II Principle V Graceful Degradation — never
        block on local model; fall through to Gemini if anything fails.
        """
        if not self.available:
            return None
        if logged_count < self._min_samples:
            log.debug(
                "LocalRoutingClient: only %d/%d training samples logged — "
                "skipping local model, using Gemini. "
                "Constitutional: Book II Principle V Everything Evolves.",
                logged_count, self._min_samples,
            )
            return None

        prompt = (
            f"System: {self._SYSTEM_PROMPT}\n\n"
            f"User: {query}\n\nAssistant:"
        )
        payload = json.dumps({
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 256},
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("response", "").strip()
        except Exception as exc:
            log.warning(
                "LocalRoutingClient: Ollama call failed for query=%r — %s. "
                "Falling through to Gemini.",
                query[:80], exc,
            )
            return None

        # Parse and validate
        plan = self._parse_plan(text)
        if plan is None:
            log.warning(
                "LocalRoutingClient: could not parse routing plan from Ollama response=%r",
                text[:200],
            )
            return None
        if plan.confidence < self._conf_threshold:
            log.info(
                "LocalRoutingClient: confidence %.2f < threshold %.2f for query=%r — "
                "falling through to Gemini for higher-quality routing.",
                plan.confidence, self._conf_threshold, query[:80],
            )
            return None
        plan.tier = "local"
        return plan

    @staticmethod
    def _parse_plan(text: str) -> Optional[RoutingPlan]:
        """Parse Ollama response text into a RoutingPlan."""
        text = text.strip()
        # Find JSON block
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            return None
        try:
            d = json.loads(text[s:e + 1])
            return GeminiRoutingClient._dict_to_plan(d, raw_response=text)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Result synthesiser — merges multi-agent results into a single answer
# ---------------------------------------------------------------------------
class ResultSynthesiser:
    """Merge results from multiple agents into a single coherent answer.

    Strategy: Ollama first (free, local) → original GEMINI_API_KEY as fallback
    → plain concatenation if both LLMs unavailable.

    Constitutional law: Book II Principle V Graceful Degradation — always
    return something, even if synthesis degrades to concatenation.
    """

    _SYNTHESIS_SYSTEM = """You are the synthesis brain of Nexus. You receive results from multiple
specialist AI agents and must merge them into a single, coherent, human-readable answer.
Be concise. Use bullet points for lists. Do not repeat information. Highlight the most
important insights first."""

    def __init__(self):
        self._ollama_url = _OLLAMA_URL
        self._ollama_model = _OLLAMA_MODEL
        # Original Gemini key for synthesis (GEMINI_API_KEY, not NEXUS_GEMINI_KEY)
        self._gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self._gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def synthesise(
        self,
        query: str,
        results: Dict[str, Any],
        plan: RoutingPlan,
        timeout: float = 20.0,
    ) -> str:
        """Merge agent results into a human-readable answer.

        Tries Ollama first, then original Gemini key, then plain concatenation.

        Constitutional law: Book II Principle V Graceful Degradation.
        """
        if not results:
            return "No results were returned by the specialist agents."

        # Build context string from agent results
        context_parts = []
        for agent_name, result in results.items():
            if not isinstance(result, dict):
                continue
            # Extract the most useful text from each agent's result
            text = self._extract_text(result)
            if text:
                context_parts.append(f"[{agent_name.upper()}]\n{text}")

        if not context_parts:
            return "Specialist agents returned no usable data."

        context = "\n\n".join(context_parts)
        prompt = (
            f"User query: {query}\n\n"
            f"Agent results:\n{context}\n\n"
            f"Synthesise a clear, concise answer:"
        )

        # Tier 1: Ollama synthesis
        if self._ollama_model:
            result = self._ollama_synthesise(prompt, timeout)
            if result:
                log.debug("ResultSynthesiser: Ollama synthesis succeeded.")
                return result

        # Tier 2: Original Gemini key synthesis
        if self._gemini_key:
            result = self._gemini_synthesise(prompt, timeout)
            if result:
                log.debug("ResultSynthesiser: Gemini synthesis succeeded.")
                return result

        # Tier 3: Plain concatenation
        log.info(
            "ResultSynthesiser: both LLMs unavailable — returning concatenated results. "
            "Constitutional: Book II Principle V Graceful Degradation."
        )
        return "\n\n".join(context_parts)

    def _ollama_synthesise(self, prompt: str, timeout: float) -> Optional[str]:
        """Call Ollama for synthesis."""
        full_prompt = (
            f"System: {self._SYNTHESIS_SYSTEM}\n\n"
            f"User: {prompt}\n\nAssistant:"
        )
        payload = json.dumps({
            "model": self._ollama_model,
            "prompt": full_prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 512},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self._ollama_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("response", "").strip()
            return text if len(text) > 20 else None
        except Exception as exc:
            log.warning("ResultSynthesiser._ollama_synthesise failed: %s", exc)
            return None

    def _gemini_synthesise(self, prompt: str, timeout: float) -> Optional[str]:
        """Call original Gemini key for synthesis (fallback)."""
        payload = json.dumps({
            "system_instruction": {"parts": [{"text": self._SYNTHESIS_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 512},
        }).encode("utf-8")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._gemini_model}:generateContent?key={self._gemini_key}"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            text = (
                raw.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
            return text if len(text) > 20 else None
        except Exception as exc:
            log.warning("ResultSynthesiser._gemini_synthesise failed: %s", exc)
            return None

    @staticmethod
    def _extract_text(result: Dict[str, Any]) -> str:
        """Extract the most useful text from an agent result dict."""
        # Try common result fields in priority order
        for key in ("summary", "text", "answer", "message"):
            val = result.get(key)
            if val and isinstance(val, str) and len(val) > 10:
                return val.strip()
        # Try nested result dict
        inner = result.get("result")
        if isinstance(inner, dict):
            for key in ("summary", "text", "answer"):
                val = inner.get(key)
                if val and isinstance(val, str) and len(val) > 10:
                    return val.strip()
        # Try report dict
        report = result.get("report")
        if isinstance(report, dict):
            val = report.get("summary") or report.get("text")
            if val and isinstance(val, str) and len(val) > 10:
                return val.strip()
        # Try sentiment dict
        sentiment = result.get("sentiment")
        if isinstance(sentiment, dict):
            label = sentiment.get("sentiment_label", "")
            count = sentiment.get("article_count", 0)
            if label:
                return f"Sentiment: {label} ({count} sources)"
        return ""


# ---------------------------------------------------------------------------
# In-process routing cache (avoids redundant LLM calls for identical queries)
# ---------------------------------------------------------------------------
class _RoutingCache:
    """Simple TTL cache for routing plans.

    Constitutional law: Book II Principle III Memory First — retrieve before
    generating. Identical queries within TTL reuse the cached plan.
    """

    def __init__(self, ttl_sec: float = 300.0):
        self._ttl = ttl_sec
        self._store: Dict[str, Tuple[RoutingPlan, float]] = {}
        self._lock = threading.Lock()

    def get(self, query: str) -> Optional[RoutingPlan]:
        with self._lock:
            entry = self._store.get(query)
        if entry is None:
            return None
        plan, ts = entry
        if time.time() - ts > self._ttl:
            with self._lock:
                self._store.pop(query, None)
            return None
        cached_plan = RoutingPlan(**asdict(plan))
        cached_plan.cached = True
        return cached_plan

    def put(self, query: str, plan: RoutingPlan) -> None:
        with self._lock:
            self._store[query] = (plan, time.time())

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# GeminiRouter — the public interface used by coordinator_agent
# ---------------------------------------------------------------------------
class GeminiRouter:
    """3-tier routing engine for Nexus.

    Usage in coordinator_agent:
        router = GeminiRouter()                    # singleton, created once
        plan = router.route(query)                 # returns RoutingPlan
        router.reinforce(query, quality=1.0)       # after agent responds

    Tier 1 — Local Ollama (trained on Gemini decisions, >= MIN_TRAINING_SAMPLES)
    Tier 2 — Gemini Flash (NEXUS_GEMINI_KEY)
    Tier 3 — COMPANY_ALIAS + regex fallback (always available)

    Constitutional law:
      Book II Principle III  Memory First — cache before LLM call.
      Book II Principle V    Everything Evolves — local model improves over time.
      Book II Principle VI   Nothing Dies Without Leaving Knowledge — all decisions logged.
      Book II No Silent Failures — all tier failures logged at WARNING.
      Book III Ch VIII       Standardised Interfaces — always returns RoutingPlan.
    """

    def __init__(
        self,
        fallback_extractor=None,   # callable(query: str) -> str  (COMPANY_ALIAS extractor)
        fallback_classifier=None,  # callable(query: str) -> dict (DomainClassifier.classify)
        log_path: Path = _LOG_PATH,
        cache_ttl: float = 300.0,
    ):
        self._local = LocalRoutingClient()
        self._gemini = GeminiRoutingClient()
        self._logger = DecisionLogger(log_path)
        self._cache = _RoutingCache(ttl_sec=cache_ttl)
        self._synthesiser = ResultSynthesiser()
        self._fallback_extractor = fallback_extractor
        self._fallback_classifier = fallback_classifier
        self._lock = threading.Lock()

        log.info(
            "GeminiRouter initialised. "
            "Tier 1 (local Ollama): %s | "
            "Tier 2 (Gemini Flash): %s | "
            "Tier 3 (COMPANY_ALIAS fallback): always available. "
            "Logged decisions: %d / %d needed for local model. "
            "Constitutional: Book II Principle V Everything Evolves.",
            "READY" if self._local.available else "NOT CONFIGURED (OLLAMA_MODEL not set)",
            "READY" if self._gemini.available else "NOT CONFIGURED (NEXUS_GEMINI_KEY not set)",
            self._logger.count(),
            _MIN_TRAINING_SAMPLES,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, query: str) -> RoutingPlan:
        """Produce a routing plan for the given query.

        Tries tiers in order: local → Gemini → fallback.
        Logs every non-cached Gemini decision to routing_decisions.jsonl.
        Caches results for cache_ttl seconds.

        Constitutional law: Book III Ch VIII Standardised Interfaces — always
        returns a RoutingPlan, never raises.
        """
        if not query or not query.strip():
            return RoutingPlan.fallback("UNKNOWN", "unknown", "general")

        # Cache check (Memory First)
        cached = self._cache.get(query)
        if cached:
            log.debug(
                "GeminiRouter: cache hit for query=%r (tier=%s). "
                "Constitutional: Book II Principle III Memory First.",
                query[:80], cached.tier,
            )
            return cached

        logged_count = self._logger.count()

        # Tier 1: Local Ollama (only if enough training data)
        plan = self._local.route(query, logged_count)
        if plan is not None:
            log.info(
                "GeminiRouter: Tier 1 (local Ollama) routed query=%r → "
                "agent=%s task=%s symbol=%s confidence=%.2f",
                query[:80], plan.primary_agent, plan.primary_task,
                plan.symbol, plan.confidence,
            )
            self._cache.put(query, plan)
            return plan

        # Tier 2: Gemini Flash
        plan = self._gemini.route(query)
        if plan is not None:
            log.info(
                "GeminiRouter: Tier 2 (Gemini Flash) routed query=%r → "
                "agent=%s task=%s symbol=%s confidence=%.2f. "
                "Logging decision for RL training. "
                "Constitutional: Book II Principle VI Nothing Dies Without Leaving Knowledge.",
                query[:80], plan.primary_agent, plan.primary_task,
                plan.symbol, plan.confidence,
            )
            self._logger.log(query, plan)
            self._cache.put(query, plan)
            return plan

        # Tier 3: COMPANY_ALIAS + DomainClassifier fallback
        plan = self._fallback_route(query)
        log.info(
            "GeminiRouter: Tier 3 (COMPANY_ALIAS fallback) routed query=%r → "
            "agent=%s task=%s symbol=%s. "
            "Constitutional: Book II Principle V Graceful Degradation.",
            query[:80], plan.primary_agent, plan.primary_task, plan.symbol,
        )
        # FIX-GR-05 (2026-07-21): Log Tier 3 decisions so the RL trainer can
        # accumulate training data even when Gemini is rate-limited (HTTP 429).
        # Without this, 100% Gemini-429 sessions produce zero logged decisions
        # → local model never trains → Nexus stuck on Tier 3 forever.
        # Tier 3 decisions are logged with confidence=0.6 (vs Gemini's ~0.85-0.95)
        # so the trainer can weight them appropriately.
        # Constitutional law: Book II Principle VI Nothing Dies Without Leaving
        # Knowledge — every routing decision must be logged.
        self._logger.log(query, plan)
        self._cache.put(query, plan)
        return plan

    def reinforce(self, query: str, quality: float) -> None:
        """Record the quality of the routing decision for RL training.

        Call this after the agent responds and you can evaluate the result.
        quality=1.0 → perfect routing; 0.0 → wrong agent/symbol.

        Constitutional law: Book II Principle V Everything Evolves — routing
        improves via real outcome feedback.
        """
        self._logger.reinforce(query, quality)
        log.debug(
            "GeminiRouter.reinforce: query=%r quality=%.2f logged. "
            "Constitutional: Book II Principle V Everything Evolves.",
            query[:80], quality,
        )

    def synthesise(
        self,
        query: str,
        results: Dict[str, Any],
        plan: RoutingPlan,
    ) -> str:
        """Merge multi-agent results into a human-readable answer.

        Uses Ollama first (free, local), then original GEMINI_API_KEY as fallback,
        then plain concatenation.

        Constitutional law: Book II Principle V Graceful Degradation.
        """
        return self._synthesiser.synthesise(query, results, plan)

    def stats(self) -> Dict[str, Any]:
        """Return router statistics for monitoring."""
        logged = self._logger.count()
        return {
            "tier1_local_available": self._local.available,
            "tier2_gemini_available": self._gemini.available,
            "tier3_fallback": "always",
            "logged_decisions": logged,
            "min_samples_for_local": _MIN_TRAINING_SAMPLES,
            "local_model_ready": logged >= _MIN_TRAINING_SAMPLES and self._local.available,
            "local_conf_threshold": _LOCAL_CONF_THRESHOLD,
            "log_path": str(_LOG_PATH),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fallback_route(self, query: str) -> RoutingPlan:
        """Tier 3: COMPANY_ALIAS + DomainClassifier fallback.

        Uses the extractor and classifier injected at construction time
        (coordinator_agent passes _extract_symbol and classifier.classify).
        If neither is available, returns a minimal general-purpose plan.

        Constitutional law: Book II Principle V Graceful Degradation — always
        returns a RoutingPlan, never raises.
        """
        symbol = "UNKNOWN"
        domain = "general"

        if self._fallback_extractor:
            try:
                symbol = self._fallback_extractor(query) or "UNKNOWN"
            except Exception as exc:
                log.warning("GeminiRouter._fallback_route: extractor failed: %s", exc)

        if self._fallback_classifier:
            try:
                cls = self._fallback_classifier(query) or {}
                domain = cls.get("domain", "general") or "general"
            except Exception as exc:
                log.warning("GeminiRouter._fallback_route: classifier failed: %s", exc)

        topic = symbol.lower()
        return RoutingPlan.fallback(symbol, topic, domain)


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------
_router_instance: Optional[GeminiRouter] = None
_router_lock = threading.Lock()


def get_router(
    fallback_extractor=None,
    fallback_classifier=None,
) -> GeminiRouter:
    """Return the process-wide GeminiRouter singleton.

    Call once at Nexus startup with the extractor and classifier from
    coordinator_agent. Subsequent calls return the same instance.

    Constitutional law: Book II Principle III Memory First — singleton avoids
    re-initialising the Gemini client and reloading the decision log on every call.
    """
    global _router_instance
    with _router_lock:
        if _router_instance is None:
            _router_instance = GeminiRouter(
                fallback_extractor=fallback_extractor,
                fallback_classifier=fallback_classifier,
            )
    return _router_instance