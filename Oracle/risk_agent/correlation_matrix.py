"""
Oracle.risk_agent.correlation_matrix
====================================
Empirical Pearson log-return correlation, currency-leg directional decomposition,
and learned correlation & currency concentration threshold estimators for
OracleRiskSubAgent.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def extract_currency_legs(symbol: str) -> Tuple[str, Optional[str]]:
    """Decompose a symbol into (base, quote) currency legs for exposure correlation."""
    s = str(symbol or "").upper().strip()
    if len(s) == 6 and s.isalpha():
        return s[:3], s[3:]
    for suffix in ("USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)], suffix
    return s, None


def compute_return_correlation(
    closes_a: List[float],
    closes_b: List[float],
) -> Optional[float]:
    """
    Compute empirical Pearson correlation of log-returns between two price series.
    Returns None if insufficient overlapping bars exist.
    """
    n = min(len(closes_a), len(closes_b))
    if n < 15:
        return None
    a = closes_a[-n:]
    b = closes_b[-n:]
    rets_a: List[float] = []
    rets_b: List[float] = []
    for i in range(1, n):
        if a[i - 1] <= 0 or a[i] <= 0 or b[i - 1] <= 0 or b[i] <= 0:
            continue
        rets_a.append(math.log(a[i] / a[i - 1]))
        rets_b.append(math.log(b[i] / b[i - 1]))
    m = len(rets_a)
    if m < 10:
        return None
    mean_a = sum(rets_a) / m
    mean_b = sum(rets_b) / m
    cov = sum((ra - mean_a) * (rb - mean_b) for ra, rb in zip(rets_a, rets_b))
    var_a = sum((ra - mean_a) ** 2 for ra in rets_a)
    var_b = sum((rb - mean_b) ** 2 for rb in rets_b)
    denom = math.sqrt(var_a * var_b)
    if denom <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, cov / denom))


def compute_currency_leg_similarity(
    sym_a: str,
    dir_a: str,
    sym_b: str,
    dir_b: str,
) -> float:
    """
    Structural directional correlation induced by shared currency legs.
    Example: Long EURUSD (+EUR, -USD) and Long GBPUSD (+GBP, -USD) share -USD -> +0.50.
    Example: Long EURUSD (+EUR, -USD) and Short USDCHF (-USD, +CHF) share -USD -> +0.50.
    """
    base_a, quote_a = extract_currency_legs(sym_a)
    base_b, quote_b = extract_currency_legs(sym_b)
    sign_a = 1.0 if dir_a in ("long", "buy") else -1.0
    sign_b = 1.0 if dir_b in ("long", "buy") else -1.0

    vec_a: Dict[str, float] = {base_a: sign_a}
    if quote_a:
        vec_a[quote_a] = vec_a.get(quote_a, 0.0) - sign_a

    vec_b: Dict[str, float] = {base_b: sign_b}
    if quote_b:
        vec_b[quote_b] = vec_b.get(quote_b, 0.0) - sign_b

    shared = set(vec_a.keys()) & set(vec_b.keys())
    if not shared:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in shared)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values())) or 1.0
    norm_b = math.sqrt(sum(v * v for v in vec_b.values())) or 1.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def compute_learned_max_correlation(risk_state: Dict[str, Any]) -> float:
    """
    Derive the maximum allowable directional correlation rho(A, B) between
    concurrent positions from empirical multi-position overlap outcomes.
    Starts at the mathematical half-variance coupling point 1 / sqrt(2) (~0.7071)
    and tightens when realized joint-loss events occur at lower correlations.
    """
    overlaps = list(risk_state.get("overlap_outcomes") or [])
    if not overlaps:
        return round(1.0 / math.sqrt(2.0), 4)

    joint_losses = [float(o["max_corr"]) for o in overlaps if o.get("joint_loss") and "max_corr" in o]
    safe_holds = [float(o["max_corr"]) for o in overlaps if not o.get("joint_loss") and "max_corr" in o]

    base = 1.0 / math.sqrt(2.0)
    if joint_losses:
        mean_loss_corr = sum(joint_losses) / len(joint_losses)
        w = len(joint_losses) / (len(joint_losses) + 3.0)
        base = (1.0 - w) * base + w * max(0.25, mean_loss_corr - 0.10)
    elif safe_holds:
        mean_safe_corr = sum(safe_holds) / len(safe_holds)
        w = len(safe_holds) / (len(safe_holds) + 5.0)
        base = (1.0 - w) * base + w * min(0.85, max(base, mean_safe_corr))

    return round(max(0.25, min(0.85, base)), 4)


def compute_learned_max_currency_exposure_fraction(risk_state: Dict[str, Any]) -> float:
    """
    Learn the maximum net notional exposure to any single currency leg
    (as a fraction of equity) from realized currency concentration outcomes.
    Starts at the golden-ratio conjugate 1/phi (~0.6180) so a single position
    at the 0.50 lot cap is permitted while two stacked same-currency positions
    (1.00 > 0.6180) are blocked unless empirically justified.
    """
    overlaps = list(risk_state.get("overlap_outcomes") or [])
    base = (math.sqrt(5.0) - 1.0) / 2.0
    loss_exposures = [
        float(o["currency_exposure_frac"])
        for o in overlaps
        if o.get("joint_loss") and float(o.get("currency_exposure_frac", 0.0)) > 0
    ]
    if loss_exposures:
        mean_loss_exp = sum(loss_exposures) / len(loss_exposures)
        w = len(loss_exposures) / (len(loss_exposures) + 3.0)
        base = (1.0 - w) * base + w * max(0.20, mean_loss_exp * 0.85)
    return round(max(0.20, min(0.85, base)), 4)
