from types import SimpleNamespace

from Oracle.execution.mt5_demo_trader import DemoTrader, SymbolMapper, _LIVE_PRESET
from Oracle.execution.mt5_broker import DEFAULT_SYMBOL_MAP


def _trader_for_server(server):
    trader = DemoTrader.__new__(DemoTrader)
    trader.symbols = ["USOIL"]
    trader.broker = SimpleNamespace(status=SimpleNamespace(server=server))
    trader._sym_mapper = SymbolMapper(demo_overrides={})
    return trader


def test_mt5_ignores_shared_ctrader_symbol_map(monkeypatch):
    monkeypatch.setenv("BROKER_SYMBOL_MAP", "USOIL:WTOIL-PERP")
    monkeypatch.delenv("MT5_BROKER_SYMBOL_MAP", raising=False)

    mapper = SymbolMapper(demo_overrides={})
    mapper.build(["USOIL", "WTOIL-PERP"], ["USOIL"])

    assert mapper.translate("USOIL") == "USOIL"


def test_mt5_broker_fallback_does_not_assume_xtiusd():
    assert DEFAULT_SYMBOL_MAP["USOIL"] == "USOIL"


def test_mt5_live_preset_replaces_crypto_with_forex():
    assert len(_LIVE_PRESET) == 8
    assert not {"BTCUSD", "ETHUSD"}.intersection(_LIVE_PRESET)
    assert {"NZDUSD", "EURGBP"}.issubset(_LIVE_PRESET)


def test_non_metaquotes_server_does_not_use_metaquotes_oil_alias():
    trader = _trader_for_server("FivePercentOnline-Experience")

    trader._build_symbol_map(broker_symbols=[])

    assert trader._sym_mapper.translate("USOIL") == "USOIL"


def test_metaquotes_demo_keeps_its_built_in_oil_alias():
    trader = _trader_for_server("MetaQuotes-Demo")

    trader._build_symbol_map(broker_symbols=[])

    assert trader._sym_mapper.translate("USOIL") == "XTIUSD"
