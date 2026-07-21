"""
shared.llm.client  (Universe-oracle deep-fix v5)
=================================================
Multi-provider LLM client — now the SINGLE gatekeeper for ALL LLM calls.

New in this version (deep-fix):
  1. ESSENTIAL GATE  — if PULSE_LLM_MODE or ORACLE_LLM_MODE == "essential_only",
     any call with essential=False returns LLMResult(ok=False, reason="skipped_non_essential")
     IMMEDIATELY, with zero HTTP traffic.  This is checked BEFORE the circuit breaker,
     BEFORE the token bucket, BEFORE any provider is touched.

  2. CIRCUIT BREAKER — after the first 429 / rate-limit error, ALL subsequent LLM calls
     (essential or not) are blocked for CIRCUIT_BREAKER_COOLDOWN_SEC (default 60 s).
     Returns LLMResult(ok=False, reason="circuit_open") instantly.  Resets automatically
     after the cooldown.

  3. TOKEN-BUCKET RATE LIMITER — process-wide singleton, max LLM_RATE_LIMIT_RPM calls/min
     (default 5).  Serialises all callers so bursts never reach the provider.

  4. SDK RETRIES DISABLED — Anthropic, OpenAI, and google-genai all have aggressive built-in
     retry logic that turns one 429 into 30+ rapid-fire requests.  All three are now
     initialised with max_retries=0.  We own all retry/backoff logic.

  5. EXPONENTIAL BACKOFF WITH FULL JITTER — on 429 / rate-limit errors, waits
     uniform(0, min(cap, 2^attempt)) seconds before retrying.  This is the AWS-recommended
     "full jitter" strategy that prevents synchronised retry waves.

  6. PROMPT DEDUP CACHE — identical (system, prompt, temperature, max_tokens) tuples within
     LLM_CACHE_TTL_SEC (default 120 s) return the cached result with zero API calls.

  7. STATS — llm.stats() now includes circuit_breaker_trips, skipped_non_essential,
     cache_hits, rate_limited_waits so you can observe the gate working.

Environment variables (all optional, safe defaults):
  PULSE_LLM_MODE or ORACLE_LLM_MODE   "essential_only" | "full"  (default "full")
  LLM_RATE_LIMIT_RPM                  int   (default 5)
  LLM_MAX_RETRIES                     int   (default 3)
  LLM_MAX_BACKOFF_SEC                 float (default 60.0)
  LLM_CACHE_TTL_SEC                   float (default 120.0)
  CIRCUIT_BREAKER_COOLDOWN_SEC        float (default 60.0)
  LLM_PROVIDER_ORDER                  comma-separated (default "anthropic,openai,gemini")
  ANTHROPIC_MODEL / OPENAI_MODEL / GEMINI_MODEL
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("shared.llm.client")

# ---------------------------------------------------------------------------
# Read mode ONCE at import time.  main.py MUST call load_dotenv() before any
# import of this module — see the patched main.py.
# ---------------------------------------------------------------------------
def _read_mode() -> str:
    for var in ("PULSE_LLM_MODE", "ORACLE_LLM_MODE"):
        v = os.getenv(var, "").strip().lower()
        if v:
            return v
    return "full"

_LLM_MODE: str = _read_mode()   # "essential_only" | "full"

# ---------------------------------------------------------------------------
# Optional SDKs — probed at import, never simulated
# ---------------------------------------------------------------------------
try:
    import anthropic as _anthropic   # type: ignore
    _HAS_ANTHROPIC = True
except Exception:
    _HAS_ANTHROPIC = False

try:
    import openai as _openai         # type: ignore
    _HAS_OPENAI = True
except Exception:
    _HAS_OPENAI = False

try:
    from google import genai as _genai   # type: ignore
    _HAS_GEMINI = True
except Exception:
    _HAS_GEMINI = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_MODELS = {
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
    "openai":    os.getenv("OPENAI_MODEL",    "gpt-4o"),
    "gemini":    os.getenv("GEMINI_MODEL",    "gemini-2.5-flash"),
    "ollama":    os.getenv("OLLAMA_MODEL",    ""),   # e.g. "llama3", "mistral", "phi3"
}

_OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434").rstrip("/")
_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()

_RATE_LIMIT_RPM      = max(1, int(os.getenv("LLM_RATE_LIMIT_RPM",          "5")))
_MAX_RETRIES         = max(0, int(os.getenv("LLM_MAX_RETRIES",              "3")))
_MAX_BACKOFF_SEC     = float(os.getenv("LLM_MAX_BACKOFF_SEC",               "60.0"))
_CACHE_TTL_SEC       = float(os.getenv("LLM_CACHE_TTL_SEC",                "120.0"))
_CB_COOLDOWN_SEC     = float(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SEC",      "60.0"))


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class LLMResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    ok: bool = False
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    raw: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "provider": self.provider, "model": self.model,
            "ok": self.ok, "reason": self.reason,
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (process-wide singleton)
# ---------------------------------------------------------------------------
class _TokenBucket:
    def __init__(self, rate_per_minute: int):
        self._rate     = rate_per_minute / 60.0
        self._capacity = float(max(1, rate_per_minute))
        self._tokens   = self._capacity
        self._last     = time.monotonic()
        self._lock     = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def acquire(self, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            wait = min(1.0 / max(self._rate, 0.001), deadline - time.monotonic())
            if wait <= 0:
                return False
            time.sleep(wait)


_bucket = _TokenBucket(_RATE_LIMIT_RPM)


# ---------------------------------------------------------------------------
# Circuit breaker (process-wide singleton)
# ---------------------------------------------------------------------------
class _CircuitBreaker:
    def __init__(self, cooldown: float):
        self._cooldown  = cooldown
        self._open_until: float = 0.0
        self._trips: int = 0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def trip(self) -> None:
        with self._lock:
            self._open_until = time.monotonic() + self._cooldown
            self._trips += 1
            log.warning(
                "LLM circuit breaker TRIPPED — all calls blocked for %.0f s (trip #%d)",
                self._cooldown, self._trips,
            )

    def trips(self) -> int:
        with self._lock:
            return self._trips


_breaker = _CircuitBreaker(_CB_COOLDOWN_SEC)


# ---------------------------------------------------------------------------
# Prompt dedup cache (process-wide)
# ---------------------------------------------------------------------------
_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


def _cache_key(system: str, prompt: str, temperature: float, max_tokens: int) -> str:
    raw = f"{system}||{prompt}||{temperature}||{max_tokens}"
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()


def _cache_get(key: str) -> Optional[LLMResult]:
    with _cache_lock:
        entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL_SEC:
        return entry["result"]
    return None


def _cache_put(key: str, result: LLMResult) -> None:
    with _cache_lock:
        _cache[key] = {"result": result, "ts": time.time()}


# ---------------------------------------------------------------------------
# Rate-limit error detection
# ---------------------------------------------------------------------------
def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "429", "rate_limit", "rate limit", "quota", "resource_exhausted",
        "too many requests", "ratelimiterror", "resourceexhausted",
        "toomanyrequests",
    ))


def _backoff(attempt: int) -> None:
    ceiling = min(_MAX_BACKOFF_SEC, 2.0 ** attempt)
    wait = random.uniform(0.0, ceiling)   # full jitter
    log.debug("LLM backoff attempt %d: sleeping %.2f s", attempt, wait)
    time.sleep(wait)


# ---------------------------------------------------------------------------
# Provider adapters  (SDK retries = 0 on all three)
# ---------------------------------------------------------------------------
class _Provider:
    name = "base"
    def available(self) -> bool: raise NotImplementedError
    def complete(self, system: str, messages: List[Dict[str, str]],
                 temperature: float, max_tokens: int) -> LLMResult: raise NotImplementedError


class AnthropicProvider(_Provider):
    name = "anthropic"

    def __init__(self):
        self._key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self._client = None
        if _HAS_ANTHROPIC and self._key:
            try:
                # max_retries=0 — we own all retry logic
                self._client = _anthropic.Anthropic(api_key=self._key, max_retries=0)
            except Exception:
                pass

    def available(self) -> bool:
        return self._client is not None

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        start = time.time()
        model = DEFAULT_MODELS["anthropic"]
        resp = self._client.messages.create(
            model=model, system=system or None,
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
            temperature=temperature, max_tokens=max_tokens,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResult(
            text=text, provider=self.name, model=model, ok=True,
            prompt_tokens=getattr(resp.usage, "input_tokens", 0),
            completion_tokens=getattr(resp.usage, "output_tokens", 0),
            latency_ms=round((time.time() - start) * 1000, 1), raw=resp,
        )


class OpenAIProvider(_Provider):
    name = "openai"

    def __init__(self):
        self._key = os.getenv("OPENAI_API_KEY", "").strip()
        self._client = None
        if _HAS_OPENAI and self._key:
            try:
                # max_retries=0 — we own all retry logic
                self._client = _openai.OpenAI(api_key=self._key, max_retries=0)
            except Exception:
                pass

    def available(self) -> bool:
        return self._client is not None

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        start = time.time()
        model = DEFAULT_MODELS["openai"]
        full = ([{"role": "system", "content": system}] if system else []) + messages
        resp = self._client.chat.completions.create(
            model=model, messages=full, temperature=temperature, max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text=text, provider=self.name, model=model, ok=True,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            latency_ms=round((time.time() - start) * 1000, 1), raw=resp,
        )


class GeminiProvider(_Provider):
    name = "gemini"

    def __init__(self):
        self._key = os.getenv("GEMINI_API_KEY", "").strip()
        self._client = None
        if _HAS_GEMINI and self._key:
            try:
                # Disable SDK retries — try HttpOptions first, fall back for older SDKs
                try:
                    from google.genai import types as _gt  # type: ignore
                    http_opts = _gt.HttpOptions(
                        retry_config=_gt.RetryConfig(max_retries=0)
                    )
                    self._client = _genai.Client(api_key=self._key, http_options=http_opts)
                except Exception:
                    self._client = _genai.Client(api_key=self._key)
            except Exception:
                pass

    def available(self) -> bool:
        return self._client is not None

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        start = time.time()
        model_name = DEFAULT_MODELS["gemini"]
        config = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "system_instruction": system or None,
        }
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        resp = self._client.models.generate_content(
            model=model_name, contents=contents, config=config,
        )
        text = resp.text or ""
        return LLMResult(
            text=text, provider=self.name, model=model_name, ok=True,
            latency_ms=round((time.time() - start) * 1000, 1), raw=resp,
        )


class OllamaProvider(_Provider):
    """
    Local Ollama provider — uses raw urllib HTTP, no 'ollama' package needed.
    Enabled when OLLAMA_MODEL is set in .env (e.g. OLLAMA_MODEL=llama3).
    Ollama must be running: `ollama serve` (default port 11434).
    """
    name = "ollama"

    def __init__(self):
        # FIX-01: Re-read env vars at construction time (after load_dotenv)
        self._url   = os.getenv("OLLAMA_URL", "http://localhost:11434").strip()
        self._model = os.getenv("OLLAMA_MODEL", "").strip()
        self._ok    = False
        if not self._model:
            return
        # Quick reachability probe at init time (non-fatal)
        try:
            import urllib.request as _ur
            req = _ur.Request(f"{self._url}/api/tags",
                              headers={"Accept": "application/json"})
            with _ur.urlopen(req, timeout=3.0) as resp:
                self._ok = resp.status == 200
        except Exception:
            self._ok = False  # Ollama not running yet — will retry at call time

    def available(self) -> bool:
        return bool(self._model)  # available if configured; actual reachability checked at call time

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        import urllib.request as _ur
        import urllib.error as _ue
        import json as _json

        start = time.time()
        # Build a single prompt string from messages
        parts = []
        if system:
            parts.append(f"System: {system}")
        for m in messages:
            role = m.get("role", "user").capitalize()
            parts.append(f"{role}: {m.get('content', '')}")
        parts.append("Assistant:")
        prompt = "\n\n".join(parts)

        payload = _json.dumps({
            "model":  self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }).encode("utf-8")

        req = _ur.Request(
            f"{self._url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _ur.urlopen(req, timeout=120.0) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except _ue.URLError as exc:
            raise RuntimeError(f"Ollama unreachable at {self._url}: {exc}") from exc

        text = data.get("response", "").strip()
        if not text:
            raise RuntimeError(f"Ollama returned empty response (model={self._model})")

        return LLMResult(
            text=text, provider=self.name, model=self._model, ok=True,
            latency_ms=round((time.time() - start) * 1000, 1), raw=data,
        )


# ---------------------------------------------------------------------------
# LLMClient — the single gatekeeper
# ---------------------------------------------------------------------------
class LLMClient:
    """
    Multi-provider LLM client.  Every call passes through:
      1. Essential gate  (instant None if non-essential + essential_only mode)
      2. Circuit breaker (instant None if tripped)
      3. Prompt cache    (instant hit if duplicate within TTL)
      4. Token bucket    (rate-limits to LLM_RATE_LIMIT_RPM)
      5. Provider loop   (tries providers in order with full-jitter backoff)
    """

    def __init__(self, preferred_order: Optional[List[str]] = None,
                 max_retries: int = _MAX_RETRIES):
        self.max_retries = max_retries
        self._providers: Dict[str, _Provider] = {
            "anthropic": AnthropicProvider(),
            "openai":    OpenAIProvider(),
            "gemini":    GeminiProvider(),
            "ollama":    OllamaProvider(),   # local LLM — first when OLLAMA_MODEL is set
        }
        # Default order: Ollama first (free, local, no rate limits) when configured,
        # then cloud providers as fallback.
        # FIX-01: Re-read OLLAMA_MODEL at construction time (after load_dotenv)
        # instead of at module import time. This ensures the env var is read AFTER
        # Nexus/main.py calls load_dotenv().
        ollama_model = os.getenv("OLLAMA_MODEL", "").strip()
        
        if preferred_order:
            order = preferred_order
        else:
            env_order = os.getenv("LLM_PROVIDER_ORDER", "").strip()
            if env_order:
                order = env_order.split(",")
            elif ollama_model:
                # Ollama configured → use it first, cloud providers as fallback
                order = ["ollama", "anthropic", "openai", "gemini"]
                log.info(
                    "LLM provider order: ollama (primary, model=%s) → anthropic → openai → gemini "
                    "(set LLM_PROVIDER_ORDER to override)",
                    ollama_model
                )
            else:
                order = ["anthropic", "openai", "gemini"]
        self._order = [p.strip() for p in order if p.strip() in self._providers]

        # stats counters
        self._calls              = 0
        self._tokens             = 0
        self._cache_hits         = 0
        self._skipped_non_ess    = 0
        self._rate_limited_waits = 0
        self._lock               = threading.Lock()

    # ------------------------------------------------------------------
    def available_providers(self) -> List[str]:
        return [n for n in self._order if self._providers[n].available()]

    @property
    def has_any(self) -> bool:
        return bool(self.available_providers())

    # ------------------------------------------------------------------
    def complete(
        self,
        system: str,
        prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        messages: Optional[List[Dict[str, str]]] = None,
        essential: bool = True,          # ← NEW: callers mark advisory calls False
    ) -> LLMResult:
        """
        Get a completion.  Passes through the 5-layer gate before any HTTP call.

        essential=False  → skipped instantly in essential_only mode.
        essential=True   → still subject to circuit breaker + rate limiter.
        """
        # ── 1. ESSENTIAL GATE ──────────────────────────────────────────
        if not essential and _LLM_MODE == "essential_only":
            with self._lock:
                self._skipped_non_ess += 1
            return LLMResult(ok=False, reason="skipped_non_essential",
                             provider="none", latency_ms=0.0)

        # ── 2. CIRCUIT BREAKER ─────────────────────────────────────────
        if _breaker.is_open():
            return LLMResult(ok=False, reason="circuit_open",
                             provider="none", latency_ms=0.0)

        # ── 3. PROMPT DEDUP CACHE ──────────────────────────────────────
        ck = _cache_key(system, prompt, temperature, max_tokens)
        cached = _cache_get(ck)
        if cached is not None:
            with self._lock:
                self._cache_hits += 1
            return cached

        # ── 4. TOKEN BUCKET ────────────────────────────────────────────
        if not _bucket.acquire(timeout=120.0):
            return LLMResult(ok=False, reason="rate_limiter_timeout",
                             provider="none", latency_ms=0.0)

        # ── 5. PROVIDER LOOP ───────────────────────────────────────────
        msgs = messages or [{"role": "user", "content": prompt}]
        available = self.available_providers()
        if not available:
            return LLMResult(ok=False, reason="llm_unavailable",
                             provider="none", latency_ms=0.0)

        last_error = ""
        for name in available:
            provider = self._providers[name]
            for attempt in range(self.max_retries + 1):
                try:
                    result = provider.complete(system, msgs, temperature, max_tokens)
                    with self._lock:
                        self._calls  += 1
                        self._tokens += result.prompt_tokens + result.completion_tokens
                    _cache_put(ck, result)
                    return result

                except Exception as exc:
                    last_error = f"{name}: {exc}"
                    if _is_rate_limit(exc):
                        _breaker.trip()          # open the circuit breaker
                        with self._lock:
                            self._rate_limited_waits += 1
                        if attempt < self.max_retries:
                            _backoff(attempt)
                        else:
                            # exhausted retries on this provider — stop immediately
                            return LLMResult(
                                ok=False,
                                reason=f"rate_limited_exhausted: {exc}",
                                provider=name, latency_ms=0.0,
                            )
                    else:
                        # non-rate-limit error: short linear wait, then try next provider
                        if attempt < self.max_retries:
                            time.sleep(0.5 * (attempt + 1))
                        break   # move to next provider

        return LLMResult(ok=False, reason=f"all_providers_failed: {last_error}",
                         provider="none")

    # ------------------------------------------------------------------
    def complete_json(
        self,
        system: str,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        essential: bool = True,          # ← NEW
    ) -> Tuple[Optional[Any], LLMResult]:
        json_system = (
            system + "\n\nRespond with ONLY valid JSON. No prose, no code fences."
        ).strip()
        result = self.complete(json_system, prompt, temperature, max_tokens,
                               essential=essential)
        if not result.ok:
            return None, result
        parsed = self._extract_json(result.text)
        return parsed, result

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_json(text: str) -> Optional[Any]:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1] if "```" in text[3:] else text
            text = text.replace("json", "", 1).strip("`\n ")
        try:
            return json.loads(text)
        except Exception:
            for opener, closer in (("{", "}"), ("[", "]")):
                s, e = text.find(opener), text.rfind(closer)
                if s != -1 and e != -1 and e > s:
                    try:
                        return json.loads(text[s:e + 1])
                    except Exception:
                        continue
        return None

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "available_providers":    self.available_providers(),
                "preferred_order":        self._order,
                "llm_mode":               _LLM_MODE,
                "ollama_model":           _OLLAMA_MODEL or "(not configured)",
                "ollama_url":             _OLLAMA_URL,
                "total_calls":            self._calls,
                "total_tokens":           self._tokens,
                "cache_hits":             self._cache_hits,
                "skipped_non_essential":  self._skipped_non_ess,
                "rate_limited_waits":     self._rate_limited_waits,
                "circuit_breaker_trips":  _breaker.trips(),
                "circuit_breaker_open":   _breaker.is_open(),
                "has_any":                self.has_any,
            }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm