"""
Forge Experiment Templates
============================
Reusable experiment TYPES, not hardcoded one-off experiments -- "any agent
can request one." This starts with the cheapest one to run: Sensitivity
Analysis (correlate an existing signal against real outcomes, no new
backtesting needed). Heavier templates (Ablation, Walk-Forward, Monte
Carlo Stress Test, etc.) are meant to be added here later, once this
first one has proven the Hypothesis Queue <-> Chronicle <-> Oracle loop
works end-to-end.

Every template function takes trade records and returns a dict with a
`supported` key (True/False/None) -- HypothesisQueue.record_evidence()
reads this to decide confirmed/rejected/inconclusive, once sample size
clears its own bar. A template returning `supported: None` means "the
data doesn't clearly point either way" -- an honest, real outcome, not a
failure to hide (this is the "Evidence Engine" philosophy: an
inconclusive result IS evidence).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def sensitivity_analysis(observations: List[Tuple[float, bool]], label: str,
                          min_effect_size: float = 0.10) -> Dict[str, Any]:
    """
    Genuinely ecosystem-generic: checks whether a signal's VALUE correlates
    with a binary OUTCOME, using only data already collected -- no
    knowledge of what the signal or outcome actually mean. Any agent can
    use this on its own data:

      Oracle:   (entry_streams["news"], trade.won)            -> does the
                news stream's confidence predict trade wins?
      Pulse:    (social_sentiment_score, price_moved_up)       -> does
                social sentiment actually predict market direction?
      Sentinel: (credibility_score, article_was_accurate)      -> does a
                higher credibility score actually mean more accurate news?

    (This was originally written coupled to Oracle's specific
    entry_streams/won trade shape -- generalized on request, before
    building more of the ecosystem loop on top of it, since a caller
    outside Oracle couldn't have used the original signature at all.)

    Method (deliberately simple and auditable, not a black box): split
    observations into "high" (value >= median) and "low" (< median)
    groups, compare outcome rate between them. A real, honest signal
    should show a meaningfully different rate between its high and low
    halves; a signal that's just noise should show roughly the same rate
    in both halves.

    observations: list of (value, outcome) tuples, e.g.
        [(0.3, True), (0.6, False), (0.8, True), ...]
    label: a human-readable name for what's being tested, used only in
        the returned `reason` text (e.g. "news stream", "social score").
    """
    n = len(observations)
    if n < 4:
        return {"supported": None, "reason": f"only {n} observations for '{label}' — too few to analyze",
                "label": label, "sample_size": n}

    values = sorted(v for v, _ in observations)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2

    high_group = [outcome for v, outcome in observations if v >= median]
    low_group = [outcome for v, outcome in observations if v < median]
    if not high_group or not low_group:
        return {"supported": None, "reason": f"all '{label}' values identical — no variation to test",
                "label": label, "sample_size": n}

    high_rate = sum(high_group) / len(high_group)
    low_rate = sum(low_group) / len(low_group)
    effect_size = high_rate - low_rate

    supported: Optional[bool]
    if abs(effect_size) >= min_effect_size:
        supported = True    # a real, meaningfully-sized difference exists
    else:
        supported = False   # no meaningful difference -- this signal isn't predictive here

    return {
        "supported": supported,
        "label": label,
        "sample_size": n,
        "high_group_rate": round(high_rate, 3),
        "low_group_rate": round(low_rate, 3),
        "effect_size": round(effect_size, 3),
        "median_split_value": round(median, 4),
        "reason": (f"{label} high-value observations had a {high_rate:.0%} positive-outcome rate vs "
                   f"{low_rate:.0%} for low-value ones (effect size {effect_size:+.2f})"),
    }


# Registry so callers (Forge's agent, Chronicle's research director) can
# look up a template by name rather than importing functions individually.
# New templates (ablation_experiment, walk_forward_analysis, etc.) get
# added here as they're built.
TEMPLATES = {
    "sensitivity_analysis": sensitivity_analysis,
}


def run_template(template_name: str, **kwargs) -> Dict[str, Any]:
    fn = TEMPLATES.get(template_name)
    if fn is None:
        return {"supported": None, "reason": f"unknown experiment template: {template_name!r}",
                "available_templates": list(TEMPLATES.keys())}
    return fn(**kwargs)