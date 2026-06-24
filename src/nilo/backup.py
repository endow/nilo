from __future__ import annotations

import hashlib
import json
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
