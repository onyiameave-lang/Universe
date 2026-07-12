"""
shared.llm.client
================
The reasoning brain of the ecosystem: a real, multi-provider LLM client.
(Book I Part IV Article I: an agent may be an LLM; Book II Part II Ch VI
Confidence; Book VI Part II Ch III Honesty.)

This is production-grade, not a stub. It talks to real LLM providers using
their official SDKs and your API keys:

  - Anthropic Claude   (ANTHROPIC_API_KEY)   -> primary reasoning
  - OpenAI GPT         (OPENAI_API_KEY)       -> alternate / fallback
  - Google Gemini      (GEMINI_API_KEY)       -> alternate / fallback

Design principles baked in:
  * Provider-agnostic: one `complete()` / `complete_json()` interface.
  * Automatic failover: if the primary provider errors or has no key, it
    transparently tries the next available provider.
  * Honest degradation: if NO provider is configured, calls return a clear
    `llm_unavailable` result instead of fabricating an answer (constitutional
    honesty). Callers decide whether to fall back to heuristics.
  * Real usage accounting: token counts and latency are recorded per call.
  * Retries with backoff on transient errors.
  * No hidden prompts: every system prompt is passed explicitly (Book II Ch VII).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---- optional SDKs, probed at import (no simulation) ----
try:
    import anthropic  # type: ignore
    _HAS_ANTHROPIC = True
except Exception:
    _HAS_ANTHROPIC = False

try:
    import openai  # type: ignore
    _HAS_OPENAI = True
except Exception:
    _HAS_OPENAI = False

try:
    from google import genai as _genai  # type: ignore
    _HAS_GEMINI = True
except Exception:
    _HAS_GEMINI = False


# Default models per provider (override via env).
DEFAULT_MODELS = {
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-4o"),
    "gemini": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
}


@dataclass
class LLMResult:
    """The outcome of an LLM call, with full provenance."""
    text: str = ""
    provider: str = ""
    model: str = ""
    ok: bool = False
    reason: str = ""            # why it failed, if it did
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    raw: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "provider": self.provider, "model": self.model,
                "ok": self.ok, "reason": self.reason,
                "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
                "latency_ms": self.latency_ms}


class _Provider:
    """Base provider adapter."""
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def complete(self, system: str, messages: List[Dict[str, str]],
                 temperature: float, max_tokens: int) -> LLMResult:
        raise NotImplementedError


class AnthropicProvider(_Provider):
    name = "anthropic"

    def __init__(self):
        self._key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self._client = None
        if _HAS_ANTHROPIC and self._key:
            try:
                self._client = anthropic.Anthropic(api_key=self._key)
            except Exception:
                self._client = None

    def available(self) -> bool:
        return self._client is not None

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        start = time.time()
        model = DEFAULT_MODELS["anthropic"]
        resp = self._client.messages.create(
            model=model, system=system or None,
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
            temperature=temperature, max_tokens=max_tokens)
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        return LLMResult(text=text, provider=self.name, model=model, ok=True,
                        prompt_tokens=getattr(resp.usage, "input_tokens", 0),
                        completion_tokens=getattr(resp.usage, "output_tokens", 0),
                        latency_ms=round((time.time() - start) * 1000, 1), raw=resp)


class OpenAIProvider(_Provider):
    name = "openai"

    def __init__(self):
        self._key = os.getenv("OPENAI_API_KEY", "").strip()
        self._client = None
        if _HAS_OPENAI and self._key:
            try:
                self._client = openai.OpenAI(api_key=self._key)
            except Exception:
                self._client = None

    def available(self) -> bool:
        return self._client is not None

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        start = time.time()
        model = DEFAULT_MODELS["openai"]
        full = ([{"role": "system", "content": system}] if system else []) + messages
        resp = self._client.chat.completions.create(
            model=model, messages=full, temperature=temperature, max_tokens=max_tokens)
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return LLMResult(text=text, provider=self.name, model=model, ok=True,
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                        latency_ms=round((time.time() - start) * 1000, 1), raw=resp)


class GeminiProvider(_Provider):
    name = "gemini"

    def __init__(self):
        self._key = os.getenv("GEMINI_API_KEY", "").strip()
        self._client = None
        if _HAS_GEMINI and self._key:
            try:
                self._client = _genai.Client(api_key=self._key)
            except Exception:
                self._client = None

    def available(self) -> bool:
        return self._client is not None

    def complete(self, system, messages, temperature, max_tokens) -> LLMResult:
        start = time.time()
        model_name = DEFAULT_MODELS["gemini"]
        
        # New google-genai SDK usage
        config = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "system_instruction": system or None
        }
        
        # Convert messages to Gemini format
        # genai SDK expects contents=[{'role': 'user', 'parts': [{'text': '...'}]}, ...]
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
            
        try:
            resp = self._client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config
            )
            text = resp.text or ""
            return LLMResult(text=text, provider=self.name, model=model_name, ok=True,
                            latency_ms=round((time.time() - start) * 1000, 1), raw=resp)
        except Exception as e:
            return LLMResult(ok=False, reason=str(e), provider=self.name, latency_ms=round((time.time() - start) * 1000, 1))


class LLMClient:
    """
    Multi-provider LLM client with failover and honest degradation.

    Usage:
        llm = get_llm()
        result = llm.complete("You are Atlas.", "Summarize dark matter research.")
        if result.ok:
            print(result.text)
    """

    def __init__(self, preferred_order: Optional[List[str]] = None,
                 max_retries: int = 2):
        self.max_retries = max_retries
        self._providers: Dict[str, _Provider] = {
            "anthropic": AnthropicProvider(),
            "openai": OpenAIProvider(),
            "gemini": GeminiProvider(),
        }
        order = preferred_order or os.getenv("LLM_PROVIDER_ORDER", "anthropic,openai,gemini").split(",")
        self._order = [p.strip() for p in order if p.strip() in self._providers]
        self._calls = 0
        self._tokens = 0

    def available_providers(self) -> List[str]:
        return [name for name in self._order if self._providers[name].available()]

    @property
    def has_any(self) -> bool:
        return len(self.available_providers()) > 0

    def complete(self, system: str, prompt: str, temperature: float = 0.3,
                 max_tokens: int = 1024,
                 messages: Optional[List[Dict[str, str]]] = None) -> LLMResult:
        """
        Get a completion, trying providers in order with retries.
        Returns an honest failure result if no provider is available.
        """
        msgs = messages or [{"role": "user", "content": prompt}]
        available = self.available_providers()
        if not available:
            return LLMResult(ok=False, reason="llm_unavailable",
                           text="", provider="none",
                           latency_ms=0.0)

        last_error = ""
        for name in available:
            provider = self._providers[name]
            for attempt in range(self.max_retries + 1):
                try:
                    result = provider.complete(system, msgs, temperature, max_tokens)
                    self._calls += 1
                    self._tokens += result.prompt_tokens + result.completion_tokens
                    return result
                except Exception as exc:
                    last_error = f"{name}: {exc}"
                    if attempt < self.max_retries:
                        time.sleep(0.6 * (attempt + 1))  # linear backoff
                    continue
        return LLMResult(ok=False, reason=f"all_providers_failed: {last_error}",
                        provider="none")

    def complete_json(self, system: str, prompt: str, temperature: float = 0.2,
                      max_tokens: int = 1024) -> Tuple[Optional[Any], LLMResult]:
        """
        Ask for strict JSON and parse it. Returns (parsed_or_None, raw_result).
        Robust to code-fenced JSON. Never fabricates: parse failure -> None.
        """
        json_system = (system + "\n\nRespond with ONLY valid JSON. No prose, no code fences.").strip()
        result = self.complete(json_system, prompt, temperature, max_tokens)
        if not result.ok:
            return None, result
        parsed = self._extract_json(result.text)
        return parsed, result

    @staticmethod
    def _extract_json(text: str) -> Optional[Any]:
        text = text.strip()
        # strip code fences if present
        if text.startswith("```"):
            text = text.split("```", 2)[1] if "```" in text[3:] else text
            text = text.replace("json", "", 1).strip("`\n ")
        try:
            return json.loads(text)
        except Exception:
            # try to locate the first {...} or [...] block
            for opener, closer in (("{", "}"), ("[", "]")):
                start = text.find(opener)
                end = text.rfind(closer)
                if start != -1 and end != -1 and end > start:
                    try:
                        return json.loads(text[start:end + 1])
                    except Exception:
                        continue
        return None

    def stats(self) -> Dict[str, Any]:
        return {"available_providers": self.available_providers(),
                "preferred_order": self._order, "total_calls": self._calls,
                "total_tokens": self._tokens, "has_any": self.has_any}


# Process-wide singleton.
_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm