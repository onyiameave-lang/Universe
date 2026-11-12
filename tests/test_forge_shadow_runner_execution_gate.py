from types import SimpleNamespace

from Forge.core.shadow_runner import MT5ShadowExecutionBridge


class FakeMT5:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self):
        self.open_positions = []
        self.deals = []

    def account_info(self):
        return SimpleNamespace(equity=100000.0, currency="USD", trade_mode=0)

    def positions_get(self):
        return self.open_positions

    def history_deals_get(self, date_from, date_to):
        return self.deals

    def symbol_select(self, symbol, enabled):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(
            trade_contract_size=100000.0,
            point=0.00001,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_stops_level=10,
            trade_tick_size=0.00001,
            trade_tick_value_loss=1.3264,
            trade_tick_value=1.3264,
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(ask=1.2501, bid=1.2500)

    def order_calc_profit(self, order_type, symbol, volume, price_open, price_close):
        if order_type == self.ORDER_TYPE_BUY:
            return (price_close - price_open) * volume * 100000.0
        return (price_open - price_close) * volume * 100000.0


class FakeBroker:
    def __init__(self, account_type="demo"):
        self.status = SimpleNamespace(
            connected=True, account_type=account_type, trade_mode=None, reason="ok"
        )
        self.paper = False
        self.orders = []
        self._mt5 = FakeMT5()

    def account(self):
        return {
            "connected": self.status.connected,
            "account_type": self.status.account_type,
            "trade_mode": self.status.trade_mode,
            "reason": self.status.reason,
        }

    def _clamp_volume(self, symbol, units):
        return units / 100000.0

    def place_order(self, plan, human_confirm=False):
        self.orders.append(plan)
        return {"status": "filled", "order": 123}


def _bridge(tmp_path, account_type="demo"):
    bridge = MT5ShadowExecutionBridge.__new__(MT5ShadowExecutionBridge)
    bridge.execute_mt5_demo_orders = True
    bridge.broker = FakeBroker(account_type)
    bridge.mt5_logger = None
    bridge.sym_mapper = None
    bridge.order_ledger_path = tmp_path / "signal_orders.sqlite3"
    return bridge


def _execute(
    bridge,
    *,
    current_signal,
    latest_signal,
    signal_id="GBPUSD:123",
    risk_conversion_available=True,
):
    return bridge.execute_or_log_mt5_demo_trade(
        cycle=1,
        symbol="GBPUSD",
        direction="LONG",
        confidence=0.68,
        regime="TRENDING_BULLISH",
        sizing_info={
            "mt5_broker_lots": 0.01,
            "position_units": 1000,
            "risk_conversion_available": risk_conversion_available,
        },
        entry_price=1.25,
        stop_price=1.24,
        target_price=1.27,
        reward_risk=2.0,
        shadow_status="TAKE_PROFIT",
        realized_r=1.0,
        penalty_audit={},
        current_signal=current_signal,
        latest_signal=latest_signal,
        signal_id=signal_id,
    )


def test_historical_replay_does_not_submit_demo_order(tmp_path):
    bridge = _bridge(tmp_path)

    result = _execute(bridge, current_signal=False, latest_signal=False)

    assert result["execution_status"] == "historical_replay_not_executed"
    assert bridge.broker.orders == []


def test_stale_latest_signal_does_not_submit_demo_order(tmp_path):
    bridge = _bridge(tmp_path)

    result = _execute(bridge, current_signal=False, latest_signal=True)

    assert result["execution_status"] == "stale_signal_not_executed"
    assert bridge.broker.orders == []


def test_current_demo_signal_is_submitted_at_most_once(tmp_path):
    bridge = _bridge(tmp_path)

    first = _execute(bridge, current_signal=True, latest_signal=True)
    second = _execute(bridge, current_signal=True, latest_signal=True)

    assert first["execution_status"] == "filled"
    assert second["execution_status"] == "duplicate_signal_skipped"
    assert len(bridge.broker.orders) == 1


def test_current_signal_is_blocked_on_non_demo_account(tmp_path):
    bridge = _bridge(tmp_path, account_type="real")

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "blocked_non_demo_account"
    assert bridge.broker.orders == []


def test_current_signal_is_blocked_with_clear_reason_when_account_type_is_unknown(tmp_path):
    bridge = _bridge(tmp_path, account_type="unknown")
    bridge.broker.status.trade_mode = 7

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "blocked_unverified_account_type"
    assert "trade mode 7" in result["reason"]
    assert bridge.broker.orders == []


def test_current_signal_is_blocked_when_mt5_account_status_cannot_refresh(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.broker.status.connected = False
    bridge.broker.status.reason = "MT5 account_info unavailable"

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "mt5_not_connected"
    assert result["reason"] == "MT5 account_info unavailable"
    assert bridge.broker.orders == []


def test_current_signal_is_blocked_when_currency_conversion_is_unavailable(tmp_path):
    bridge = _bridge(tmp_path)

    result = _execute(
        bridge,
        current_signal=True,
        latest_signal=True,
        risk_conversion_available=False,
    )

    assert result["execution_status"] == "risk_conversion_unavailable"
    assert bridge.broker.orders == []


def test_mt5_symbol_specs_include_loss_tick_value_and_account_currency(tmp_path):
    bridge = _bridge(tmp_path)

    specs = bridge.get_mt5_symbol_specs("EURGBP", 0.83532)

    assert specs["trade_tick_size"] == 0.00001
    assert specs["trade_tick_value_loss"] == 1.3264
    assert specs["account_currency"] == "USD"


def test_current_signal_is_blocked_when_daily_loss_limit_is_reached(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.broker._mt5.deals = [
        SimpleNamespace(type=0, profit=-2001.0, commission=0.0, swap=0.0, fee=0.0)
    ]

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "risk_limit_blocked"
    assert "daily limit" in result["reason"]
    assert bridge.broker.orders == []


def test_current_signal_is_blocked_when_floating_loss_breaches_daily_limit(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.broker._mt5.open_positions = [
        SimpleNamespace(
            symbol="EURUSD",
            volume=0.1,
            type=0,
            price_open=1.25,
            sl=1.24,
            profit=-2001.0,
            swap=0.0,
            ticket=458,
        )
    ]

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "risk_limit_blocked"
    assert "daily limit" in result["reason"]
    assert bridge.broker.orders == []


def test_current_signal_is_blocked_when_aggregate_stop_risk_would_exceed_limit(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.broker._mt5.open_positions = [
        SimpleNamespace(
            symbol="EURUSD",
            volume=1.0,
            type=0,
            price_open=1.25,
            sl=1.23,
            ticket=456,
        )
    ]

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "risk_limit_blocked"
    assert "aggregate limit" in result["reason"]
    assert bridge.broker.orders == []


def test_current_signal_is_blocked_when_open_position_has_no_stop(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.broker._mt5.open_positions = [
        SimpleNamespace(
            symbol="EURUSD",
            volume=0.1,
            type=0,
            price_open=1.25,
            sl=0.0,
            ticket=457,
        )
    ]

    result = _execute(bridge, current_signal=True, latest_signal=True)

    assert result["execution_status"] == "risk_limit_blocked"
    assert "no stop loss" in result["reason"]
    assert bridge.broker.orders == []


def test_dynamic_equity_updates_after_wins_and_losses(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.equity_state_path = tmp_path / "equity_state.json"
    bridge._tracked_equity_usd = 100000.0
    # Simulate offline/paper fallback so tracked equity is used and persisted
    bridge.broker.paper = True

    initial_eq = bridge.get_account_equity_usd()
    assert initial_eq == 100000.0

    # Simulate a -1.25% loss trade -> equity must drop below $100,000
    after_loss = bridge.update_tracked_equity_after_trade(-1.25, realized_pnl_usd=None)
    assert abs(after_loss - 98750.0) < 1e-2
    assert abs(bridge.get_account_equity_usd() - 98750.0) < 1e-2

    # Simulate live MT5 broker connected with real-time equity $97,420.50
    bridge.broker.paper = False
    bridge.broker.account = lambda: {"equity": 97420.50, "balance": 98000.0}
    live_eq = bridge.get_account_equity_usd()
    assert abs(live_eq - 97420.50) < 1e-2


def test_position_conflict_guard_blocks_same_and_opposing_open_positions(tmp_path):
    bridge = _bridge(tmp_path)

    # 1. Existing LONG position open (type=0) -> new LONG signal is blocked from stacking
    bridge.broker.get_positions_by_symbol = lambda sym: [
        {"ticket": 555, "symbol": sym, "type": 0, "volume": 0.1, "price_open": 1.25}
    ]
    same_dir = _execute(bridge, current_signal=True, latest_signal=True, signal_id="GBPUSD:201")
    assert same_dir["execution_status"] == "existing_same_direction_position_open"
    assert same_dir["existing_ticket"] == 555
    assert bridge.broker.orders == []

    # 2. Existing SHORT position open (type=1) -> new LONG signal is blocked by conflict guard
    bridge.broker.get_positions_by_symbol = lambda sym: [
        {"ticket": 556, "symbol": sym, "type": 1, "volume": 0.1, "price_open": 1.26}
    ]
    opp_dir = _execute(bridge, current_signal=True, latest_signal=True, signal_id="GBPUSD:202")
    assert opp_dir["execution_status"] == "existing_opposing_position_open"
    assert opp_dir["existing_ticket"] == 556
    assert bridge.broker.orders == []


def test_manage_open_mt5_positions_modifies_and_closes_live_tickets(tmp_path):
    from Forge.core.mtf_institutional_engine import MultiTimeframeInstitutionalEngine

    bridge = _bridge(tmp_path)
    modified = []
    closed = []

    bridge.broker.get_positions_by_symbol = lambda sym: [
        {
            "ticket": 701,
            "symbol": sym,
            "type": "buy",
            "volume": 0.10,
            "price_open": 1.2500,
            "price_current": 1.2590,
            "sl": 1.2450,
            "tp": 1.2650,
            "profit": 90.0,
        },
    ]
    bridge.broker.modify_position = lambda ticket, stop_loss=None, take_profit=None: (
        modified.append((ticket, stop_loss, take_profit)) or {"status": "modified", "ticket": ticket}
    )
    bridge.broker.close_position = lambda ticket: (
        closed.append(ticket) or {"status": "closed", "ticket": ticket}
    )

    mtf_engine = MultiTimeframeInstitutionalEngine()
    latest_bar = {
        "open": 1.2585,
        "close": 1.2610,
        "high": 1.2620,
        "low": 1.2580,
        "atr14": 0.0020,
        "regime": "TRENDING_BULLISH",
        "ema9": 1.2605,
        "ema21": 1.2575,
        "ema50": 1.2530,
        "rsi14": 64.0,
    }
    htf_bars = [
        {"high": 1.2750, "low": 1.2400, "close": 1.2610, "atr14": 0.0040}
        for _ in range(30)
    ]
    management_policy = {
        "learned_optimal_stop_atr_mult": 1.2,
        "learned_initial_major_tp_r": 2.0,
        "learned_trail_activation_r": 0.8,
        "learned_trail_distance_r": 0.5,
        "learned_necessity_exit_threshold": 0.55,
    }

    summary = bridge.manage_open_mt5_positions(
        symbol="GBPUSD",
        latest_bar=latest_bar,
        macro_bars=htf_bars,
        setup_bars=htf_bars,
        htf_bars=htf_bars,
        learned_genome={"ema_fast_weight": 0.5, "ema_mid_weight": 0.5},
        management_policy=management_policy,
        sentinel_signal=0.45,
        pulse_signal=0.35,
        mtf_engine=mtf_engine,
    )

    assert summary["mt5_connected"] is True
    assert summary["open_positions_count"] == 1
    assert len(summary["actions_taken"]) == 1
    assert summary["actions_taken"][0]["ticket"] == 701


def test_reconcile_mt5_closed_deals_grades_real_losses_and_emits_feedback(tmp_path):
    import json

    bridge = _bridge(tmp_path)
    bridge.equity_state_path = tmp_path / "equity_state.json"
    bridge._tracked_equity_usd = 98450.0
    bridge.broker.account = lambda: {"equity": 98450.0, "balance": 98450.0}
    bridge.broker.get_closed_deals = lambda symbol=None, lookback_days=7: [
        {
            "position_id": 9001,
            "deal_ticket": 19001,
            "symbol": "GBPUSD",
            "direction": "LONG",
            "volume_lots": 0.20,
            "entry_price": 1.2550,
            "exit_price": 1.2490,
            "gross_profit_usd": -120.0,
            "commission_usd": -1.4,
            "swap_usd": 0.0,
            "net_profit_usd": -121.4,
            "exit_reason": "STOP_LOSS",
            "closed_at_unix": 1710000500,
        },
        {
            "position_id": 9002,
            "deal_ticket": 19002,
            "symbol": "GBPUSD",
            "direction": "SHORT",
            "volume_lots": 0.20,
            "entry_price": 1.2550,
            "exit_price": 1.2450,
            "gross_profit_usd": 1200.0,
            "commission_usd": -1.4,
            "swap_usd": 0.0,
            "net_profit_usd": 1198.6,
            "exit_reason": "TAKE_PROFIT",
            "closed_at_unix": 1710000900,
        },
    ]

    feedback_outbox = tmp_path / "forge_strategy_feedback_outbox.jsonl"
    management_policy = {
        "learned_initial_major_tp_r": 2.0,
        "learned_major_sl_penalty_r": 0.85,
        "learned_minor_early_exit_penalty_r": 0.25,
    }

    rec1 = bridge.reconcile_mt5_closed_deals(
        symbol="GBPUSD",
        management_policy=management_policy,
        feedback_outbox_path=feedback_outbox,
    )

    assert rec1["mt5_connected"] is True
    assert rec1["closed_deals_count"] == 2
    assert rec1["mt5_real_wins"] == 1
    assert rec1["mt5_real_losses"] == 1

    lines = [json.loads(line) for line in feedback_outbox.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["source"] == "mt5_real_broker_deal_reconciliation"
    assert lines[0]["penalty_tier"] == "MAJOR_PENALTY"
    assert lines[1]["event_type"] == "forge.mt5_live_deal.true_tp_win_reinforcement"
    assert lines[1]["is_true_tp_win"] is True

    # Second call should deduplicate already-reconciled deals so no new feedback lines are written
    rec2 = bridge.reconcile_mt5_closed_deals(
        symbol="GBPUSD",
        management_policy=management_policy,
        feedback_outbox_path=feedback_outbox,
    )
    lines_after = [json.loads(line) for line in feedback_outbox.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines_after) == 2


def test_load_cached_dataset_self_heals_trailing_extra_json_data(tmp_path):
    import json
    from Forge.core.mtf_institutional_engine import MultiTimeframeInstitutionalEngine

    engine = MultiTimeframeInstitutionalEngine(workspace_root=tmp_path)
    target = engine.mtf_chronicle_dir / "NZDUSD" / "latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    valid_payload = {"schema": "forge.mtf_market_dataset.v2", "symbol": "NZDUSD", "series": {"M15": []}}
    # Simulate corrupted Windows file with trailing 'Extra data' bytes after the root JSON object
    corrupted = json.dumps(valid_payload) + '}}trailing_garbage{"extra": 123}'
    target.write_text(corrupted, encoding="utf-8")

    recovered = engine.load_cached_dataset("NZDUSD")
    assert recovered is not None
    assert recovered["symbol"] == "NZDUSD"
    # Verify the file on disk was self-healed into clean, valid JSON
    re_parsed = json.loads(target.read_text(encoding="utf-8"))
    assert re_parsed["symbol"] == "NZDUSD"


