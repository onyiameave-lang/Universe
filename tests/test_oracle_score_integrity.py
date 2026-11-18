from Forge.core.institutional_standard import LearnedPenaltyAndNecessityEngine
from Forge.core.shadow_runner import MT5ShadowExecutionBridge
from Oracle.core.risk import RiskManager
from Oracle.risk_agent.cooldown_and_shocks import compute_learned_stop_target_multipliers


def _policy():
    return {
        "learned_near_tp_zone_pct": 0.8,
        "penalty_hierarchy": {
            "learned_major_sl_penalty_r": 2.0,
            "learned_minor_early_exit_base_penalty_r": 0.35,
        },
    }


def test_near_target_defensive_exit_is_not_reported_as_tp_fill():
    result = LearnedPenaltyAndNecessityEngine.grade_closed_trade_with_penalties(
        realized_r=1.9,
        initial_major_tp_r=2.0,
        final_major_tp_r=2.0,
        shadow_status="CLOSED_NEAR_TP_REVERSAL_PROTECTION",
        policy=_policy(),
    )

    assert result["outcome_class"] == "NEAR_TP_DEFENSIVE_SAVE_MINOR_PENALTY"
    assert result["is_near_tp_save"] is True
    assert result["is_true_tp_win"] is False
    assert result["penalty_tier"] == "MINOR_PENALTY"


def test_partial_target_expansion_then_time_exit_is_not_full_tp_win():
    result = LearnedPenaltyAndNecessityEngine.grade_closed_trade_with_penalties(
        realized_r=2.3,
        initial_major_tp_r=2.0,
        final_major_tp_r=3.0,
        shadow_status="CLOSED_TIME_HORIZON_AFTER_1_EXPANSIONS",
        policy=_policy(),
    )

    assert result["is_true_tp_win"] is False
    assert result["counterfactual_audit"] != "REACHED_MAIN_TAKE_PROFIT"


def test_only_explicit_target_exit_statuses_count_as_tp_wins():
    for status in ("CLOSED_AT_MAJOR_TP", "CLOSED_AT_EXPANDED_MAJOR_TP_2"):
        result = LearnedPenaltyAndNecessityEngine.grade_closed_trade_with_penalties(
            realized_r=2.0,
            initial_major_tp_r=2.0,
            final_major_tp_r=2.0,
            shadow_status=status,
            policy=_policy(),
        )
        assert result["is_true_tp_win"] is True
        assert result["counterfactual_audit"] == "REACHED_MAIN_TAKE_PROFIT"


def test_learned_risk_rejects_stop_wider_than_target():
    risk = RiskManager(equity=10_000)
    risk.stop_mult = 2.4
    risk.target_mult = 1.2

    plan = risk.evaluate("BTCUSD", "long", entry=60_000, atr=500, confidence=0.99)

    assert plan["approved"] is False
    assert any("below configured minimum" in reason for reason in plan["reasons"])


def test_risk_plan_reports_rr_from_actual_learned_levels():
    risk = RiskManager(equity=10_000)
    risk.stop_mult = 1.2
    risk.target_mult = 2.4

    plan = risk.evaluate("BTCUSD", "long", entry=60_000, atr=500, confidence=0.99)

    assert plan["approved"] is True
    assert plan["reward_risk"] == 2.0


def test_stop_target_learner_uses_losses_as_well_as_wins():
    stop_mult, target_mult = compute_learned_stop_target_multipliers({
        "geometry_outcomes": [
            {"stop_atr_mult": 1.0, "target_atr_mult": 3.0, "won": True, "pnl_r": 2.0},
            {"stop_atr_mult": 3.0, "target_atr_mult": 1.0, "won": False, "pnl_r": -1.0},
        ]
    })

    assert (stop_mult, target_mult) == (2.0, 2.0)


def test_profitable_non_tp_broker_close_is_not_counted_as_tp_win():
    class _Status:
        connected = True

    class _Broker:
        status = _Status()

        def get_closed_deals(self, *, symbol, lookback_days):
            return [{
                "position_id": 1,
                "net_profit_usd": 100.0,
                "exit_reason": "CLIENT_OR_EXPERT_CLOSE",
            }]

    bridge = object.__new__(MT5ShadowExecutionBridge)
    bridge.broker = _Broker()
    bridge.resolve_broker_symbol = lambda symbol: symbol
    bridge.get_account_equity_usd = lambda: 10_000.0

    result = bridge.reconcile_mt5_closed_deals(
        symbol="BTCUSD",
        management_policy={"learned_initial_major_tp_r": 2.0},
    )

    assert result["mt5_real_wins"] == 0
    assert result["mt5_real_scratches"] == 1
    assert result["reconciled_deals"][0]["penalty_audit"]["is_true_tp_win"] is False
