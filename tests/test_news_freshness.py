from datetime import datetime, timedelta, timezone

from Oracle.intelligence.news_impact import NewsImpactClassifier, NewsImpactConfig


def _article(**overrides):
    article = {
        "published_at": "",
        "collected_at": datetime.now(timezone.utc).timestamp(),
        "event_type": "macro",
        "credibility": 1.0,
        "misinformation_risk": 0.0,
        "sentiment": 1.0,
        "title": "unexpected central bank shock",
        "summary": "unexpected policy announcement",
    }
    article.update(overrides)
    return article


def test_recent_published_timestamp_can_drive_news_impact():
    article = _article(published_at=datetime.now(timezone.utc).isoformat())
    assessment = NewsImpactClassifier().assess([article], "EURUSD")
    assert assessment.level == "high"
    assert assessment.pause_new_entries is True


def test_stale_or_unknown_timestamp_cannot_drive_news_impact():
    window = timedelta(minutes=5)
    classifier = NewsImpactClassifier(NewsImpactConfig(recency_window=window))
    stale = _article(collected_at=(datetime.now(timezone.utc) - timedelta(hours=2)).timestamp())
    unknown = _article(collected_at=datetime.now(timezone.utc).timestamp())

    assert classifier.assess([stale], "EURUSD").level == "none"
    assert classifier.assess([unknown], "EURUSD").level == "none"