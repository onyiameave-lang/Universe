from types import SimpleNamespace

from Oracle.cio.engine import OracleCIOEngine
from Oracle.scout_agent.learner import (
    blend_strategy_performance,
    compute_learned_session_edge_adjustment,
    update_scout_from_outcome,
)


def _scout_state():
    return {
        "dimension_alpha": {},
        "dimension_beta": {},
        "dimension_weights": {},
        "conviction_outcomes": [],
        "counterfactual_outcomes": [],
        "learned_conviction_floor": 0.5,
        "symbol_regime_stats": {},
        "strategy_stats": {},
        "session_stats": {},
    }


def test_missing_entry_scores_do_not_invent_feature_attribution():
    state = _scout_state()

    update_scout_from_outcome(
        state,
        symbol="EURUSD",
        regime="trending_up",
        session="london",
        entry_conviction=0.7,
        dimension_scores_at_entry={},
        won=False,
        pnl_r=-1.0,
    )

    assert all(value == 1.0 for value in state["dimension_alpha"].values())
    assert all(value == 1.0 for value in state["dimension_beta"].values())


def test_strategy_and_session_evidence_change_estimates_with_shrinkage():
    state = _scout_state()
    for _ in range(10):
        update_scout_from_outcome(
            state,
            symbol="EURUSD",
            regime="trending_up",
            session="asian",
            entry_conviction=0.7,
            dimension_scores_at_entry={},
            won=False,
            pnl_r=-1.0,
            strategy_id="genome-bad",
        )
    for _ in range(10):
        update_scout_from_outcome(
            state,
            symbol="EURUSD",
            regime="trending_up",
            session="london",
            entry_conviction=0.7,
            dimension_scores_at_entry={},
            won=True,
            pnl_r=1.0,
            strategy_id="genome-good",
        )

    win_prior, mean_prior = 0.6, 0.1
    bad_win, bad_mean = blend_strategy_performance(
        state,
        symbol="EURUSD",
        regime="trending_up",
        strategy_id="genome-bad",
        prior_win_probability=win_prior,
        prior_mean_r=mean_prior,
    )
    good_win, good_mean = blend_strategy_performance(
        state,
        symbol="EURUSD",
        regime="trending_up",
        strategy_id="genome-good",
        prior_win_probability=win_prior,
        prior_mean_r=mean_prior,
    )

    assert bad_win < win_prior and bad_mean < mean_prior
    assert good_win > win_prior and good_mean > mean_prior
    assert compute_learned_session_edge_adjustment(state, "asian") < 0
    assert compute_learned_session_edge_adjustment(state, "london") > 0


def test_cio_close_handoff_updates_entry_dimension_strategy_and_session(tmp_path):
    oracle = SimpleNamespace(risk=None, chronicle=None, sentinel=None, pulse=None)
    cio = OracleCIOEngine(oracle_agent=oracle, storage_dir=tmp_path)

    cio.record_realized_outcome(
        symbol="EURUSD",
        direction="long",
        entry_price=1.1,
        exit_price=1.101,
        initial_stop=1.099,
        initial_target=1.103,
        entry_confidence=0.8,
        entry_regime="trending_up",
        entry_session="asian",
        pnl_r=0.5,
        won=True,
        exit_reason="manual_close_profit",
        dimension_scores_at_entry={
            "technical_blueprint": 0.95,
            "sentinel_news": 0.1,
            "pulse_social": 0.1,
            "chronicle_memory": 0.1,
            "htf_structure": 0.1,
            "spread_efficiency": 0.1,
        },
        strategy_id="genome-123",
    )

    scout = cio.learning_store.data["scout"]
    assert scout["dimension_alpha"]["technical_blueprint"] > 1.0
    assert scout["dimension_alpha"]["sentinel_news"] == 1.0
    assert scout["session_stats"]["asian"]["wins"] == 1
    strategy_record = scout["strategy_stats"]["EURUSD::trending_up::genome-123"]
    assert strategy_record["wins"] == 1
    assert scout["conviction_outcomes"][-1]["session"] == "asian"
    assert scout["conviction_outcomes"][-1]["strategy_id"] == "genome-123"


def test_cio_does_not_infer_entry_session_for_recovered_position(tmp_path):
    oracle = SimpleNamespace(risk=None, chronicle=None, sentinel=None, pulse=None)
    cio = OracleCIOEngine(oracle_agent=oracle, storage_dir=tmp_path)

    cio.record_realized_outcome(
        symbol="EURUSD",
        direction="long",
        entry_price=1.1,
        exit_price=1.101,
        initial_stop=1.099,
        initial_target=1.103,
        pnl_r=0.5,
        won=True,
    )

    scout = cio.learning_store.data["scout"]
    assert scout["session_stats"] == {}
    assert scout["conviction_outcomes"][-1]["session"] is None
