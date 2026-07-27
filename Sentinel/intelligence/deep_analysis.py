"""
Sentinel Deep Analysis — structured event understanding
==========================================================
Implements Stages 1, 2, and 6 of the "teach Sentinel to reason about
events, not count words" redesign:

  Stage 1: Understand the article -> structured extraction (event_type,
           companies/countries/people, magnitude, timeframe, confidence)
  Stage 2: Identify the event -> a real event-type taxonomy instead of a
           positive/negative label
  Stage 6: Multi-dimensional sentiment -> company / economic / investor /
           supply_chain / long_term_outlook, not one blended number

Deliberately NOT attempted here (see conversation): the Knowledge Graph
(Stage 4) and Historical Memory comparison (Stage 5) need real accumulated
infrastructure/data that doesn't exist yet; downstream sector-impact
prediction (Stage 8) needs calibration data. This module is designed so
those can plug in later without rework -- e.g. `companies`/`countries`
extracted here are exactly what a future knowledge graph would key on.

Design philosophy
------------------
This ADDS a new, richer path -- it does not remove intelligence.analysis's
existing classify_event()/sentiment(). Those keyword-based functions stay
as the fast, free, always-available fallback (this codebase's own stated
"graceful degradation" principle — see Atlas's comments in
research_agent.py). deep_analyze() only activates when an LLM client with
a real key is available (self.llm.has_any), matching exactly how
intelligence.analysis.sentiment() already gates its own LLM fallback call.

IMPORTANT: the actual LLM call itself cannot be tested in this sandbox (no
network, no API key) — only the request-building and, critically, the
response VALIDATION/clamping logic can be verified here (with mocked LLM
responses, including malformed/adversarial ones). Never trust a raw LLM
JSON response blindly: _validate_and_clamp() is the load-bearing part of
this file precisely because of that.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("sentinel.deep_analysis")

EVENT_TAXONOMY = (
    "earnings", "merger", "bankruptcy", "interest_rate", "inflation",
    "product_launch", "executive_change", "war", "sanctions", "election",
    "natural_disaster", "regulation", "lawsuit", "trade_policy",
    "supply_chain", "general",
)
MAGNITUDES = ("small", "medium", "large")
TIMEFRAMES = ("immediate", "short_term", "medium_term", "long_term")

SENTIMENT_DIMENSIONS = ("company", "economic", "investor", "supply_chain", "long_term_outlook")

_SYSTEM_PROMPT = (
    "You are a financial news analyst. You read articles and extract "
    "structured market intelligence -- not a positive/negative label, but "
    "what actually happened, who is involved, and how it plausibly affects "
    "markets across several distinct dimensions. You are careful not to "
    "overstate confidence when an article is vague, speculative, or "
    "based on a single unconfirmed source."
)

_PROMPT_TEMPLATE = """Read this news article and extract structured market intelligence.

Title: {title}
Summary: {summary}

Respond with ONLY a JSON object with this exact shape:
{{
  "event_type": one of {taxonomy},
  "companies": [list of company names mentioned, empty list if none],
  "countries": [list of country names mentioned, empty list if none],
  "people": [list of named people mentioned, empty list if none],
  "magnitude": one of {magnitudes},
  "timeframe": one of {timeframes},
  "sentiment": {{
    "company": number from -1 (very bad for the company) to 1 (very good),
    "economic": number from -1 to 1 (broad economic implication),
    "investor": number from -1 to 1 (how investors are likely reacting),
    "supply_chain": number from -1 to 1 (supply chain implication, 0 if none),
    "long_term_outlook": number from -1 to 1
  }},
  "confidence": number from 0 to 1 (how confident YOU are in this reading --
    low if the article is vague, speculative, opinion-based, or relies on
    a single unconfirmed claim),
  "reasoning": "one plain sentence explaining the market logic"
}}
"""


def build_prompt(title: str, summary: str) -> str:
    return _PROMPT_TEMPLATE.format(
        title=title, summary=summary or "(no summary available)",
        taxonomy=list(EVENT_TAXONOMY), magnitudes=list(MAGNITUDES), timeframes=list(TIMEFRAMES),
    )


def deep_analyze(title: str, summary: str, llm=None) -> Optional[Dict[str, Any]]:
    """
    Returns a validated structured analysis dict, or None if no LLM is
    available or the call/parse failed (callers should fall back to the
    existing lexical classify_event()/sentiment() in that case -- this
    function never raises, and never returns a partially-trusted result).
    """
    if llm is None or not getattr(llm, "has_any", False):
        return None
    try:
        parsed, result = llm.complete_json(
            _SYSTEM_PROMPT, build_prompt(title, summary),
            temperature=0.2, max_tokens=400,
            essential=False,   # gated by the same circuit-breaker as sentiment()'s LLM fallback
        )
    except Exception as exc:
        log.warning("deep_analyze: LLM call raised: %s", exc)
        return None
    if parsed is None:
        return None
    return _validate_and_clamp(parsed)


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:   # NaN
        return default
    return max(lo, min(hi, v))


def _validate_and_clamp(parsed: Any) -> Optional[Dict[str, Any]]:
    """
    Never trust a raw LLM JSON response. Every field is validated against
    an explicit allow-list or numeric range; anything malformed, missing,
    wrong-typed, or hallucinated outside the taxonomy falls back to a safe
    default rather than propagating garbage (or a KeyError/TypeError)
    downstream into Oracle's trading decisions.
    """
    if not isinstance(parsed, dict):
        return None

    event_type = parsed.get("event_type")
    if not isinstance(event_type, str) or event_type not in EVENT_TAXONOMY:
        event_type = "general"

    def _str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(v) for v in value if isinstance(v, (str, int, float))][:10]

    companies = _str_list(parsed.get("companies"))
    countries = _str_list(parsed.get("countries"))
    people = _str_list(parsed.get("people"))

    magnitude = parsed.get("magnitude")
    if not isinstance(magnitude, str) or magnitude not in MAGNITUDES:
        magnitude = "medium"

    timeframe = parsed.get("timeframe")
    if not isinstance(timeframe, str) or timeframe not in TIMEFRAMES:
        timeframe = "short_term"

    raw_sentiment = parsed.get("sentiment")
    sentiment = {}
    if not isinstance(raw_sentiment, dict):
        raw_sentiment = {}
    for dim in SENTIMENT_DIMENSIONS:
        sentiment[dim] = _clamp(raw_sentiment.get(dim), -1.0, 1.0, default=0.0)

    confidence = _clamp(parsed.get("confidence"), 0.0, 1.0, default=0.3)

    reasoning = parsed.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, str) else ""
    reasoning = reasoning[:300]   # cap length -- don't trust an LLM to respect "one sentence"

    return {
        "event_type": event_type,
        "companies": companies,
        "countries": countries,
        "people": people,
        "magnitude": magnitude,
        "timeframe": timeframe,
        "sentiment": sentiment,
        "confidence": confidence,
        "reasoning": reasoning,
        "source": "deep_analysis",
    }