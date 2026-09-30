import logging

from Oracle.intelligence.signal_fusion import SignalFusion


def test_sentinel_failure_is_visible_and_degrades_signal(caplog):
    class Sentinel:
        def sentiment_for(self, _symbol):
            raise RuntimeError("feed unavailable")

    fusion = SignalFusion(sentinel=Sentinel())

    with caplog.at_level(logging.WARNING):
        result = fusion._news_signal("EURUSD")

    assert result["evidence_state"] == "degraded"
    assert "Sentinel sentiment request failed for EURUSD" in caplog.text


def test_pulse_failure_is_visible_and_degrades_signal(caplog):
    class Pulse:
        def sentiment_for(self, _symbol):
            raise RuntimeError("feed unavailable")

    fusion = SignalFusion(pulse=Pulse())

    with caplog.at_level(logging.WARNING):
        result = fusion._social_signal("EURUSD")

    assert result["evidence_state"] == "degraded"
    assert "Pulse sentiment request failed for EURUSD" in caplog.text
