"""Preview/apply bounded-history compaction for Chronicle's SQLite state.

Usage (with the V2 API and V1 trader stopped):
    python Chronicle/tools/compact_state_history.py                 # dry-run
    python Chronicle/tools/compact_state_history.py --apply --api-stopped

Apply always creates and verifies a compressed SQLite backup first. Raw,
immutable Oracle event keys and current aggregate values are not deleted.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Chronicle.core.sqlite_state_store import history_limit_for_key  # noqa: E402

STORE_DIR = ROOT / "Chronicle" / "memory" / "store"
DATABASE = STORE_DIR / "state_store.sqlite3"
BACKUP_DIR = STORE_DIR / "backups"


def _encode(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                                     timeout=30.0)
    else:
        connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _compacted_record(key: str, record: Dict[str, Any]) -> Dict[str, Any]:
    limit = history_limit_for_key(key)
    history = record.get("history", [])
    record["history"] = history[-limit:] if limit else []
    return record


def _walk_records(connection: sqlite3.Connection) -> Iterable[Tuple[str, str]]:
    cursor = connection.execute("SELECT key, record_json FROM state_records ORDER BY key")
    while True:
        batch = cursor.fetchmany(64)
        if not batch:
            return
        yield from batch


def preview(connection: sqlite3.Connection) -> Dict[str, int]:
    result = {"records": 0, "records_to_compact": 0,
              "history_entries_removed": 0, "estimated_bytes_reclaimed": 0}
    for key, raw in _walk_records(connection):
        result["records"] += 1
        record = json.loads(raw)
        old_history = record.get("history", [])
        if not isinstance(old_history, list):
            raise ValueError(f"invalid history for Chronicle key {key!r}")
        new_record = _compacted_record(key, dict(record))
        removed = len(old_history) - len(new_record["history"])
        if removed:
            result["records_to_compact"] += 1
            result["history_entries_removed"] += removed
            result["estimated_bytes_reclaimed"] += max(
                0, len(raw.encode("utf-8")) - len(_encode(new_record).encode("utf-8"))
            )
    return result


def _verified_compressed_backup() -> Tuple[Path, str]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    raw_backup = BACKUP_DIR / f"state_store_precompact_{stamp}.sqlite3"
    archive = BACKUP_DIR / f"state_store_precompact_{stamp}.sqlite3.gz"
    source = _connect(DATABASE, readonly=True)
    destination = sqlite3.connect(str(raw_backup))
    try:
        source.backup(destination, pages=2048, sleep=0.05)
        check = destination.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"backup integrity check failed: {check}")
    finally:
        destination.close()
        source.close()

    raw_hash = hashlib.sha256()
    with raw_backup.open("rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
        while chunk := src.read(1024 * 1024):
            raw_hash.update(chunk)
            dst.write(chunk)
    expected = raw_hash.hexdigest()
    restored_hash = hashlib.sha256()
    with gzip.open(archive, "rb") as src:
        while chunk := src.read(1024 * 1024):
            restored_hash.update(chunk)
    if restored_hash.hexdigest() != expected:
        archive.unlink(missing_ok=True)
        raise RuntimeError("compressed backup verification failed")
    raw_backup.unlink()
    return archive, expected


def _archive_legacy_json_backup() -> Path | None:
    """Compress the one-time pre-SQLite backup without losing its restore data."""
    legacy = STORE_DIR / "state_store.json.legacy"
    if not legacy.is_file():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = STORE_DIR / f"state_store.json.legacy.{stamp}.gz"
    raw_hash = hashlib.sha256()
    with legacy.open("rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
        while chunk := src.read(1024 * 1024):
            raw_hash.update(chunk)
            dst.write(chunk)
    expected = raw_hash.hexdigest()
    restored_hash = hashlib.sha256()
    with gzip.open(archive, "rb") as src:
        while chunk := src.read(1024 * 1024):
            restored_hash.update(chunk)
    if restored_hash.hexdigest() != expected:
        archive.unlink(missing_ok=True)
        raise RuntimeError("legacy JSON archive verification failed; original was retained")
    legacy.unlink()
    return archive


def apply_compaction(api_stopped: bool) -> None:
    if not api_stopped:
        raise ValueError("--apply requires --api-stopped after stopping the V2 API and V1 trader")
    if not DATABASE.is_file():
        raise FileNotFoundError(f"Chronicle state database not found: {DATABASE}")

    database_bytes_before = DATABASE.stat().st_size
    # Confirm exactly what will be removed before making a verified rollback copy.
    with _connect(DATABASE, readonly=True) as connection:
        before = preview(connection)
    print("Compaction plan:", json.dumps(before, sort_keys=True))
    backup, backup_hash = _verified_compressed_backup()
    print(f"Verified rollback backup: {backup}")
    print(f"Backup SHA-256: {backup_hash}")

    connection = _connect(DATABASE)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = 0
        for key, raw in _walk_records(connection):
            record = json.loads(raw)
            compacted = _compacted_record(key, dict(record))
            if compacted.get("history") != record.get("history", []):
                connection.execute(
                    "UPDATE state_records SET record_json = ? WHERE key = ?",
                    (_encode(compacted), key),
                )
                changed += 1
        connection.commit()
        print(f"Compacted {changed} current state records; current values and revisions were preserved.")
        # Reclaim free pages after compaction; this is intentionally offline-only.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"post-compaction integrity check failed: {check}")
        remaining = preview(connection)
        if remaining["history_entries_removed"]:
            raise RuntimeError("post-compaction history verification failed")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    legacy_archive = _archive_legacy_json_backup()
    database_bytes_after = DATABASE.stat().st_size
    print("Compaction complete.")
    print(f"Database bytes: {database_bytes_before} -> {database_bytes_after}")
    if legacy_archive:
        print(f"Verified compressed legacy backup: {legacy_archive}")


def restore_backup(backup: Path, api_stopped: bool) -> None:
    if not api_stopped:
        raise ValueError("--restore requires --api-stopped after stopping the V2 API and V1 trader")
    backup = backup.resolve()
    if not backup.is_file() or backup.suffix.lower() != ".gz":
        raise ValueError("restore requires a verified .sqlite3.gz backup from this tool")
    temporary = DATABASE.with_name(DATABASE.name + ".restore.tmp")
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(backup, "rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        with _connect(temporary, readonly=True) as check_db:
            check = check_db.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError(f"backup integrity check failed: {check}")
        for suffix in ("-wal", "-shm"):
            Path(str(DATABASE) + suffix).unlink(missing_ok=True)
        os.replace(temporary, DATABASE)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Restored and verified Chronicle database from {backup}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true", help="compact the database after verified backup")
    actions.add_argument("--restore", type=Path, help="restore a .sqlite3.gz backup")
    parser.add_argument("--api-stopped", action="store_true",
                        help="confirm V2 API and V1 trader are stopped before writes")
    args = parser.parse_args()
    try:
        if args.restore:
            restore_backup(args.restore, args.api_stopped)
        elif args.apply:
            apply_compaction(args.api_stopped)
        else:
            if not DATABASE.is_file():
                raise FileNotFoundError(f"Chronicle state database not found: {DATABASE}")
            with _connect(DATABASE, readonly=True) as connection:
                plan = preview(connection)
            print("DRY RUN (no data changed):", json.dumps(plan, sort_keys=True))
            print("To apply safely, stop V2 API and V1 trader, then rerun with --apply --api-stopped.")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
