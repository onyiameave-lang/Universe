from Oracle.intelligence.signal_fusion import SignalFusion


class _DegradedEvidence:
    def sentiment_for(self, symbol):
        return {"sentiment": 0.0, "confidence": 0.0,
                "evidence_state": "degraded", "post_count": 0}


def test_fusion_marks_technical_only_evidence_as_degraded():
    fusion = SignalFusion(sentinel=_DegradedEvidence(), pulse=_DegradedEvidence())

    result = fusion.fuse("EURUSD", {"rsi_14": 50, "regime": {"regime": "ranging"}})

    assert result["degraded_evidence"] is True
    assert result["evidence_state"] == "degraded"