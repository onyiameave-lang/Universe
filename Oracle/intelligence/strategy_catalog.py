"""Read-only, provenance-preserving snapshot of Oracle's saved strategy genomes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

SCHEMA = "oracle.strategy_catalog.snapshot.v1"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_strategy_catalog_snapshot(memory_dir: Path | None = None) -> Dict[str, Any]:
    """Export saved Oracle genomes without inferring strategy names or families."""
    memory_dir = memory_dir or Path(__file__).resolve().parents[1] / "memory"
    source_files = ("champion_library.json", "evolved_strategies.json", "champion_history.json")
    records: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []
    files_read: List[str] = []
    for filename in source_files:
        path = memory_dir / filename
        if not path.exists():
            errors.append({"source": filename, "error": "missing"})
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"source": filename, "error": type(exc).__name__})
            continue
        if not isinstance(document, dict):
            errors.append({"source": filename, "error": "invalid_root_type"})
            continue
        files_read.append(filename)
        for memory_key, stored in sorted(document.items()):
            if not isinstance(stored, dict):
                errors.append({"source": filename, "record": str(memory_key), "error": "invalid_record_type"})
                continue
            genome = stored.get("genome")
            if not isinstance(genome, dict) or not isinstance(genome.get("modules"), dict):
                continue
            content_fp = _digest(genome)
            genome_id = str(genome.get("genome_id") or stored.get("genome_id") or "").strip()
            identity = f"{genome_id or 'unidentified'}:{content_fp}"
            record = records.get(identity)
            if record is None:
                family = genome.get("family", stored.get("family"))
                name = genome.get("strategy_name", stored.get("strategy_name"))
                record = {
                    "record_id": _digest({"identity": identity}),
                    "strategy_id": genome_id or f"oracle-genome-{content_fp[:16]}",
                    "name": name if isinstance(name, str) and name.strip() else None,
                    "family": family if isinstance(family, str) and family.strip() else None,
                    "classification_status": "explicit" if family or name else "unclassified",
                    "symbol": stored.get("symbol"), "regime": stored.get("regime"),
                    "generation": genome.get("generation", stored.get("generation")),
                    "parents": genome.get("parents", stored.get("parents", [])),
                    "version": stored.get("version", stored.get("parent_version")),
                    "content_fingerprint": content_fp,
                    "provenance": "persisted Oracle V1 genome; source identity retained",
                    "genome": genome, "source_records": [],
                }
                records[identity] = record
            record["source_records"].append({
                "file": filename, "memory_key": str(memory_key),
                "certified_at": stored.get("certified_at"),
                "experiment_id": stored.get("experiment_id"),
                "parent_champion_id": stored.get("parent_champion_id"),
            })
    try:
        from Oracle.intelligence.strategy_library import ALL_FAMILIES
        templates = [{
            "family": str(family.name), "description": str(getattr(family, "description", "")),
            "preferred_regimes": list(getattr(family, "preferred_regimes", []) or []),
            "source": "Oracle strategy-library template; not a certified strategy instance",
        } for family in ALL_FAMILIES if isinstance(getattr(family, "name", None), str)]
    except ImportError as exc:
        templates = []
        errors.append({"source": "strategy_library", "error": type(exc).__name__})
    body = {"schema": SCHEMA, "source_files": files_read,
            "records": sorted(records.values(), key=lambda item: item["record_id"]),
            "family_templates": templates, "source_errors": errors}
    body["catalog_fingerprint"] = _digest(body)
    body["record_count"] = len(body["records"])
    body["family_template_count"] = len(templates)
    body["source_error_count"] = len(errors)
    return body
