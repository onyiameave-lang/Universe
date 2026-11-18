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


def test_shadow_mode_still_starts_position_management_thread(monkeypatch):
    import Oracle.execution.ctrader_demo_trader as trader_module

    starts = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def is_alive(self):
            return False

        def start(self):
            starts.append(self.kwargs["name"])

    monkeypatch.setattr(trader_module.threading, "Thread", FakeThread)
    trader = CTraderDemoTrader.__new__(CTraderDemoTrader)
    trader.shadow_mode = True
    trader._manage_thread = None
    trader._manage_stop_event = threading.Event()
    trader._manage_interval = 15

    trader.start_trade_manager()

    assert starts == ["ctm-poll"]


def test_manager_adopts_every_broker_position_even_same_symbol_in_shadow_mode():
    trader = CTraderDemoTrader.__new__(CTraderDemoTrader)
    positions = [
        {"ticket": 101, "symbol": "EURUSD", "type": "buy", "price_open": 1.1,
         "sl": 1.09, "tp": 1.12, "volume": 1000},
        {"ticket": 102, "symbol": "EURUSD", "type": "sell", "price_open": 1.11,
         "sl": 1.12, "tp": 1.09, "volume": 1000},
        {"ticket": 103, "symbol": "GBPUSD", "type": "buy", "price_open": 1.3,
         "sl": 1.29, "tp": 1.32, "volume": 1000},
    ]
    trader.shadow_mode = True
    trader.broker = SimpleNamespace(
        status=SimpleNamespace(connected=True, account_type="demo"),
        _positions_last_refresh_ok=True,
        positions=lambda: positions,
        market_state=lambda _symbol: {"state": "CLOSED"},
    )
    trader._manage_lock = threading.RLock()
    trader._managed_positions = {}
    trader._sym_mapper = SimpleNamespace(translate=lambda symbol: symbol)
    trader._oracle_symbol_for_broker = lambda symbol: symbol
    trader._position_key = CTraderDemoTrader._position_key

    def fake_register(symbol, direction, **kwargs):
        trader._managed_positions[str(kwargs["position_id"])] = SimpleNamespace(symbol=symbol)

    trader._register_position = fake_register

    trader._poll_managed_positions()

    assert set(trader._managed_positions) == {"101", "102", "103"}
