"""
Sentinel Tier 0 — negation-aware, self-improving lexical sentiment
======================================================================
The always-available floor beneath Deep Analysis (which needs an LLM).
Two independent improvements over the old pure keyword-counter:

1. Negation-scope detection (static, fixes a real bug found this session):
   "recession fears will NOT materialize" was scoring -1.0 (maximally
   bearish) because the old scorer just counts bearish words ("recession",
   "fears") and is blind to the "NOT" that flips the entire meaning. This
   detects negation cue words and flips the polarity of bullish/bearish
   terms found within a short window after them.

2. Term reliability weighting (the part that evolves over months): instead
   of every bullish/bearish word counting equally forever, each term has a
   persistent, evolving reliability weight -- graded against what price
   ACTUALLY did afterward (Oracle has this ground truth; Sentinel doesn't
   trade, so this module only tracks the weights -- see record_outcome()
   for how a caller like Oracle's TradeLearningEngine feeds it real
   outcomes).

Safety design (learned directly from a real bug found this session: Atlas's
select_family() mutates a shared global dict in place, causing one call to
PERMANENTLY corrupt strategy selection for every future call, forever):
  - The static BULLISH/BEARISH term sets in intelligence.analysis are never
    mutated -- weights live in a completely separate, persisted store.
  - Weight updates are Bayesian-smoothed (same shape as
    ChampionConfidenceTracker), never raw multiplicative compounding --
    one bad outcome can't runaway-dominate a term's weight.
  - Every weight is bounded to [MIN_WEIGHT, MAX_WEIGHT] regardless of
    update history.
  - New terms discovered via Deep Analysis fall-through are QUEUED for
    human review (get_suggested_terms()), never auto-added to the live
    scoring vocabulary.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from intelligence.analysis import BULLISH, BEARISH, EVENT_TYPES   # the static term sets stay untouched
except ImportError:
    from Sentinel.intelligence.analysis import BULLISH, BEARISH, EVENT_TYPES   # type: ignore

log = logging.getLogger("sentinel.term_reliability")

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[1] / "memory" / "term_reliability.json"

# Bayesian smoothing prior -- same shape as Oracle's
# ChampionConfidenceTracker (PRIOR_TRADES/PRIOR_WIN_RATE), applied here to
# individual terms instead of whole champions.
PRIOR_OBSERVATIONS = 8
PRIOR_RELIABILITY = 0.5   # a brand-new, never-graded term starts at "coin flip" trust
MIN_WEIGHT, MAX_WEIGHT = 0.1, 2.0   # bounded regardless of update history

NEGATION_CUES = (
    "not", "no", "never", "without", "unlikely", "isn't", "doesn't", "won't",
    "wont", "cannot", "can't", "cant", "fails to", "failed to", "failing to",
    "avoid", "avoids", "avoided", "denies", "denied", "rules out",
)
NEGATION_WINDOW = 4   # how many words after a cue count as "in its scope"


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z']+", text.lower())


def _find_negation_spans(tokens: List[str]) -> List[Tuple[int, int]]:
    """Returns (start, end) index ranges that are within a negation cue's scope."""
    spans = []
    cue_tokens = {c for c in NEGATION_CUES if " " not in c}
    multi_word_cues = [c for c in NEGATION_CUES if " " in c]
    joined = " ".join(tokens)
    for cue in multi_word_cues:
        start_char = 0
        while True:
            idx = joined.find(cue, start_char)
            if idx == -1:
                break
            word_idx = len(joined[:idx].split())
            spans.append((word_idx, word_idx + NEGATION_WINDOW))
            start_char = idx + len(cue)
    for i, tok in enumerate(tokens):
        if tok in cue_tokens:
            spans.append((i, i + NEGATION_WINDOW))
    return spans


def _in_any_span(idx: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start <= idx < end for start, end in spans)


class TermReliabilityTracker:
    """
    Persistent, evolving reliability weight per term (bullish or bearish
    keyword). Starts every term at weight 1.0 (identical to the old
    unweighted counter) until real outcomes accumulate.
    """

    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = store_path or DEFAULT_STORE_PATH
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = self._load()
        self._suggestions: List[Dict[str, Any]] = self._data.get("_suggestions", [])
        self._approved: Dict[str, List[str]] = self._data.get("_approved", {})

    def _load(self) -> Dict[str, Any]:
        if self.store_path.exists():
            try:
                return json.loads(self.store_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("term_reliability.json unreadable (%s); starting fresh", exc)
        return {}

    def _save(self) -> None:
        try:
            out = dict(self._data)
            out["_suggestions"] = self._suggestions
            out["_approved"] = self._approved
            self.store_path.write_text(json.dumps(out, indent=2, sort_keys=True))
        except OSError as exc:
            log.warning("could not persist term_reliability.json: %s", exc)

    def weight(self, term: str) -> float:
        rec = self._data.get(term)
        if rec is None:
            return 1.0   # ungraded term: full weight, identical to old behavior
        reliability = rec.get("reliability", PRIOR_RELIABILITY)
        # Map reliability (0..1, where 0.5 = coin flip) onto a weight
        # centered at 1.0 -- a term that's been right 90% of the time gets
        # boosted, one right 10% of the time gets suppressed, bounded.
        raw = 0.4 + reliability * 1.6   # reliability=0.5 -> 1.2; smoothly bounded below
        return max(MIN_WEIGHT, min(MAX_WEIGHT, raw))

    def record_outcome(self, term: str, predicted_correctly: bool) -> Dict[str, Any]:
        """
        Call once per (term, real outcome) pair -- e.g. Oracle's
        TradeLearningEngine, after a trade closes, can look at which terms
        fired in the news that informed it and whether price moved the way
        that term implied. Bayesian-smoothed: no single outcome can swing a
        term's weight drastically, exactly to avoid the kind of runaway,
        unbounded drift found elsewhere this session.
        """
        rec = self._data.setdefault(term, {"observations": 0, "correct": 0})
        rec["observations"] += 1
        if predicted_correctly:
            rec["correct"] += 1
        total = rec["observations"]
        rec["reliability"] = round(
            (PRIOR_OBSERVATIONS * PRIOR_RELIABILITY + rec["correct"]) / (PRIOR_OBSERVATIONS + total), 4
        )
        rec["last_updated"] = time.time()
        self._save()
        return rec

    def suggest_term(self, term: str, event_type: str, example_headline: str) -> None:
        """
        Queues a candidate new term for human review -- e.g. called when
        Deep Analysis confidently classifies something the lexical path
        missed entirely. NEVER auto-added to BULLISH/BEARISH; see
        get_suggested_terms() / approve_term().
        """
        term = term.lower().strip()
        if not term or any(s["term"] == term for s in self._suggestions):
            return
        self._suggestions.append({
            "term": term, "event_type": event_type, "example": example_headline,
            "suggested_at": time.time(),
        })
        self._save()

    def get_suggested_terms(self) -> List[Dict[str, Any]]:
        return list(self._suggestions)

    def dismiss_suggestion(self, term: str) -> None:
        self._suggestions = [s for s in self._suggestions if s["term"] != term.lower().strip()]
        self._save()

    def approve_suggestion(self, term: str) -> Optional[Dict[str, Any]]:
        """
        Promotes a queued suggestion into the persistent approved-keywords
        store (a separate layer from the static EVENT_TYPES code in
        intelligence.analysis, which is never mutated — see
        classify_event()'s use of get_approved_keywords()). Removes it
        from the suggestion queue. Returns the approved entry, or None if
        no matching suggestion was found.
        """
        term = term.lower().strip()
        match = next((s for s in self._suggestions if s["term"] == term), None)
        if match is None:
            return None
        event_type = match["event_type"]
        self._approved.setdefault(event_type, [])
        if term not in self._approved[event_type]:
            self._approved[event_type].append(term)
        self._suggestions = [s for s in self._suggestions if s["term"] != term]
        self._save()
        return match

    def get_approved_keywords(self) -> Dict[str, List[str]]:
        return dict(self._approved)


_tracker_singleton: Optional[TermReliabilityTracker] = None


def get_tracker() -> TermReliabilityTracker:
    """Process-wide singleton, matching shared.llm.get_llm()'s pattern —
    avoids re-reading/writing term_reliability.json on every single
    sentiment() call."""
    global _tracker_singleton
    if _tracker_singleton is None:
        _tracker_singleton = TermReliabilityTracker()
    return _tracker_singleton


def get_approved_keywords() -> Dict[str, List[str]]:
    """Convenience wrapper so intelligence.analysis.classify_event() doesn't
    need to manage a tracker instance itself."""
    return get_tracker().get_approved_keywords()


def _matched_terms_with_polarity(title: str, body: str) -> List[Tuple[str, str, float]]:
    """
    Shared core: finds every BULLISH/BEARISH term present, with its
    EFFECTIVE polarity after negation-flip is applied ("bullish"/"bearish"
    — the term's own listed category may differ from its effective one if
    negation flipped it), and a signed magnitude (+1.0/-1.0, unweighted).
    Both negation_aware_sentiment() and get_matched_terms() build on this
    single source of truth so the matching logic only exists once.
    """
    text = f"{title} {body}"
    tokens = _tokenize(text)
    if not tokens:
        return []
    spans = _find_negation_spans(tokens)
    joined = " ".join(tokens)
    matches: List[Tuple[str, str, float]] = []

    for category, terms, base_polarity in (("bullish", BULLISH, 1.0), ("bearish", BEARISH, -1.0)):
        for term in terms:
            term_tokens = term.split()
            if len(term_tokens) == 1:
                for i, tok in enumerate(tokens):
                    # startswith (not exact ==) restores the old substring-
                    # style recall for plurals/inflections ("cuts" matches
                    # "cut", "surges" matches "surge") while still giving us
                    # a token index to check against negation spans.
                    if tok.startswith(term):
                        negated = _in_any_span(i, spans)
                        signed = -base_polarity if negated else base_polarity
                        matches.append((term, "bullish" if signed > 0 else "bearish", signed))
            elif term in joined:
                idx = len(joined[:joined.find(term)].split())
                negated = _in_any_span(idx, spans)
                signed = -base_polarity if negated else base_polarity
                matches.append((term, "bullish" if signed > 0 else "bearish", signed))
    return matches


_STOPWORDS = frozenset((
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "and", "but", "with", "as", "at", "by", "from", "this", "that", "its", "it",
    "despite", "amid", "after", "before", "new", "says", "said", "will", "would",
    "has", "have", "had", "be", "been", "being", "than", "into", "over", "more",
))


def candidate_keywords_from_miss(title: str, event_type: str) -> List[str]:
    """
    LLM-as-teacher vocabulary discovery (Tier 0 mechanism 2): when the
    lexical classify_event() misses an article entirely (falls through to
    "general") but Deep Analysis confidently classifies it as something
    real, this extracts candidate bigrams that might be the missing
    pattern -- e.g. "cuts rates" or "signals hikes" for the Fed headline
    that classify_event() missed because it only recognizes the exact
    phrase "rate cut"/"rate hike".

    Deliberately conservative: skips stopwords, skips bigrams where BOTH
    words are already part of some existing category's keyword list (not
    a new pattern), and returns at most 3 candidates. These are meant for
    a human to review via get_suggested_terms() -- never auto-added.
    """
    tokens = _tokenize(title)
    if len(tokens) < 2:
        return []

    known_words = set()
    for terms in EVENT_TYPES.values():
        for t in terms:
            known_words.update(t.split())

    candidates: List[str] = []
    for i in range(len(tokens) - 1):
        w1, w2 = tokens[i], tokens[i + 1]
        if w1 in _STOPWORDS or w2 in _STOPWORDS:
            continue
        if w1 in known_words and w2 in known_words:
            continue   # both words already recognized somewhere -- not a new gap
        candidates.append(f"{w1} {w2}")

    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:3]


def get_matched_terms(title: str, body: str) -> Dict[str, List[str]]:
    """
    Returns {"bullish": [...], "bearish": [...]} -- the terms that
    actually fired, using their EFFECTIVE (post-negation) polarity. This is
    what a caller (Oracle) should capture at trade-entry time, so that
    later, once the trade closes and the real outcome is known, each term
    can be graded against reality via record_outcome().
    """
    out: Dict[str, List[str]] = {"bullish": [], "bearish": []}
    for term, effective_category, _signed in _matched_terms_with_polarity(title, body):
        out[effective_category].append(term)
    return out


def negation_aware_sentiment(title: str, body: str, tracker: Optional[TermReliabilityTracker] = None) -> float:
    """
    Tier 0 sentiment: same bullish/bearish term sets as before, but now
    negation-aware (a term inside a negation cue's scope has its polarity
    flipped) and weighted by each term's evolving reliability (defaults to
    1.0 -- identical to the old unweighted behavior -- until real outcomes
    accumulate via record_outcome()).
    """
    score = 0.0
    for term, _effective_category, signed in _matched_terms_with_polarity(title, body):
        w = tracker.weight(term) if tracker else 1.0
        score += signed * w

    if score == 0.0:
        return 0.0
    return max(-1.0, min(1.0, score / max(1.0, abs(score))))