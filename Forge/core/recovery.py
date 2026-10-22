"""Verified local backup and verification for Forge's durable state.

Each SQLite database is copied with SQLite's online backup API so committed
WAL transactions are included. Other files (including model artifacts and
compatibility snapshots) are copied byte-for-byte and recorded in a SHA-256
manifest. The stores are individually consistent; operators should pause Forge
writes during backup when they need a cross-ledger point-in-time recovery set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List
from urllib.parse import quote


SCHEMA_VERSION = 1
_FORGE_ROOT = Path(__file__).resolve().parents[1]
_STATE_DIRECTORIES = (
    "challenger",
    "deployment",
    "drift",
    "experiments",
    "memory",
    "models",
    "reuse",
    "security",
    "training_requests",
)
_MANIFEST = "manifest.json"
_MANIFEST_DIGEST = "manifest.sha256"


class RecoveryError(RuntimeError):
    """Raised when a backup cannot be created or verified safely."""


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _manifest_relative_path(value: Any) -> PurePosixPath:
    """Validate a canonical, cross-platform relative path from a manifest."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise RecoveryError(f"Unsafe path in backup manifest: {value!r}")
    relative = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        relative.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or not relative.parts
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any(part.endswith((".", " ")) for part in relative.parts)
        or any(
            part.split(".", 1)[0].upper() in {
                "CON", "PRN", "AUX", "NUL",
                *(f"COM{index}" for index in range(1, 10)),
                *(f"LPT{index}" for index in range(1, 10)),
            }
            for part in relative.parts
        )
    ):
        raise RecoveryError(f"Unsafe path in backup manifest: {value!r}")
    return relative


def _sqlite_snapshot(source: Path, target: Path) -> str:
    # SQLite's backup API reads a transactionally consistent view and includes
    # committed data currently represented in a WAL file.
    uri = "file:" + quote(str(source.resolve()).replace("\\", "/"), safe="/:") + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=30) as src:
            with sqlite3.connect(target, timeout=30) as dst:
                src.backup(dst)
                integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        raise RecoveryError(f"Unable to snapshot SQLite database {source}: {exc}") from exc
    if integrity != "ok":
        raise RecoveryError(f"SQLite snapshot failed integrity_check: {source}: {integrity}")
    return integrity


def _copy_stable_file(source: Path, target: Path) -> None:
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RecoveryError(f"Source changed while being copied: {source}")


def _iter_state_files(root: Path) -> Iterable[tuple[Path, Path]]:
    for directory in _STATE_DIRECTORIES:
        source_root = root / directory
        if not source_root.exists():
            continue
        if source_root.is_symlink() or not source_root.is_dir():
            raise RecoveryError(f"State path must be a real directory: {source_root}")
        for current, directories, filenames in os.walk(source_root, followlinks=False):
            current_path = Path(current)
            for name in list(directories):
                child = current_path / name
                if child.is_symlink():
                    raise RecoveryError(f"Symbolic links are not allowed in state: {child}")
            for name in filenames:
                source = current_path / name
                if source.is_symlink() or not source.is_file():
                    raise RecoveryError(f"State entries must be regular files: {source}")
                # WAL and shared-memory files are represented by the online
                # snapshot of their corresponding database, never copied raw.
                if source.name.endswith(("-wal", "-shm")):
                    database = source.with_name(source.name[:-4])
                    if database.is_symlink() or not database.is_file():
                        raise RecoveryError(
                            f"SQLite sidecar has no regular database to snapshot: {source}"
                        )
                    continue
                relative = source.relative_to(root)
                yield source, relative


def create_backup(destination: str | Path, *, source_root: str | Path = _FORGE_ROOT) -> Dict[str, Any]:
    """Create a new, atomic backup directory and return its manifest."""
    source_argument = Path(source_root).expanduser()
    if source_argument.is_symlink():
        raise RecoveryError(f"Forge state root cannot be a symbolic link: {source_argument}")
    root = source_argument.resolve(strict=True)
    target_argument = Path(destination).expanduser()
    if target_argument.is_symlink():
        raise RecoveryError(f"Backup destination cannot be a symbolic link: {target_argument}")
    target = target_argument.resolve()
    if not root.is_dir():
        raise RecoveryError(f"Forge state root is not a directory: {root}")
    if target == root or root in target.parents:
        raise RecoveryError("Backup destination cannot be inside the Forge state root")
    if target.exists():
        raise RecoveryError(f"Backup destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    records: List[Dict[str, Any]] = []
    try:
        for source, relative in _iter_state_files(root):
            output = staging.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            is_sqlite = source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
            if is_sqlite:
                integrity = _sqlite_snapshot(source, output)
            else:
                _copy_stable_file(source, output)
                integrity = None
            size, digest = _digest_file(output)
            records.append({
                "path": relative.as_posix(),
                "size_bytes": size,
                "sha256": digest,
                "kind": "sqlite" if is_sqlite else "file",
                "sqlite_integrity": integrity,
            })

        if not records:
            raise RecoveryError("No Forge state files were found; refusing to create an empty backup")
        records.sort(key=lambda item: item["path"])
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "Forge durable state",
            "consistency": "per-store; pause Forge writes for cross-ledger point-in-time consistency",
            "files": records,
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        (staging / _MANIFEST).write_bytes(encoded)
        (staging / _MANIFEST_DIGEST).write_text(hashlib.sha256(encoded).hexdigest() + "\n", encoding="ascii")
        os.replace(staging, target)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(backup: str | Path) -> Dict[str, Any]:
    """Verify the manifest digest, file inventory, hashes, and database health."""
    backup_argument = Path(backup).expanduser()
    if backup_argument.is_symlink():
        raise RecoveryError(f"Backup root cannot be a symbolic link: {backup_argument}")
    root = backup_argument.resolve(strict=True)
    if not root.is_dir():
        raise RecoveryError(f"Backup must be a real directory: {root}")
    manifest_path = root / _MANIFEST
    digest_path = root / _MANIFEST_DIGEST
    try:
        encoded = manifest_path.read_bytes()
        expected_manifest_digest = digest_path.read_text(encoding="ascii").strip()
        manifest = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"Backup manifest is missing or invalid: {exc}") from exc
    if hashlib.sha256(encoded).hexdigest() != expected_manifest_digest:
        raise RecoveryError("Backup manifest digest does not match")
    if len(expected_manifest_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_manifest_digest
    ):
        raise RecoveryError("Backup manifest digest is malformed")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise RecoveryError("Unsupported or invalid backup manifest")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RecoveryError("Backup manifest has no valid file inventory")

    expected_paths = {_MANIFEST, _MANIFEST_DIGEST}
    seen = set()
    for record in files:
        if not isinstance(record, dict):
            raise RecoveryError("Backup inventory contains an invalid record")
        relative_text = record.get("path")
        relative = _manifest_relative_path(relative_text)
        if relative_text in {_MANIFEST, _MANIFEST_DIGEST}:
            raise RecoveryError(f"Reserved path in backup manifest: {relative_text}")
        if relative_text in seen:
            raise RecoveryError(f"Duplicate path in backup manifest: {relative_text}")
        seen.add(relative_text)
        expected_paths.add(relative_text)
        path = root.joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RecoveryError(f"Backup file is missing: {relative_text}") from exc
        if root not in resolved.parents or path.is_symlink() or not resolved.is_file():
            raise RecoveryError(f"Backup path is not a contained regular file: {relative_text}")
        size, digest = _digest_file(resolved)
        if size != record.get("size_bytes") or digest != record.get("sha256"):
            raise RecoveryError(f"Backup file failed size or SHA-256 verification: {relative_text}")
        if record.get("kind") == "sqlite":
            try:
                uri = "file:" + quote(str(resolved).replace("\\", "/"), safe="/:") + "?mode=ro"
                with sqlite3.connect(uri, uri=True) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.Error as exc:
                raise RecoveryError(f"Backup database cannot be opened: {relative_text}: {exc}") from exc
            if integrity != "ok" or record.get("sqlite_integrity") != "ok":
                raise RecoveryError(f"Backup database failed integrity_check: {relative_text}")
        elif record.get("kind") != "file":
            raise RecoveryError(f"Unknown backup file kind: {relative_text}")

    actual_paths = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if any((current_path / name).is_symlink() for name in directories):
            raise RecoveryError("Backup contains a symbolic link")
        for name in filenames:
            item = current_path / name
            if item.is_symlink():
                raise RecoveryError("Backup contains a symbolic link")
            actual_paths.add(item.relative_to(root).as_posix())
    if actual_paths != expected_paths:
        raise RecoveryError("Backup file inventory does not match its manifest")
    return manifest


def restore_backup(backup: str | Path, destination: str | Path) -> Dict[str, Any]:
    """Restore a verified backup into a new state-root directory.

    The destination must not exist. Restoring to a separate directory makes
    this suitable for a recovery drill without overwriting the live state.
    The restored staging tree is verified again before its atomic rename.
    """
    backup_argument = Path(backup).expanduser()
    if backup_argument.is_symlink():
        raise RecoveryError(f"Backup root cannot be a symbolic link: {backup_argument}")
    backup_root = backup_argument.resolve(strict=True)
    manifest = verify_backup(backup_root)
    target_argument = Path(destination).expanduser()
    if target_argument.is_symlink():
        raise RecoveryError(f"Restore destination cannot be a symbolic link: {target_argument}")
    target = target_argument.resolve()
    if target == backup_root or backup_root in target.parents:
        raise RecoveryError("Restore destination cannot be inside the backup")
    if target.exists():
        raise RecoveryError(f"Restore destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        for record in manifest["files"]:
            relative = PurePosixPath(record["path"])
            source = backup_root.joinpath(*relative.parts)
            output = staging.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        shutil.copyfile(backup_root / _MANIFEST, staging / _MANIFEST)
        shutil.copyfile(backup_root / _MANIFEST_DIGEST, staging / _MANIFEST_DIGEST)
        verify_backup(staging)
        os.replace(staging, target)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and verify Forge durable-state backups")
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup", help="create a new verified backup directory")
    backup_parser.add_argument("destination", type=Path)
    backup_parser.add_argument("--source-root", type=Path, default=_FORGE_ROOT)
    verify_parser = commands.add_parser("verify", help="verify a backup manifest and all stored files")
    verify_parser.add_argument("backup", type=Path)
    restore_parser = commands.add_parser("restore", help="restore a verified backup to a new directory")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            manifest = create_backup(args.destination, source_root=args.source_root)
            print(f"Backup created: {Path(args.destination).resolve()} ({len(manifest['files'])} files)")
        elif args.command == "verify":
            manifest = verify_backup(args.backup)
            print(f"Backup verified: {Path(args.backup).resolve()} ({len(manifest['files'])} files)")
        else:
            manifest = restore_backup(args.backup, args.destination)
            print(
                f"Backup restored and verified: {Path(args.destination).resolve()} "
                f"({len(manifest['files'])} files)"
            )
        return 0
    except (OSError, RecoveryError) as exc:
        print(f"Forge recovery error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
