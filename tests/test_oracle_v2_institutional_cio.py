from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from Oracle.agents.oracle_agent import OracleAgent
from Oracle.core.market_data import Bar, Series
from Oracle.core.v2_cio_engine import (
    OracleCIOEngine,
    OraclePortfolioSubAgent,
    OracleRiskSubAgent,
    OracleScoutSubAgent,
    OracleV2LearningStore,
    SCOUT_DIMENSIONS,
)
from Oracle.intelligence.decision_ledger import OracleDecisionLedger


from datetime import datetime, timezone
import math


def _make_trending_series(symbol: str, start_price: float = 1.1000, step: float = 0.0020, n: int = 80) -> Series:
    bars: List[Bar] = []
    price = start_price
    sym_hash = sum((idx + 1) * ord(ch) for idx, ch in enumerate(symbol.upper()))
    freq = 0.35 + (sym_hash % 17) * 0.07
    phase = (sym_hash % 13) * 0.45
    for i in range(n):
        open_p = price
        # Independent symbol-specific oscillation around an upward drift so cross-symbol
        # log-return correlation rho(A, B) is near 0 while SMA20 > SMA50, MACD > 0, and RSI in [55, 68].
        wave = math.sin(i * freq + phase) + 0.5 * math.cos(i * (freq * 1.7) - phase)
        if i >= n - 8:
            delta = (0.85 + 0.35 * max(0.0, wave)) * step
        elif wave < -0.35:
            delta = -0.55 * step
        else:
            delta = (0.65 + 0.45 * abs(wave)) * step
        close_p = price + delta
        high_p = max(open_p, close_p) + abs(step) * 0.5
        low_p = min(open_p, close_p) - abs(step) * 0.3
        bars.append(
            Bar(
                ts=f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                open=round(open_p, 5),
                high=round(high_p, 5),
                low=round(low_p, 5),
                close=round(close_p, 5),
                volume=1000.0 + i * 10.0,
            )
        )
        price = close_p
    return Series(symbol=symbol.upper(), bars=bars, source="unit_test")


class _StubSentinel:
    def __init__(self, high_impact_symbols: List[str] | None = None, degraded: bool = False):
        self.high_impact_symbols = {s.upper() for s in (high_impact_symbols or [])}
        self.degraded = degraded

    def sentiment_for(self, symbol: str) -> Dict[str, Any]:
        sym = symbol.upper()
        now_iso = datetime.now(timezone.utc).isoformat()
        if self.degraded:
            return {
                "sentiment": 0.0,
                "direction": 0.0,
                "confidence": 0.0,
                "evidence_state": "degraded",
                "note": "Sentinel feed timeout",
                "article_count": 0,
                "articles": [],
                "risk_articles": [],
            }
        if sym in self.high_impact_symbols:
            articles = [
                {
                    "title": f"Emergency central bank surprise shock rate decision on {sym}",
                    "summary": "Unexpected emergency rate hike and sudden crisis panic",
                    "event_type": "monetary_policy",
                    "credibility": 0.95,
                    "misinformation_risk": 0.0,
                    "sentiment": -0.90,
                    "published_at": now_iso,
                }
            ]
            return {
                "sentiment": 0.65,
                "direction": 0.65,
                "confidence": 0.85,
                "evidence_state": "complete",
                "article_count": len(articles),
                "articles": articles,
                "risk_articles": articles,
            }
        articles = [
            {
                "title": f"{sym} institutional demand strengthens steadily",
                "summary": "Steady flows support continuation",
                "event_type": "general",
                "credibility": 0.88,
                "misinformation_risk": 0.0,
                "sentiment": 0.35,
                "published_at": now_iso,
            }
        ]
        return {
            "sentiment": 0.65,
            "direction": 0.65,
            "confidence": 0.80,
            "evidence_state": "complete",
            "article_count": len(articles),
            "articles": articles,
            "risk_articles": articles,
        }

    def act(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        topics = context.get("topics") or []
        sym = str(topics[0]).upper() if topics else "EURUSD"
        info = self.sentiment_for(sym)
        return {"status": "complete", "articles": info.get("articles", [])}


class _StubPulse:
    def __init__(self, manipulated_symbols: List[str] | None = None):
        self.manipulated_symbols = {s.upper() for s in (manipulated_symbols or [])}

    def sentiment_for(self, symbol: str) -> Dict[str, Any]:
        sym = symbol.upper()
        if sym in self.manipulated_symbols:
            flags = [
                {
                    "symbol": sym,
                    "direction": "bullish",
                    "manipulation_score": 0.88,
                    "posts": 42,
                    "low_authenticity_share": 0.84,
                    "type": "coordinated_pump",
                    "severity": "high",
                    "bot_ratio": 0.84,
                    "reason": f"Coordinated bot pump detected on {sym}",
                }
            ]
            return {
                "sentiment": 0.8,
                "direction": 0.8,
                "confidence": 0.75,
                "evidence_state": "complete",
                "post_count": 42,
                "manipulation_warning": True,
                "manipulation_flags": flags,
            }
        return {
            "sentiment": 0.55,
            "direction": 0.55,
            "confidence": 0.75,
            "evidence_state": "complete",
            "post_count": 18,
            "manipulation_warning": False,
            "manipulation_flags": [],
        }

    def act(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        sym = str(context.get("symbol") or context.get("topic") or "EURUSD").upper()
        info = self.sentiment_for(sym)
        return {"status": "complete", "manipulation_flags": info.get("manipulation_flags", [])}


class _StubAtlas:
    def __init__(self):
        self.queries: List[Dict[str, Any]] = []

    def act(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        self.queries.append({"task": task, "context": context})
        return {
            "status": "complete",
            "report": {
                "summary": "Atlas recommends volatility-adjusted stop buffers and regime transition filters.",
                "recommendations": ["widen_stop_in_news_shock", "filter_exhaustion_bars"],
            },
        }


class _StubForge:
    def __init__(self):
        self.requests: List[Dict[str, Any]] = []

    def act(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        self.requests.append({"task": task, "context": context})
        return {"status": "accepted", "task": task}


def test_zero_hardcoded_variables_all_subagents_learn_from_outcomes(tmp_path: Path):
    store = OracleV2LearningStore(storage_dir=tmp_path)
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    scout = OracleScoutSubAgent(store, ledger)
    portfolio = OraclePortfolioSubAgent(store, ledger)
    risk = OracleRiskSubAgent(store, ledger)

    # 1. Scout starts with uniform maximum-entropy weights (1/6 each)
    initial_weights = scout.learned_dimension_weights()
    assert len(initial_weights) == len(SCOUT_DIMENSIONS)
    for w in initial_weights.values():
        assert abs(w - (1.0 / len(SCOUT_DIMENSIONS))) < 1e-4

    # Feed outcomes where sentinel_news and technical_blueprint strongly predict wins
    for _ in range(12):
        scout.learn_from_realized_trade(
            symbol="EURUSD",
            regime="trending_up",
            direction="long",
            entry_conviction=0.72,
            won=True,
            pnl_r=1.8,
            dimension_scores={
                "technical_blueprint": 0.92,
                "sentinel_news": 0.88,
                "pulse_social": 0.25,
                "chronicle_memory": 0.50,
                "htf_structure": 0.85,
                "spread_efficiency": 0.80,
            },
        )
    updated_weights = scout.learned_dimension_weights()
    assert updated_weights["technical_blueprint"] > initial_weights["technical_blueprint"]
    assert updated_weights["sentinel_news"] > initial_weights["sentinel_news"]
    assert updated_weights["pulse_social"] < initial_weights["pulse_social"]

    # 2. Portfolio learns session slot budget, daily trade budget, and Kelly risk fraction
    for _ in range(8):
        portfolio.record_trade_outcome(
            symbol="XAUUSD",
            asset_class="metals",
            won=True,
            pnl_r=2.1,
            active_concurrency=4,
            daily_trade_index=4,
        )
    for _ in range(8):
        portfolio.record_trade_outcome(
            symbol="EURUSD",
            asset_class="fx",
            won=False,
            pnl_r=-1.0,
            active_concurrency=2,
            daily_trade_index=9,
        )

    assert portfolio.learned_session_slot_budget() == 4
    assert portfolio.learned_daily_trade_budget() < 9
    kelly_fraction = portfolio.learned_kelly_risk_fraction(current_drawdown_pct=0.0, confidence=0.80)
    assert 0.002 <= kelly_fraction <= 0.025

    # 3. Risk learns post-loss cooldown and news/social multipliers empirically
    base_loss_cd = risk.learned_cooldown_seconds(" EURUSD ".strip(), "loss")
    for _ in range(6):
        risk.learn_from_realized_trade(
            symbol="EURUSD",
            won=False,
            pnl_r=-1.2,
            news_impact="medium",
            social_risk="medium",
            seconds_since_prior_trade=300.0,
            prior_trade_won=False,
        )
    adapted_loss_cd = risk.learned_cooldown_seconds("EURUSD", "loss")
    assert adapted_loss_cd > base_loss_cd
    assert risk.learned_news_multipliers()["medium"] < 0.70
    assert risk.learned_social_multipliers()["medium"] < 0.70


def test_cross_sectional_scout_tournament_ranks_setups_and_logs_unentered_trades(tmp_path: Path):
    sentinel = _StubSentinel(high_impact_symbols=["USDJPY"])
    pulse = _StubPulse(manipulated_symbols=["BTCUSD"])
    oracle = OracleAgent(
        sentinel_client=sentinel,
        pulse_client=pulse,
        equity=10000.0,
    )
    # Isolate V2 learning and decision ledger in tmp_path
    oracle.decision_ledger = OracleDecisionLedger(storage_dir=tmp_path)
    oracle.cio = OracleCIOEngine(
        oracle_agent=oracle,
        storage_dir=tmp_path,
        decision_ledger=oracle.decision_ledger,
    )

    # Confirm lazy loading: legacy V1 evolution/lab/backtester are NOT instantiated in RAM
    assert oracle._backtester is None
    assert oracle._lab is None

    # Provide clean trending market series across 6 distinct asset symbols
    symbols = ["EURUSD", "XAUUSD", "USOIL", "NAS100", "USDJPY", "BTCUSD"]
    series_map = {
        "EURUSD": _make_trending_series("EURUSD", 1.0800, 0.0025),
        "XAUUSD": _make_trending_series("XAUUSD", 2300.0, 6.5),
        "USOIL": _make_trending_series("USOIL", 75.0, 0.35),
        "NAS100": _make_trending_series("NAS100", 18000.0, 45.0),
        "USDJPY": _make_trending_series("USDJPY", 150.0, 0.30),
        "BTCUSD": _make_trending_series("BTCUSD", 65000.0, 220.0),
    }
    oracle.data.get = lambda sym: {
        "status": "complete",
        "symbol": sym.upper(),
        "source": "unit_test",
        "series": series_map[sym.upper()],
    }

    # Run CIO tournament with session slot override = 2 so top 2 are selected and others are logged
    result = oracle.act("cio.scout_cycle", {"symbols": symbols, "max_session_slots": 2})
    assert result["status"] == "complete"
    selected = result["selected_trades"]
    assert len(selected) == 2
    # High-impact news (USDJPY) and social manipulation (BTCUSD) must be rejected
    selected_syms = {item["symbol"] for item in selected}
    assert "USDJPY" not in selected_syms
    assert "BTCUSD" not in selected_syms

    # Verify Decision Ledger recorded every unentered trade with explicit reasons
    unentered = oracle.decision_ledger.load_records(event_type="TRADE_NOT_ENTERED")
    unentered_by_sym = {rec["symbol"]: rec for rec in unentered}
    assert "USDJPY" in unentered_by_sym
    assert unentered_by_sym["USDJPY"]["news_impact"] == "high"
    assert "BTCUSD" in unentered_by_sym
    assert unentered_by_sym["BTCUSD"]["social_risk"] == "high"

    # At least 2 valid candidates were outranked for session slots and logged with hypothetical levels
    outranked = [
        rec for rec in unentered
        if rec.get("rejection_stage") == "outranked_for_session_slots"
    ]
    assert len(outranked) >= 2
    for rec in outranked:
        assert rec["hypothetical_entry_price"] > 0
        assert rec["hypothetical_stop"] > 0
        assert rec["hypothetical_target"] > 0
        assert rec["tournament_rank"] >= 3


def test_counterfactual_replay_and_atlas_forge_deficiency_loop(tmp_path: Path):
    atlas = _StubAtlas()
    forge = _StubForge()
    sentinel = _StubSentinel(degraded=True)
    pulse = _StubPulse()

    oracle = OracleAgent(
        sentinel_client=sentinel,
        pulse_client=pulse,
        atlas_client=atlas,
        forge_client=forge,
        equity=10000.0,
    )
    oracle.decision_ledger = OracleDecisionLedger(storage_dir=tmp_path)
    oracle.cio = OracleCIOEngine(
        oracle_agent=oracle,
        storage_dir=tmp_path,
        decision_ledger=oracle.decision_ledger,
    )

    series = _make_trending_series("EURUSD", 1.1000, 0.0030, n=90)
    oracle.data.get = lambda sym: {
        "status": "complete",
        "symbol": sym.upper(),
        "source": "unit_test",
        "series": series,
    }

    # 1. Degraded Sentinel stream must be explicitly exposed in the ledger (Zero Silent Failures)
    oracle.act("trade.propose", {"symbol": "EURUSD"})
    failures = oracle.decision_ledger.load_records(event_type="SYSTEM_FAILURE_EXPOSED")
    assert any(f["component"] == "Sentinel" and f["error_type"] == "DegradedNewsStream" for f in failures)

    # 2. Record an unentered trade and replay it counterfactually against bars
    last_bar_close = series.bars[-1].close
    oracle.decision_ledger.record_trade_not_entered(
        symbol="EURUSD",
        direction="long",
        rejection_stage="below_learned_conviction_floor",
        primary_reason="scout_score 0.49 < floor 0.52",
        reasons=["scout_score 0.49 < floor 0.52"],
        scout_score=0.49,
        entry_confidence=0.49,
        learned_conviction_floor=0.52,
        regime="trending_up",
        hypothetical_entry_price=series.bars[10].close,
        hypothetical_stop=series.bars[10].close - 0.0050,
        hypothetical_target=series.bars[10].close + 0.0075,
        atr=0.0025,
    )
    assert last_bar_close > series.bars[10].close + 0.0075

    replay_res = oracle.act("cio.counterfactual_replay", {"max_records": 10})
    assert replay_res["status"] == "complete"
    assert replay_res["replayed_count"] >= 1
    assert replay_res["missed_winners"] >= 1

    # 3. Record realized losses and verify Oracle queries Atlas and dispatches a mandate to Forge
    for i in range(3):
        oracle.cio.record_realized_outcome(
            symbol="EURUSD",
            direction="long",
            entry_price=1.1000,
            exit_price=1.0950,
            initial_stop=1.0950,
            initial_target=1.1100,
            won=False,
            pnl_r=-1.0,
            exit_reason="stop_loss",
            duration_sec=600.0,
            entry_confidence=0.60,
            exit_confidence=0.35,
            entry_regime="trending_up",
            exit_regime="ranging",
            close_event_id=f"loss-{i}",
        )

    review = oracle.act("cio.atlas_forge_review", {"symbols": ["EURUSD"]})
    assert review["status"] == "complete"
    assert len(review["deficiencies_detected"]) == 1
    assert len(atlas.queries) >= 1
    assert len(review["atlas_insights"]) >= 1
    assert len(forge.requests) >= 1
    assert forge.requests[0]["task"] == "oracle.improvement.request"
    assert forge.requests[0]["context"]["request"]["symbol"] == "EURUSD"


def test_trade_learning_delegates_reevolution_to_forge_and_updates_cio(tmp_path: Path):
    import time
    from Oracle.intelligence.trade_learning import ChampionConfidenceTracker, TradeLearningEngine
    from Oracle.benchmarks.trading_benchmark import TradingBenchmark

    forge = _StubForge()
    sentinel = _StubSentinel()
    pulse = _StubPulse()
    oracle = OracleAgent(
        sentinel_client=sentinel,
        pulse_client=pulse,
        forge_client=forge,
        equity=10000.0,
    )
    oracle.decision_ledger = OracleDecisionLedger(storage_dir=tmp_path)
    oracle.cio = OracleCIOEngine(
        oracle_agent=oracle,
        storage_dir=tmp_path,
        decision_ledger=oracle.decision_ledger,
    )

    tracker = ChampionConfidenceTracker(store_path=tmp_path / "champion_confidence.json")
    benchmark = TradingBenchmark(storage_dir=tmp_path)
    learner = TradeLearningEngine(confidence_tracker=tracker, benchmark=benchmark)

    # Pre-seed 14 losses so the 15th loss crosses MIN_TRADES_FOR_STATUS (15) with live_confidence < RETIRE_THRESHOLD (0.35)
    for _ in range(14):
        tracker.record_outcome("EURUSD", "trending_up", won=False, pnl_r=-1.0)

    pos = SimpleNamespace(
        symbol="EURUSD",
        direction=SimpleNamespace(value="buy"),
        entry_price=1.1000,
        initial_stop=1.0950,
        initial_target=1.1100,
        entry_confidence=0.68,
        entry_regime="trending_up",
        entry_time=time.time() - 900.0,
        entry_streams={
            "technical": {"direction": 0.6, "confidence": 0.75},
            "news": {"direction": 0.5, "confidence": 0.80},
            "social": {"direction": 0.4, "confidence": 0.70},
            "memory": {"direction": 0.0, "confidence": 0.0},
        },
        entry_term_evidence={"bullish": ["inflows"], "bearish": []},
        journal_id=None,
        execution_environment="paper",
        strategy_id="champ-eurusd-v1",
    )

    outcome = learner.record_close(
        oracle,
        pos,
        exit_price=1.0950,
        exit_confidence=0.25,
        exit_regime="ranging",
        exit_reason="stop_loss",
        close_event_id="close-15",
    )
    assert outcome.won is False
    assert outcome.pnl_r == -1.0

    # Wait briefly for background auto-reevolve thread to dispatch to Forge
    for _ in range(20):
        if forge.requests:
            break
        time.sleep(0.02)

    # Confirm Oracle V2 delegated retraining to Forge rather than running in-process evolution
    assert oracle._evolution is None
    assert len(forge.requests) >= 1
    closed_records = oracle.decision_ledger.load_records("TRADE_CLOSED")
    assert len(closed_records) == 1
    assert closed_records[0]["symbol"] == "EURUSD"
    assert closed_records[0]["won"] is False


import tempfile
import unittest


class OracleV2InstitutionalCIOTests(unittest.TestCase):
    def test_1_zero_hardcoded_variables(self):
        with tempfile.TemporaryDirectory() as td:
            test_zero_hardcoded_variables_all_subagents_learn_from_outcomes(Path(td))

    def test_2_cross_sectional_scout_tournament(self):
        with tempfile.TemporaryDirectory() as td:
            test_cross_sectional_scout_tournament_ranks_setups_and_logs_unentered_trades(Path(td))

    def test_3_counterfactual_replay_and_atlas_forge_loop(self):
        with tempfile.TemporaryDirectory() as td:
            test_counterfactual_replay_and_atlas_forge_deficiency_loop(Path(td))

    def test_4_trade_learning_delegates_to_forge_and_updates_cio(self):
        with tempfile.TemporaryDirectory() as td:
            test_trade_learning_delegates_reevolution_to_forge_and_updates_cio(Path(td))


if __name__ == "__main__":
    unittest.main()


