from Pulse.core.intelligence_engine import _resolve_symbol
from Pulse.core.collectors import _topics_to_subs
from Pulse.intelligence.sentiment import extract_symbols


def test_index_names_resolve_to_oracle_symbols():
    assert "JPN225" in extract_symbols("Nikkei 225 rallied")
    assert _resolve_symbol("NIKKEI") == "JPN225"
    assert "AUS200" in extract_symbols("ASX 200 rallied")
    assert _resolve_symbol("ASX200") == "AUS200"


def test_usdcad_news_terms_match_and_use_finance_sources():
    assert "USDCAD" in extract_symbols("Canadian dollar strengthens against US dollar")
    assert _topics_to_subs(["USDCAD"])[1] == "Finance"
