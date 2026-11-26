from types import SimpleNamespace
import threading

from Oracle.execution.ctrader_demo_trader import (
    CTraderDemoTrader,
    DEFAULT_SYMBOLS,
    SymbolMapper,
    _LIVE_PRESET,
    _duplicate_broker_symbol_mappings,
    _is_trading_disabled_rejection,
    _call_with_timeout,
)
from Oracle.core.risk import Position as RiskPosition


def test_live_preset_has_ten_symbols_without_disabled_oil_and_adds_top_champions():
    assert len(_LIVE_PRESET) == 10
    assert len(set(_LIVE_PRESET)) == 10
    assert "USOIL" not in _LIVE_PRESET
    assert not {"BTCUSD", "ETHUSD"}.intersection(_LIVE_PRESET)
    assert {"NZDUSD", "EURGBP"}.issubset(_LIVE_PRESET)
    assert {"VIX", "MSFT", "USDCAD"}.issubset(_LIVE_PRESET)


def test_default_ctrader_preset_skips_duplicate_broker_instruments():
    mapper = SymbolMapper()
    mapper.build(
        ["US500", "US100", "US30", "UK100", "DE40", "JP225"],
        DEFAULT_SYMBOLS,
    )

    duplicates = _duplicate_broker_symbol_mappings(DEFAULT_SYMBOLS, mapper)

    assert duplicates["SPX"] == "US500"
    assert duplicates["NASDAQ"] == "NAS100"
    assert duplicates["DJI"] == "US30"
    assert duplicates["FTSE"] == "UK100"
    assert duplicates["DAX"] == "GER40"
    assert duplicates["NIKKEI"] == "JPN225"


def test_trading_disabled_rejection_is_recognized_for_instrument_skip():
    assert _is_trading_disabled_rejection({
        "status": "rejected",
        "reason": "TRADING_DISABLED: Trading is disabled",
    })
    assert not _is_trading_disabled_rejection({
        "status": "rejected",
        "reason": "INVALID_REQUEST: Invalid stop loss",
    })
    assert not _is_trading_disabled_rejection({
        "status": "filled",
        "reason": "TRADING_DISABLED",
    })


def _kill_switch_trader(account_response, *, start_equity=10_000.0,
                        cached_equity=10_000.0):
    trader = CTraderDemoTrader.__new__(CTraderDemoTrader)
    trader._ks_fired = False
    trader._ks_check_unavailable = False
    trader._start_equity = start_equity
    trader._ks_equity_cache = cached_equity
    trader._ks_equity_ts = 0.0
    trader._ks_equity_ttl = 0.0
    trader.session_max_loss_pct = 0.05
    trader.broker = SimpleNamespace(
        status=SimpleNamespace(connected=True),
        account=lambda: account_response,
    )
    # These tests exercise only kill-switch decisions; dashboard persistence
    # requires a fully populated cTrader session and is covered separately.
    trader._persist_dashboard_snapshot = lambda **_kwargs: None
    return trader


def test_account_timeout_does_not_trigger_kill_switch_or_zero_last_equity():
    trader = _kill_switch_trader({
        "account_info_available": False,
        "equity": 0.0,
    })

    assert trader._kill_switch_check() is False
    assert trader._ks_check_unavailable is True
    assert trader._ks_equity_cache == 10_000.0
    assert trader._ks_fired is False


def test_valid_profitable_equity_does_not_trigger_kill_switch():
    trader = _kill_switch_trader({
        "account_info_available": True,
        "equity": 10_500.0,
    })

    assert trader._kill_switch_check() is False
    assert trader._ks_equity_cache == 10_500.0
    assert trader._ks_fired is False


def test_valid_equity_loss_still_triggers_kill_switch():
    trader = _kill_switch_trader({
        "account_info_available": True,
        "equity": 9_000.0,
    })

    assert trader._kill_switch_check() is True
    assert trader._ks_fired is True


def test_first_valid_account_refresh_sets_session_baseline():
    trader = _kill_switch_trader(
        {"account_info_available": True, "equity": 12_500.0},
        start_equity=None,
        cached_equity=None,
    )

    assert trader._kill_switch_check() is False
    assert trader._start_equity == 12_500.0
    assert trader._ks_equity_cache == 12_500.0


def test_timed_out_symbol_call_remains_single_flight_until_it_finishes(monkeypatch):
    import Oracle.execution.ctrader_demo_trader as trader_module

    monkeypatch.setattr(trader_module, "_symbol_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(trader_module, "_symbol_futures", {})

    release = threading.Event()
    started = threading.Event()
    calls = []

    def blocked_call():
        assert threading.current_thread().daemon is True
        calls.append("started")
        started.set()
        release.wait(2)
        return {"status": "complete"}

    _, timed_out = _call_with_timeout(blocked_call, 0.01, work_key=("trader", "EURUSD"))
    assert timed_out is True
    assert started.wait(1)
    future = trader_module._symbol_futures[("trader", "EURUSD")]

    late_result, duplicate_timed_out = _call_with_timeout(
        lambda: calls.append("duplicate"), 0.01, work_key=("trader", "EURUSD"))

    assert duplicate_timed_out is True
    assert late_result is None
    assert calls == ["started"]
    # The sole executor slot is still held by the timed-out Oracle call.
    _, capacity_timed_out = _call_with_timeout(
        lambda: calls.append("other symbol"), 0.01, work_key=("trader", "GBPUSD"))
    assert capacity_timed_out is True
    release.set()
    assert future.result(timeout=1)["status"] == "complete"


def test_broker_position_close_signals_scan_to_refresh_capacity():
    trader = CTraderDemoTrader.__new__(CTraderDemoTrader)
    trader._position_state_lock = threading.Lock()
    trader._capacity_release_event = threading.Event()
    trader._broker_position_signature = None

    trader._observe_broker_positions([
        {"ticket": 1, "symbol": "EURUSD"},
        {"ticket": 2, "symbol": "GBPUSD"},
        {"ticket": 3, "symbol": "XRPUSD"},
    ])
    assert not trader._capacity_release_event.is_set()

    released = trader._observe_broker_positions([
        {"ticket": 1, "symbol": "EURUSD"},
        {"ticket": 3, "symbol": "XRPUSD"},
    ])

    assert released is True
    assert trader._capacity_release_event.is_set()


def test_broker_snapshot_replaces_only_live_risk_positions():
    trader = CTraderDemoTrader.__new__(CTraderDemoTrader)
    trader.symbols = ["EURUSD", "GBPUSD"]
    trader._sym_mapper = SymbolMapper()
    trader._sym_mapper.build(["EURUSD", "GBPUSD"], trader.symbols)
    trader._manage_lock = threading.Lock()
    portfolio = SimpleNamespace(
        positions=[RiskPosition("USDJPY", "long", 1000, 150.0, 149.0, 152.0)],
        trades_today={"EURUSD": 2},
        last_loss_at={"GBPUSD": 123.0},
    )
    trader.oracle = SimpleNamespace(risk=SimpleNamespace(portfolio=portfolio))

    trader._sync_risk_positions_from_broker([
        {"ticket": 10, "symbol": "EURUSD", "type": "buy", "volume": 1000,
         "price_open": 1.08, "sl": 1.07, "tp": 1.10},
    ])

    assert [(p.symbol, p.direction, p.size) for p in portfolio.positions] == [
        ("EURUSD", "long", 1000.0),
    ]
    assert portfolio.trades_today == {"EURUSD": 2}
    assert portfolio.last_loss_at == {"GBPUSD": 123.0}
