from Atlas.api import frontend_chat_bridge as atlas


def test_catalog_summary_leaves_missing_benchmark_values_unavailable(monkeypatch):
    catalog = {
        "generated_at": "2026-11-24T12:00:00Z",
        "desks": {
            "EXAMPLE_DESK": {
                "lifecycle_state": "PROPOSED",
                "validation_and_benchmark": {
                    "untouched_lockbox_40pct": {},
                    "v2_vs_frozen_v1_uplift": {},
                    "walk_forward_4_windows": {},
                },
            }
        },
    }
    monkeypatch.setattr(atlas, "_load_json", lambda _path: catalog)

    result = atlas._summarize_desk_catalog()

    desk = result["desks"][0]
    assert desk["lifecycle_state"] == "PROPOSED"
    assert desk["episodes_tested"] is None
    assert desk["lockbox_hit_rate_pct"] is None
    assert desk["hit_rate_uplift_pct"] is None
    assert desk["p_value"] is None
    assert desk["walk_forward_windows_passed"] is None


def test_atlas_does_not_answer_account_questions_with_fabricated_values():
    answer = atlas._check_internal_universe_answer("What is our account balance and open trades?")

    assert answer is not None
    assert "cannot verify" in answer
    assert "$104,621" not in answer
    assert "100%" not in answer


def test_symbol_context_does_not_invent_market_state_or_strategy_metrics(monkeypatch):
    monkeypatch.setattr(atlas, "_load_json", lambda _path: {})

    result = atlas._extract_symbol_context("EURUSD")

    assert result["EURUSD"]["dominant_regime"] is None
    assert result["EURUSD"]["current_session"] is None
    assert result["EURUSD"]["timeframes_loaded"] == []
    assert result["EURUSD"]["strategy_records"] == {}


def test_missing_atlas_confidence_stays_unavailable_and_is_not_saved_as_a_score(tmp_path, monkeypatch):
    store = tmp_path / "Chronicle" / "memory" / "store"
    store.mkdir(parents=True)
    (store / "memories.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(atlas, "_ECO_ROOT", tmp_path)

    assert atlas._finite_confidence(None) is None
    assert atlas._finite_confidence(float("nan")) is None
    assert atlas._finite_confidence(1.1) is None
    assert atlas._finite_confidence(0.72) == 0.72

    atlas._save_to_chronicle_memory(
        query="A sourced research answer", subject_terms=["sourced", "research"],
        direct_answer="Answer backed by recorded sources.", sources=[], domain="general",
    )

    import json
    records = json.loads((store / "memories.json").read_text(encoding="utf-8"))
    assert len(records) == 1
    assert "confidence" not in records[0]
