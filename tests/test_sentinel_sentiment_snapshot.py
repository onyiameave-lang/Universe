import threading
import time
from datetime import datetime, timezone

from Sentinel.core.intelligence_engine import IntelligenceEngine
from Sentinel.intelligence.analysis import extract_symbols


def _article(symbol):
    return {
        "symbols": [symbol],
        "published_at": datetime.now(timezone.utc).isoformat(),
        "sentiment": 0.6,
        "credibility": 0.8,
        "source": "rss",
        "title": f"Fresh {symbol} headline",
    }


def test_symbol_match_rejects_unrelated_or_undated_articles():
    articles = [
        _article("EURUSD"),
        {**_article("GBPUSD"), "published_at": ""},
    ]

    matched = IntelligenceEngine._matching_fresh_articles(articles, "EURUSD")

    assert len(matched) == 1
    assert matched[0]["symbols"] == ["EURUSD"]


def test_index_aliases_match_fresh_news_for_oracle_symbols():
    now = datetime.now(timezone.utc).isoformat()
    articles = [
        {**_article("JPN225"), "title": "Nikkei 225 rises", "symbols": extract_symbols("Nikkei 225 rises", ""),
         "published_at": now},
        {**_article("AUS200"), "title": "ASX 200 gains", "symbols": extract_symbols("ASX 200 gains", ""),
         "published_at": now},
    ]

    assert len(IntelligenceEngine._matching_fresh_articles(articles, "JPN225")) == 1
    assert len(IntelligenceEngine._matching_fresh_articles(articles, "AUS200")) == 1


def test_usdcad_aliases_match_fresh_news_for_oracle_symbol():
    title = "Canadian dollar strengthens against US dollar"
    article = {
        **_article("USDCAD"),
        "title": title,
        "symbols": extract_symbols(title, ""),
    }

    matched = IntelligenceEngine._matching_fresh_articles([article], "USDCAD")

    assert len(matched) == 1


def test_sentiment_for_reuses_one_broad_snapshot_across_symbols():
    engine = IntelligenceEngine.__new__(IntelligenceEngine)
    engine._sentiment_snapshot = []
    engine._sentiment_snapshot_at = 0.0
    engine._sentiment_snapshot_status = {}
    engine._sentiment_refresh_lock = threading.Lock()
    calls = []

    def fake_gather(**kwargs):
        calls.append(kwargs)
        return {
            "articles": [_article("EURUSD"), _article("GBPUSD")],
            "source_status": {"rss": {"status": "ok"}},
        }

    engine.gather = fake_gather
    assert engine.sentiment_for("EURUSD")["evidence_state"] == "fresh"
    assert engine.sentiment_for("GBPUSD")["evidence_state"] == "fresh"
    assert len(calls) == 1
    assert calls[0]["topics"] is None