from Forge.core.mtf_institutional_engine import MultiTimeframeInstitutionalEngine


def test_pulse_sentinel_context_skips_zero_price_bar_without_dividing_by_zero(tmp_path):
    engine = MultiTimeframeInstitutionalEngine(workspace_root=tmp_path)
    bars = [
        {
            "open": 0.0,
            "close": 0.0,
            "atr14": 0.0,
            "unix_sec": 1_792_000_000,
            "volatility_percentile": 0.9,
            "efficiency_ratio": 0.5,
            "rsi14": 50.0,
            "ema9": 0.0,
            "ema21": 0.0,
        },
        {
            "open": 1.0,
            "close": 1.001,
            "atr14": 0.0,
            "unix_sec": 1_792_000_060,
            "volatility_percentile": 0.5,
            "efficiency_ratio": 0.4,
            "rsi14": 60.0,
            "ema9": 1.001,
            "ema21": 1.0,
        },
    ]

    result = engine.fetch_pulse_and_sentinel_context("EURUSD", bars)

    assert len(result["sentinel_bar_scores"]) == len(bars)
    assert len(result["pulse_bar_scores"]) == len(bars)
    assert result["sentinel_bar_scores"][0] == 0.0
    assert result["pulse_bar_scores"][0] == 0.0
    assert all(abs(value) <= 1.0 for value in result["sentinel_bar_scores"])
    assert all(abs(value) <= 1.0 for value in result["pulse_bar_scores"])


def test_bar_annotation_drops_zero_price_candle_and_preserves_crypto_atr_precision(tmp_path):
    engine = MultiTimeframeInstitutionalEngine(workspace_root=tmp_path)
    bars = [
        {
            "open": 0.000012,
            "high": 0.000012,
            "low": 0.000012,
            "close": 0.000012,
            "volume": 10,
            "unix_sec": 1_792_000_000,
            "ts": "2026-10-15T00:00:00Z",
        },
        {
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,
            "volume": 0,
            "unix_sec": 1_792_000_060,
            "ts": "2026-10-15T00:01:00Z",
        },
    ]

    annotated = engine.annotate_bar_series(bars)

    assert len(annotated) == 1
    assert annotated[0]["atr14"] > 0


def test_safe_training_atr_rejects_zero_price_and_floors_valid_price():
    assert MultiTimeframeInstitutionalEngine._safe_atr_for_price(0.0, 0.0) is None
    assert MultiTimeframeInstitutionalEngine._safe_atr_for_price(0.00001, 0.0) > 0


def test_training_split_bounds_never_exceed_available_trigger_bars():
    assert MultiTimeframeInstitutionalEngine._training_split_bounds(25) == (25, 25)
    assert MultiTimeframeInstitutionalEngine._training_split_bounds(100) == (60, 40)
