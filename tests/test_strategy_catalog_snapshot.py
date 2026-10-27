import json
import tempfile
from pathlib import Path

from Oracle.intelligence.strategy_catalog import build_strategy_catalog_snapshot


def test_catalog_snapshot_exports_saved_genome_with_provenance():
    genome = {"genome_id": "v1-test", "generation": 3, "parents": [],
              "modules": {"trend": {"logic_type": "test", "params": {}}}}
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        tmp_path = Path(temp_dir)
        (tmp_path / "evolved_strategies.json").write_text(json.dumps({
            "EURUSD::trending_up": {
                "symbol": "EURUSD", "regime": "trending_up", "genome": genome,
                "out_of_sample": {"total_return": 0.1},
            },
        }), encoding="utf-8")

        snapshot = build_strategy_catalog_snapshot(tmp_path)

    assert snapshot["schema"] == "oracle.strategy_catalog.snapshot.v1"
    assert snapshot["record_count"] == 1
    record = snapshot["records"][0]
    assert record["symbol"] == "EURUSD"
    assert record["regime"] == "trending_up"
    assert record["genome"] == genome
    assert record["source_records"][0]["memory_key"] == "EURUSD::trending_up"
    assert len(snapshot["catalog_fingerprint"]) == 64
