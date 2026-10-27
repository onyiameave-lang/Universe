"""Safe migration helpers for Forge's local runtime stores."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def migrate_legacy_storage(
    legacy_dir: Path, memory_dir: Path, names: tuple[str, ...] | None = None
) -> None:
    """Copy legacy files into memory/ once; never delete the source files."""
    legacy_dir, memory_dir = Path(legacy_dir), Path(memory_dir)
    if not legacy_dir.is_dir() or legacy_dir.resolve() == memory_dir.resolve():
        return
    memory_dir.mkdir(parents=True, exist_ok=True)
    sources = ([legacy_dir / name for name in names] if names is not None
               else list(legacy_dir.rglob("*")))
    for source in sources:
        if not source.is_file() or source.name.endswith(("-wal", "-shm")):
            continue
        relative = Path(source.name) if names is not None else source.relative_to(legacy_dir)
        target = memory_dir / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            try:
                with closing(sqlite3.connect(str(source), timeout=30.0)) as source_db, \
                        closing(sqlite3.connect(str(target), timeout=30.0)) as target_db:
                    source_db.backup(target_db)
                    result = target_db.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        target.unlink(missing_ok=True)
                        raise RuntimeError(f"SQLite integrity check failed while migrating {source}")
            except Exception:
                target.unlink(missing_ok=True)
                raise
        else:
            shutil.copy2(source, target)
