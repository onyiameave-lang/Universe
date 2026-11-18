from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

from Oracle.cio.counterfactual_replayer import replay_unentered_counterfactuals
from Oracle.core.market_data import Bar, Series
from Oracle.intelligence.decision_ledger import OracleDecisionLedger
from Oracle.scout_agent.learner import compare_strategy_performance, update_scout_from_counterfactual
from Oracle.execution.shadow_feedback import record_max_positions_shadow
from Oracle.agents.oracle_agent import _is_capacity_only_rejection


class _Scout:
    def __init__(self):
        self.learned = []

    def learn_from_counterfactual(self, **kwargs):
        self.learned.append(kwargs)
        return {"status": "complete"}

    def learned_conviction_floor(self):
        return 0.5


class _Data:
    def __init__(self, series):
        self.series = series
        self.calls = 0

    def get(self, symbol, **kwargs):
        self.calls += 1
        return {"status": "complete", "series": self.series, "source": self.series.source}


def _bars(ts, *, high, low, close=100.0):
    return [Bar(ts=ts, open=100.0, high=high, low=low, close=close, volume=10.0)]


def _candidate(ledger, *, observed_at, symbol="EURUSD", shadow=True, strategy="genome-7"):
    return ledger.record_trade_not_entered(
        symbol=symbol,
        direction="long",
        rejection_stage="max_positions_shadow",
        primary_reason="account position limit reached",
        scout_score=0.8,
        entry_confidence=0.75,
        regime="trending_up",
        session="london",
        strategy_id=strategy,
        hypothetical_entry_price=100.0,
        hypothetical_stop=95.0,
        hypothetical_target=110.0,
        shadow_observation=shadow,
        dimension_scores={"technical_blueprint": 0.8},
        observed_at_unix=observed_at,
    )


def _decision_time():
    return datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def test_shadow_candidate_replays_only_later_broker_bars_and_updates_strategy_stats(tmp_path):
    decision_at = _decision_time()
    later = datetime.fromtimestamp(decision_at, timezone.utc) + timedelta(minutes=15)
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    candidate = _candidate(ledger, observed_at=decision_at)
    scout = _Scout()
    data = _Data(Series(
        symbol="EURUSD",
        source="ctrader:EURUSD",
        bars=_bars(later.isoformat(), high=111.0, low=99.0, close=110.5),
    ))

    result = replay_unentered_counterfactuals(
        oracle_agent=SimpleNamespace(data=data),
        scout_agent=scout,
        decision_ledger=ledger,
        shadow_only=True,
    )
    assert result["replayed_count"] == 1, result
    replay = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY")[-1]
    assert replay["original_event_id"] == candidate["event_id"]
    assert replay["classification"] == "MISSED_WINNER"
    assert replay["counterfactual_r"] == 2.0
    assert replay["strategy_id"] == "genome-7"
    assert replay["data_timeframe"] == "M15"
    assert replay["pnl_basis"].endswith("costs_and_slippage_not_modeled")
    assert scout.learned[0]["strategy_id"] == "genome-7"
    assert scout.learned[0]["session"] == "london"
    shadow_summary = ledger.summarize_diagnostics()["shadow_observations"]
    assert shadow_summary["candidates"] == 1
    assert shadow_summary["settled"] == 1
    assert shadow_summary["mean_r"] == 2.0
    assert shadow_summary["separate_from_realized_trades"] is True


def test_ambiguous_stop_and_target_bar_resolves_against_candidate(tmp_path):
    decision_at = _decision_time()
    later = datetime.fromtimestamp(decision_at, timezone.utc) + timedelta(minutes=15)
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    _candidate(ledger, observed_at=decision_at)
    result = replay_unentered_counterfactuals(
        oracle_agent=SimpleNamespace(data=_Data(Series(
            symbol="EURUSD", source="broker:ctrader",
            bars=_bars(later.isoformat(), high=111.0, low=94.0),
        ))),
        scout_agent=_Scout(),
        decision_ledger=ledger,
        shadow_only=True,
    )

    replay = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY")[-1]
    assert result["avoided_losses"] == 1
    assert replay["counterfactual_r"] == -1.0
    assert replay["exit_reason"] == "ambiguous_bar_stop_first"
    assert replay["ambiguous_bar"] is True


def test_shadow_replay_only_processes_new_completed_bars_until_exit(tmp_path):
    decision_at = _decision_time()
    first_bar = datetime.fromtimestamp(decision_at, timezone.utc) + timedelta(minutes=15)
    second_bar = first_bar + timedelta(minutes=15)
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    _candidate(ledger, observed_at=decision_at)
    scout = _Scout()
    data = _Data(Series(
        symbol="EURUSD", source="broker:ctrader",
        bars=_bars(first_bar.isoformat(), high=105.0, low=99.0),
    ))
    args = {
        "oracle_agent": SimpleNamespace(data=data),
        "scout_agent": scout,
        "decision_ledger": ledger,
        "shadow_only": True,
    }

    first = replay_unentered_counterfactuals(**args)
    progress = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY_PROGRESS")
    assert first["still_open"] == 1
    assert len(progress) == 1
    assert progress[0]["bars_observed"] == 1

    repeated = replay_unentered_counterfactuals(**args)
    assert repeated["still_open"] == 1
    assert len(ledger.read_all(event_type="COUNTERFACTUAL_REPLAY_PROGRESS")) == 1

    data.series = Series(
        symbol="EURUSD", source="broker:ctrader",
        bars=[
            *_bars(first_bar.isoformat(), high=105.0, low=99.0),
            *_bars(second_bar.isoformat(), high=111.0, low=99.0),
        ],
    )
    settled = replay_unentered_counterfactuals(**args)
    assert settled["replayed_count"] == 1
    replay = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY")[-1]
    assert replay["bars_observed"] == 2
    assert replay["exit_reason"] == "take_profit"


def test_virtual_trade_manager_trailing_stop_survives_replay_restart(tmp_path):
    decision_at = _decision_time()
    first_bar = datetime.fromtimestamp(decision_at, timezone.utc) + timedelta(minutes=15)
    second_bar = first_bar + timedelta(minutes=15)
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    _candidate(ledger, observed_at=decision_at)
    scout = _Scout()
    data = _Data(Series(
        symbol="EURUSD", source="ctrader:EURUSD",
        bars=[
            Bar(ts=first_bar.isoformat(), open=100.0, high=106.0, low=99.0, close=105.0, volume=10.0),
        ],
    ))
    args = {
        "oracle_agent": SimpleNamespace(data=data),
        "scout_agent": scout,
        "decision_ledger": ledger,
        "shadow_only": True,
    }

    first = replay_unentered_counterfactuals(**args)
    assert first["still_open"] == 1
    state = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY_PROGRESS")[-1]["position_state"]
    assert state["current_stop"] == 101.0
    assert state["management_actions"][-1]["action"] == "tighten_stop"

    data.series = Series(
        symbol="EURUSD", source="ctrader:EURUSD",
        bars=[
            Bar(ts=first_bar.isoformat(), open=100.0, high=106.0, low=99.0, close=105.0, volume=10.0),
            Bar(ts=second_bar.isoformat(), open=100.0, high=102.0, low=100.0, close=101.0, volume=10.0),
        ],
    )
    settled = replay_unentered_counterfactuals(**args)
    assert settled["replayed_count"] == 1
    replay = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY")[-1]
    assert replay["exit_reason"] == "stop_loss"
    assert replay["final_stop_price"] == 101.0
    assert replay["max_favorable_excursion_r"] == 1.2
    assert replay["management_actions"]


def test_live_snapshot_uses_same_manager_for_hostile_regime_exit(tmp_path):
    decision_at = _decision_time()
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    _candidate(ledger, observed_at=decision_at)
    result = replay_unentered_counterfactuals(
        oracle_agent=SimpleNamespace(data=_Data(Series(
            symbol="EURUSD", source="ctrader:EURUSD", bars=[],
        ))),
        scout_agent=_Scout(),
        decision_ledger=ledger,
        shadow_only=True,
        current_snapshots={"EURUSD": {
            "price": 99.0, "confidence": 0.75, "regime": "trending_down",
            "observed_at_unix": time.time(),
        }},
    )
    assert result["replayed_count"] == 1
    replay = ledger.read_all(event_type="COUNTERFACTUAL_REPLAY")[-1]
    assert replay["exit_reason"].startswith("trade_manager:regime flipped hostile")
    assert replay["counterfactual_r"] == -0.2


def test_shadow_only_filter_and_future_bar_requirement(tmp_path):
    decision_at = _decision_time()
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    _candidate(ledger, observed_at=decision_at, shadow=False, strategy="real-rejection")
    shadow_candidate = _candidate(ledger, observed_at=decision_at, shadow=True)
    scout = _Scout()
    data = _Data(Series(
        symbol="EURUSD", source="broker:ctrader",
        bars=_bars(datetime.fromtimestamp(decision_at, timezone.utc).isoformat(), high=111.0, low=99.0),
    ))

    result = replay_unentered_counterfactuals(
        oracle_agent=SimpleNamespace(data=data),
        scout_agent=scout,
        decision_ledger=ledger,
        shadow_only=True,
    )

    assert result["replayed_count"] == 0
    assert result["still_open"] == 1
    assert not scout.learned
    assert data.calls == 1
    pending = ledger.unentered_trades_pending_replay(shadow_only=True)
    assert [row["event_id"] for row in pending] == [shadow_candidate["event_id"]]


def test_scout_compares_realized_and_shadow_strategy_evidence_separately():
    comparison = compare_strategy_performance(
        {
            "strategy_stats": {"EURUSD::trending_up::genome-7": {
                "wins": 3, "losses": 1, "sum_r": 2.0,
            }},
            "counterfactual_strategy_stats": {"EURUSD::trending_up::genome-7": {
                "wins": 4, "losses": 2, "sum_r": 1.0, "observations": 6,
            }},
            "shadow_strategy_stats": {"EURUSD::trending_up::genome-7": {
                "wins": 4, "losses": 2, "sum_r": 1.0, "observations": 6,
            }},
        },
        symbol="EURUSD",
        regime="trending_up",
        strategy_id="genome-7",
    )

    assert comparison["realized"]["observations"] == 4
    assert comparison["shadow"]["observations"] == 6
    assert comparison["realized"]["win_rate"] == 0.75
    assert comparison["shadow"]["mean_r"] == round(1.0 / 6.0, 4)
    assert comparison["shadow_used_for_live_scoring"] is False


def test_shadow_outcomes_do_not_change_realized_or_live_threshold_evidence():
    state = {
        "learned_conviction_floor": 0.5,
        "conviction_outcomes": [],
        "counterfactual_outcomes": [],
    }
    before = state["learned_conviction_floor"]

    update_scout_from_counterfactual(
        state,
        symbol="EURUSD",
        regime="trending_up",
        conviction=0.8,
        stage_rejected="max_positions_shadow",
        counterfactual_r=2.0,
        would_have_won=True,
        strategy_id="genome-7",
        session="london",
        shadow_observation=True,
    )

    assert state["learned_conviction_floor"] == before
    assert not state["counterfactual_outcomes"]
    assert state["shadow_counterfactual_outcomes"][0]["shadow_observation"] is True
    assert "EURUSD::trending_up::genome-7" not in state.get("strategy_stats", {})
    assert state["shadow_strategy_stats"]["EURUSD::trending_up::genome-7"]["wins"] == 1


def test_max_position_rejection_creates_one_deduplicated_shadow_candidate(tmp_path):
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    signal = {
        "scout_score": 0.8,
        "effective_confidence": 0.75,
        "learned_conviction_floor": 0.5,
        "scout_evaluation": {
            "strategy_id": "genome-7",
            "regime": "trending_up",
            "session": "london",
            "dimension_scores": {"technical_blueprint": 0.8},
            "learned_dimension_weights": {"technical_blueprint": 0.5},
        },
    }
    plan = {"direction": "long", "entry": 100.0, "stop": 95.0, "target": 110.0}

    assert record_max_positions_shadow(
        ledger, symbol="EURUSD", plan=plan, signal=signal,
        reason="account position limit reached",
    )
    assert not record_max_positions_shadow(
        ledger, symbol="EURUSD", plan=plan, signal=signal,
        reason="account position limit reached",
    )

    pending = ledger.unentered_trades_pending_replay(shadow_only=True)
    assert len(pending) == 1
    assert pending[0]["strategy_id"] == "genome-7"
    assert pending[0]["dimension_scores"]["technical_blueprint"] == 0.8


def test_shadow_book_allows_only_one_active_position_per_symbol(tmp_path):
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    _candidate(ledger, observed_at=_decision_time(), strategy="genome-7")

    assert ledger.has_pending_shadow_candidate(
        symbol="EURUSD", direction="short", strategy_id="different-genome"
    )
    overlap1 = ledger.record_shadow_candidate_overlap(
        symbol="EURUSD", direction="short", strategy_id="different-genome",
        candidate_context={"entry": 99.0},
    )
    overlap2 = ledger.record_shadow_candidate_overlap(
        symbol="EURUSD", direction="short", strategy_id="different-genome",
        candidate_context={"entry": 98.0},
    )

    assert overlap1["event_type"] == "SHADOW_CANDIDATE_OVERLAP"
    assert overlap2["event_id"] == overlap1["event_id"]
    assert len(ledger.unentered_trades_pending_replay(shadow_only=True)) == 1
    assert len(ledger.read_all(event_type="SHADOW_CANDIDATE_OVERLAP")) == 1


def test_preexisting_same_symbol_shadow_duplicates_become_overlap_observations(tmp_path):
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    first = _candidate(ledger, observed_at=_decision_time(), strategy="genome-first")
    second = _candidate(
        ledger, observed_at=_decision_time() + 60, strategy="genome-second"
    )

    pending = ledger.unentered_trades_pending_replay(shadow_only=True)

    assert [row["event_id"] for row in pending] == [first["event_id"]]
    overlap = ledger.read_all(event_type="SHADOW_CANDIDATE_OVERLAP")[-1]
    assert overlap["active_event_id"] == first["event_id"]
    assert overlap["superseded_candidate_id"] == second["event_id"]


def test_bounded_shadow_replay_batches_rotate_without_starving_candidates(tmp_path):
    ledger = OracleDecisionLedger(storage_dir=tmp_path)
    observed_at = _decision_time()
    for symbol in ("EURUSD", "GBPUSD", "USDJPY"):
        _candidate(ledger, observed_at=observed_at, symbol=symbol)

    first = ledger.unentered_trades_pending_replay(max_items=2, shadow_only=True)
    second = ledger.unentered_trades_pending_replay(max_items=2, shadow_only=True)

    assert {row["symbol"] for row in first} == {"EURUSD", "GBPUSD"}
    assert {row["symbol"] for row in second} == {"EURUSD", "USDJPY"}


def test_only_capacity_exclusively_rejected_candidates_are_shadow_eligible():
    assert _is_capacity_only_rejection(["max positions (3) reached"])
    assert _is_capacity_only_rejection([" Max Positions (10) Reached "])
    assert not _is_capacity_only_rejection([])
    assert not _is_capacity_only_rejection([
        "max positions (3) reached", "drawdown 8.0% >= limit 5%",
    ])
    assert not _is_capacity_only_rejection(["already holding EURUSD"])
