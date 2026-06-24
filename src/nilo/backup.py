from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .gitmeta import head_commit, working_tree_state
from .store import default_db_path
from .timeutil import now_iso


BACKUP_REASONS = {"manual", "before-upgrade", "before-migration", "before-restore", "daily", "other"}


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    backup_path: Path
    meta_path: Path
    meta: dict[str, Any]


@dataclass(frozen=True)
class BackupRecord:
    backup_path: Path
    meta_path: Path
    meta: dict[str, Any]
    db_exists: bool


@dataclass(frozen=True)
class RestoreResult:
    restored_path: Path
    backup_path: Path
    before_restore: BackupResult | None
    integrity_check: str


def resolve_db_path(path: Path | None = None) -> Path:
    return (path or default_db_path()).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integrity_check(path: Path) -> str:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"database integrity check failed: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    value = str(row[0]) if row else ""
    if value != "ok":
        raise BackupError(f"database integrity check failed: {value}")
    return value


def reserve_backup_path(db_path: Path, *, suffix: str = "") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stem = f"nilo-{timestamp}{suffix}"
    candidate = backup_dir / f"{stem}.db"
    if reserve_file(candidate):
        return candidate
    for index in range(1, 100):
        candidate = backup_dir / f"{stem}-{index:02d}.db"
        if reserve_file(candidate):
            return candidate
    raise BackupError("could not allocate backup file name")


def reserve_file(path: Path) -> bool:
    if path.with_suffix(path.suffix + ".meta.json").exists():
        return False
    try:
        handle = path.open("xb")
    except FileExistsError:
        return False
    handle.close()
    return True


def cleanup_backup_files(backup_path: Path) -> None:
    meta_path = backup_path.with_suffix(backup_path.suffix + ".meta.json")
    for path in (meta_path, backup_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def create_backup(db_path: Path | None = None, *, reason: str = "manual", cwd: Path | None = None) -> BackupResult:
    if reason not in BACKUP_REASONS:
        raise BackupError(f"invalid backup reason: {reason}")
    source = resolve_db_path(db_path)
    if not source.exists():
        raise BackupError(f"database not found: {source}")

    backup_path = reserve_backup_path(source)
    source_conn: sqlite3.Connection | None = None
    backup_conn: sqlite3.Connection | None = None
    backup_failed = False
    try:
        source_conn = sqlite3.connect(source)
        backup_conn = sqlite3.connect(backup_path)
        source_conn.backup(backup_conn)
    except sqlite3.DatabaseError as exc:
        backup_failed = True
        raise BackupError(f"database backup failed: {exc}") from exc
    finally:
        if backup_conn is not None:
            backup_conn.close()
        if source_conn is not None:
            source_conn.close()
        if backup_failed:
            cleanup_backup_files(backup_path)

    try:
        check = integrity_check(backup_path)
        digest = sha256_file(backup_path)
        repo = cwd or Path.cwd()
        tree_state = working_tree_state(repo)
        meta = {
            "schema_version": 1,
            "created_at": now_iso(),
            "source": display_path(source, repo),
            "reason": reason,
            "git_head": head_commit(repo),
            "working_tree_dirty": bool(tree_state["working_tree_dirty"]),
            "nilo_version": __version__,
            "db_size_bytes": backup_path.stat().st_size,
            "sha256": digest,
            "integrity_check": check,
            "backup_path": display_path(backup_path, repo),
            "encrypted": False,
            "encryption": None,
            "exported_to": None,
        }
        meta_path = backup_path.with_suffix(backup_path.suffix + ".meta.json")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        cleanup_backup_files(backup_path)
        raise
    return BackupResult(backup_path=backup_path, meta_path=meta_path, meta=meta)


def display_path(path: Path, cwd: Path) -> str:
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return str(path)


def meta_path_for_backup(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")


def load_backup_records(db_path: Path | None = None) -> list[BackupRecord]:
    source = resolve_db_path(db_path)
    backup_dir = source.parent / "backups"
    if not backup_dir.exists():
        return []
    records: list[BackupRecord] = []
    for meta_path in sorted(backup_dir.glob("*.db.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"could not read backup meta: {meta_path}: {exc}") from exc
        backup_path = meta_path.with_suffix("").with_suffix("")
        records.append(BackupRecord(backup_path=backup_path, meta_path=meta_path, meta=meta, db_exists=backup_path.exists()))
    records.sort(key=lambda record: str(record.meta.get("created_at", "")), reverse=True)
    return records


def verify_backup_file(backup_path: Path) -> None:
    source = backup_path.resolve()
    if not source.exists():
        raise BackupError(f"backup not found: {source}")
    meta_path = meta_path_for_backup(source)
    if not meta_path.exists():
        raise BackupError(f"backup meta not found: {meta_path}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"could not read backup meta: {meta_path}: {exc}") from exc
    expected_sha = str(meta.get("sha256") or "")
    if not expected_sha:
        raise BackupError(f"backup meta has no sha256: {meta_path}")
    integrity_check(source)
    actual_sha = sha256_file(source)
    if actual_sha != expected_sha:
        raise BackupError(f"backup sha256 mismatch: expected {expected_sha}, got {actual_sha}")


def sqlite_sidecar_paths(db_path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
        Path(str(db_path) + "-journal"),
    )


def remove_sqlite_sidecars(db_path: Path) -> None:
    for sidecar in sqlite_sidecar_paths(db_path):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupError(f"could not remove SQLite sidecar file before restore verification: {sidecar}: {exc}") from exc


def restore_backup(backup_path: Path, db_path: Path | None = None, *, cwd: Path | None = None) -> RestoreResult:
    source = backup_path.resolve()
    destination = resolve_db_path(db_path)
    if source == destination:
        raise BackupError("backup path is the same as destination database")
    verify_backup_file(source)

    before_restore: BackupResult | None = None
    if destination.exists():
        before_restore = create_backup(destination, reason="before-restore", cwd=cwd)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.restore.tmp")
    try:
        shutil.copy2(source, temp_path)
        temp_check = integrity_check(temp_path)
        temp_path.replace(destination)
        remove_sqlite_sidecars(destination)
        final_check = integrity_check(destination)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        remove_sqlite_sidecars(temp_path)
        raise
    remove_sqlite_sidecars(temp_path)
    if final_check != temp_check:
        raise BackupError(f"restore verification changed unexpectedly: {temp_check} -> {final_check}")
    return RestoreResult(
        restored_path=destination,
        backup_path=source,
        before_restore=before_restore,
        integrity_check=final_check,
    )
