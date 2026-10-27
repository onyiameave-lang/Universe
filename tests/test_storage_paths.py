import sqlite3
from pathlib import Path

from Forge.core.storage_paths import migrate_legacy_storage


def test_legacy_files_are_copied_once_without_removing_source(tmp_path: Path):
    legacy = tmp_path / "legacy"
    memory = tmp_path / "memory" / "owner"
    legacy.mkdir()
    (legacy / "state.json").write_text('{"revision": 1}', encoding="utf-8")

    migrate_legacy_storage(legacy, memory)

    assert (memory / "state.json").read_text(encoding="utf-8") == '{"revision": 1}'
    assert (legacy / "state.json").is_file()


def test_legacy_sqlite_database_is_backed_up_and_verified(tmp_path: Path):
    legacy = tmp_path / "legacy"
    memory = tmp_path / "memory"
    legacy.mkdir()
    source = legacy / "state.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('preserved')")

    migrate_legacy_storage(legacy, memory)

    with sqlite3.connect(memory / "state.sqlite3") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM records").fetchone()[0] == "preserved"
    assert source.is_file()


def test_migration_does_not_overwrite_existing_memory_value(tmp_path: Path):
    legacy = tmp_path / "legacy"
    memory = tmp_path / "memory"
    legacy.mkdir()
    memory.mkdir()
    (legacy / "state.json").write_text("legacy", encoding="utf-8")
    (memory / "state.json").write_text("current", encoding="utf-8")

    migrate_legacy_storage(legacy, memory)

    assert (memory / "state.json").read_text(encoding="utf-8") == "current"
