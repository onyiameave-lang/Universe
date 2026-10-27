"""Restore/export the Forge-owned Oracle Chronicle namespace for Actions runs.

The workflow runs on an ephemeral runner. This deliberately snapshots only
``forge.oracle.*`` Chronicle state, leaving other agents' memory and state out
of the Oracle research branch artifact.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE = ROOT / "Chronicle" / "memory" / "store"
DEFAULT_SNAPSHOT = ROOT / "Chronicle" / "memory" / "research" / "oracle_state" / "forge_oracle_state.json"
LEGACY_SNAPSHOT = ROOT / "Chronicle" / "research" / "oracle_state" / "forge_oracle_state.json"
NAMESPACE = "forge.oracle."
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from Chronicle.core.state_store import StateStore
except ImportError:  # direct package execution from inside Chronicle
    from core.state_store import StateStore  # type: ignore


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("restore", "export"))
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    args = parser.parse_args(argv)
    store = StateStore(str(args.storage_dir))

    if args.action == "restore":
        if not args.snapshot.exists() and args.snapshot == DEFAULT_SNAPSHOT and LEGACY_SNAPSHOT.is_file():
            args.snapshot = LEGACY_SNAPSHOT
        if not args.snapshot.exists():
            print("No Forge Oracle Chronicle snapshot exists yet; starting a new namespace.")
            return 0
        try:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read Forge Oracle Chronicle snapshot: {type(exc).__name__}",
                  file=sys.stderr)
            return 1
        result = store.restore_snapshot(snapshot, NAMESPACE)
        if result.get("status") != "complete":
            print(result.get("message", "Forge Oracle Chronicle restore failed"), file=sys.stderr)
            return 1
        print(f"Restored Forge Oracle Chronicle state: {result['restored']} records; "
              f"kept {result['skipped_newer']} newer local records.")
        return 0

    snapshot = store.export_snapshot(NAMESPACE)
    _write_json_atomic(args.snapshot, snapshot)
    print(f"Exported {snapshot['record_count']} Forge Oracle Chronicle state records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
