from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from subprocess import PIPE, run
from typing import Any, Sequence

from . import __version__
from .gitmeta import head_commit, working_tree_state
from .store import default_db_path
from .timeutil import now_iso


BACKUP_REASONS = {"manual", "before-upgrade", "before-migration", "before-restore", "daily", "other"}
DEFAULT_PRUNE_PROTECTED_REASONS = {"manual", "before-upgrade", "before-migration", "before-restore"}
POST_COMMAND_OUTPUT_LIMIT = 8192


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
class PruneResult:
    kept: list[BackupRecord]
    pruned: list[BackupRecord]
    protected: list[BackupRecord]


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


def backup_config_path(db_path: Path | None = None) -> Path:
    return resolve_db_path(db_path).parent / "config.toml"


def load_backup_config(db_path: Path | None = None) -> dict[str, Any]:
    path = backup_config_path(db_path)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BackupError(f"could not parse backup config: {path}: {exc}") from exc
    backup = data.get("backup", {})
    if not isinstance(backup, dict):
        raise BackupError(f"backup config section must be a table: {path}")
    return backup


def configured_age_recipient(db_path: Path | None = None) -> str:
    value = load_backup_config(db_path).get("age_recipient", "")
    return value if isinstance(value, str) else ""


def configured_post_command(db_path: Path | None = None) -> list[str] | None:
    value = load_backup_config(db_path).get("post_command")
    if value is None:
        return None
    return validate_post_command(value)


def validate_post_command(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise BackupError("backup.post_command must be a non-empty argv array")
    argv: list[str] = []
    for item in value:
        if not isinstance(item, str) or item == "":
            raise BackupError("backup.post_command entries must be non-empty strings")
        argv.append(item)
    return argv


def save_age_recipient(db_path: Path | None, recipient: str) -> Path:
    if not recipient:
        raise BackupError("age recipient is required to save backup config")
    path = backup_config_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(recipient, ensure_ascii=False)
    if not path.exists():
        write_text_atomic(path, f"[backup]\nage_recipient = {encoded}\n")
        return path

    lines = path.read_text(encoding="utf-8").splitlines()
    backup_header_index = next((index for index, line in enumerate(lines) if line.strip() == "[backup]"), None)
    if backup_header_index is None:
        suffix = ["", "[backup]", f"age_recipient = {encoded}"] if lines else ["[backup]", f"age_recipient = {encoded}"]
        write_text_atomic(path, "\n".join(lines + suffix) + "\n")
        return path

    insert_index = len(lines)
    for index in range(backup_header_index + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            insert_index = index
            break
        if stripped.split("=", 1)[0].strip() == "age_recipient":
            lines[index] = f"age_recipient = {encoded}"
            write_text_atomic(path, "\n".join(lines) + "\n")
            return path
    lines.insert(insert_index, f"age_recipient = {encoded}")
    write_text_atomic(path, "\n".join(lines) + "\n")
    return path


def write_text_atomic(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def create_backup(
    db_path: Path | None = None,
    *,
    reason: str = "manual",
    cwd: Path | None = None,
    export_dir: Path | None = None,
    encrypt: bool = False,
    recipient: str | None = None,
    age_command: str = "age",
    post_command: Sequence[str] | None = None,
) -> BackupResult:
    if reason not in BACKUP_REASONS:
        raise BackupError(f"invalid backup reason: {reason}")
    age_executable: str | None = None
    if encrypt:
        age_executable = require_age_ready(recipient, age_command)
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
    if encrypt:
        try:
            encrypted = encrypt_backup_file(backup_path, meta_path, meta, recipient or "", repo, age_command, age_executable)
        except Exception:
            cleanup_backup_files(backup_path)
            raise
        backup_path = encrypted.backup_path
        meta_path = encrypted.meta_path
        meta = encrypted.meta
    if export_dir is not None:
        meta = export_backup_files(backup_path, meta_path, meta, export_dir, repo)
    configured_command = list(post_command) if post_command is not None else configured_post_command(db_path)
    if configured_command:
        meta = run_post_command(configured_command, backup_path, meta_path, meta, repo)
    return BackupResult(backup_path=backup_path, meta_path=meta_path, meta=meta)


def require_age_ready(recipient: str | None, age_command: str = "age") -> str:
    if not recipient:
        raise BackupError("age recipient is required for encrypted backup")
    executable = shutil.which(age_command)
    if executable is None:
        raise BackupError(f"age command not found: {age_command}")
    return executable


def require_age_command(age_command: str = "age") -> str:
    executable = shutil.which(age_command)
    if executable is None:
        raise BackupError(f"age command not found: {age_command}")
    return executable


def encrypt_file_with_age(
    plaintext_path: Path,
    encrypted_path: Path,
    recipient: str,
    age_command: str = "age",
    age_executable: str | None = None,
) -> None:
    executable = age_executable or require_age_ready(recipient, age_command)
    with encrypted_path.open("wb") as output:
        result = run(
            [executable, "-r", recipient, str(plaintext_path)],
            stdout=output,
            stderr=PIPE,
            check=False,
        )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else "age encryption failed"
        raise BackupError(message)


def decrypt_file_with_age(encrypted_path: Path, plaintext_path: Path, age_command: str = "age") -> None:
    executable = require_age_command(age_command)
    with plaintext_path.open("wb") as output:
        result = run(
            [executable, "-d", str(encrypted_path)],
            stdout=output,
            stderr=PIPE,
            check=False,
        )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else "age decryption failed"
        raise BackupError(message)


def encrypt_backup_file(
    backup_path: Path,
    meta_path: Path,
    meta: dict[str, Any],
    recipient: str,
    cwd: Path,
    age_command: str = "age",
    age_executable: str | None = None,
) -> BackupResult:
    encrypted_path = Path(str(backup_path) + ".age")
    encrypted_meta_path = meta_path_for_backup(encrypted_path)
    if encrypted_path.exists() or encrypted_meta_path.exists():
        raise BackupError(f"encrypted backup already exists: {encrypted_path}")
    try:
        encrypt_file_with_age(backup_path, encrypted_path, recipient, age_command, age_executable)
        ciphertext_sha = sha256_file(encrypted_path)
        plaintext_sha = str(meta["sha256"])
        encrypted_meta = dict(meta)
        encrypted_meta.update(
            {
                "db_size_bytes": encrypted_path.stat().st_size,
                "sha256": ciphertext_sha,
                "backup_path": display_path(encrypted_path, cwd),
                "encrypted": True,
                "encryption": {
                    "tool": "age",
                    "recipient": recipient,
                    "plaintext_sha256": plaintext_sha,
                    "ciphertext_sha256": ciphertext_sha,
                    "plaintext_size_bytes": meta["db_size_bytes"],
                    "ciphertext_size_bytes": encrypted_path.stat().st_size,
                },
            }
        )
        encrypted_meta_path.write_text(json.dumps(encrypted_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        cleanup_backup_files(encrypted_path)
        raise
    cleanup_backup_files(backup_path)
    return BackupResult(backup_path=encrypted_path, meta_path=encrypted_meta_path, meta=encrypted_meta)


def reserve_export_path(export_dir: Path, source_name: str) -> Path:
    destination = export_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source = Path(source_name)
    stem = source.stem
    suffix = source.suffix
    candidate = destination / source.name
    if reserve_file(candidate):
        return candidate
    for index in range(1, 100):
        candidate = destination / f"{stem}-{index:02d}{suffix}"
        if reserve_file(candidate):
            return candidate
    raise BackupError("could not allocate export file name")


def export_backup_files(backup_path: Path, meta_path: Path, meta: dict[str, Any], export_dir: Path, cwd: Path) -> dict[str, Any]:
    export_path = reserve_export_path(export_dir, backup_path.name)
    export_meta_path = meta_path_for_backup(export_path)
    temp_path = export_path.with_name(f".{export_path.name}.{os.getpid()}.tmp")
    temp_meta_path = export_meta_path.with_name(f".{export_meta_path.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(backup_path, temp_path)
        temp_sha = sha256_file(temp_path)
        expected_sha = str(meta.get("sha256") or "")
        if temp_sha != expected_sha:
            raise BackupError(f"export sha256 mismatch: expected {expected_sha}, got {temp_sha}")
        temp_path.replace(export_path)
        export_sha = sha256_file(export_path)
        if export_sha != expected_sha:
            raise BackupError(f"export sha256 mismatch after copy: expected {expected_sha}, got {export_sha}")

        local_meta = dict(meta)
        export_meta = dict(meta)
        exported_to = {
            "backup_path": display_path(export_path, cwd),
            "meta_path": display_path(export_meta_path, cwd),
            "sha256": export_sha,
            "exported_at": now_iso(),
        }
        local_meta["exported_to"] = exported_to
        export_meta["backup_path"] = display_path(export_path, cwd)
        export_meta["exported_to"] = exported_to
        meta_path.write_text(json.dumps(local_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_meta_path.write_text(json.dumps(export_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_meta_path.replace(export_meta_path)
    except Exception:
        for path in (temp_meta_path, temp_path, export_meta_path, export_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    return local_meta


def run_post_command(argv_template: Sequence[str], backup_path: Path, meta_path: Path, meta: dict[str, Any], cwd: Path) -> dict[str, Any]:
    argv = render_post_command(argv_template, backup_path, meta_path, meta, cwd)
    executed_at = now_iso()
    try:
        result = run(argv, stdout=PIPE, stderr=PIPE, text=True, check=False)
    except OSError as exc:
        updated_meta = with_post_command_meta(
            meta,
            argv,
            executed_at,
            returncode=None,
            stdout="",
            stderr=str(exc),
        )
        meta_path.write_text(json.dumps(updated_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_exported_post_command_meta(updated_meta, cwd)
        raise BackupError(f"post_command failed to start: {exc}") from exc

    updated_meta = with_post_command_meta(
        meta,
        argv,
        executed_at,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )
    meta_path.write_text(json.dumps(updated_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_exported_post_command_meta(updated_meta, cwd)
    if result.returncode != 0:
        raise BackupError(f"post_command failed with exit code {result.returncode}")
    return updated_meta


def render_post_command(argv_template: Sequence[str], backup_path: Path, meta_path: Path, meta: dict[str, Any], cwd: Path) -> list[str]:
    argv = validate_post_command(list(argv_template))
    exported_to = meta.get("exported_to")
    exported_backup_path = ""
    exported_meta_path = ""
    if isinstance(exported_to, dict):
        exported_backup_path = str(exported_to.get("backup_path") or "")
        exported_meta_path = str(exported_to.get("meta_path") or "")
    tokens = {
        "backup_path": display_path(backup_path, cwd),
        "meta_path": display_path(meta_path, cwd),
        "reason": str(meta.get("reason") or ""),
        "sha256": str(meta.get("sha256") or ""),
        "encrypted": "true" if meta.get("encrypted") else "false",
        "exported_backup_path": exported_backup_path,
        "exported_meta_path": exported_meta_path,
    }
    rendered: list[str] = []
    for arg in argv:
        rendered_arg = arg
        for key, value in tokens.items():
            rendered_arg = rendered_arg.replace("{" + key + "}", value)
        if "{" in rendered_arg or "}" in rendered_arg:
            raise BackupError(f"unsupported post_command template token in argument: {arg}")
        rendered.append(rendered_arg)
    return rendered


def with_post_command_meta(
    meta: dict[str, Any],
    argv: Sequence[str],
    executed_at: str,
    *,
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    updated = dict(meta)
    updated["post_command"] = {
        "argv": list(argv),
        "executed_at": executed_at,
        "returncode": returncode,
        "success": returncode == 0,
        "stdout": truncate_output(stdout),
        "stderr": truncate_output(stderr),
    }
    return updated


def update_exported_post_command_meta(meta: dict[str, Any], cwd: Path) -> None:
    exported_to = meta.get("exported_to")
    post_command = meta.get("post_command")
    if not isinstance(exported_to, dict) or not isinstance(post_command, dict):
        return
    meta_value = exported_to.get("meta_path")
    if not isinstance(meta_value, str) or not meta_value:
        return
    export_meta_path = Path(meta_value)
    if not export_meta_path.is_absolute():
        export_meta_path = cwd / export_meta_path
    if not export_meta_path.exists():
        return
    try:
        exported_meta = json.loads(export_meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"could not update exported backup meta with post_command result: {export_meta_path}: {exc}") from exc
    exported_meta["post_command"] = post_command
    export_meta_path.write_text(json.dumps(exported_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def truncate_output(value: str) -> str:
    if len(value) <= POST_COMMAND_OUTPUT_LIMIT:
        return value
    return value[:POST_COMMAND_OUTPUT_LIMIT] + "\n[truncated]"


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
    meta_paths = sorted(set(backup_dir.glob("*.db.meta.json")) | set(backup_dir.glob("*.db.age.meta.json")))
    for meta_path in meta_paths:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"could not read backup meta: {meta_path}: {exc}") from exc
        backup_path = meta_path.with_suffix("").with_suffix("")
        records.append(BackupRecord(backup_path=backup_path, meta_path=meta_path, meta=meta, db_exists=backup_path.exists()))
    records.sort(key=lambda record: str(record.meta.get("created_at", "")), reverse=True)
    return records


def prune_backup_records(
    db_path: Path | None = None,
    *,
    keep: int,
    include_reasons: set[str] | None = None,
    dry_run: bool = False,
) -> PruneResult:
    if keep < 0:
        raise BackupError("--keep must be zero or greater")
    if include_reasons is not None:
        invalid = include_reasons - BACKUP_REASONS
        if invalid:
            raise BackupError(f"invalid backup reason for prune: {sorted(invalid)[0]}")
    records = load_backup_records(db_path)
    if include_reasons is None:
        candidate_reasons = BACKUP_REASONS - DEFAULT_PRUNE_PROTECTED_REASONS
    else:
        candidate_reasons = set(include_reasons)

    protected: list[BackupRecord] = []
    candidates: list[BackupRecord] = []
    for record in records:
        reason = str(record.meta.get("reason") or "")
        if reason in candidate_reasons:
            candidates.append(record)
        else:
            protected.append(record)

    kept = candidates[:keep]
    pruned = candidates[keep:]
    if not dry_run:
        for record in pruned:
            remove_backup_record(record)
    return PruneResult(kept=kept, pruned=pruned, protected=protected)


def remove_backup_record(record: BackupRecord) -> None:
    errors: list[str] = []
    for path in (record.backup_path, record.meta_path):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise BackupError("could not prune backup record: " + "; ".join(errors))


def verify_backup_file(backup_path: Path) -> None:
    source = backup_path.resolve()
    if not source.exists():
        raise BackupError(f"backup not found: {source}")
    if source.suffix == ".age":
        raise BackupError("encrypted backup requires restore --decrypt")
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
    if meta.get("encrypted"):
        raise BackupError("encrypted backup requires restore --decrypt")
    integrity_check(source)
    actual_sha = sha256_file(source)
    if actual_sha != expected_sha:
        raise BackupError(f"backup sha256 mismatch: expected {expected_sha}, got {actual_sha}")


def load_backup_meta(backup_path: Path) -> dict[str, Any]:
    meta_path = meta_path_for_backup(backup_path)
    if not meta_path.exists():
        raise BackupError(f"backup meta not found: {meta_path}")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"could not read backup meta: {meta_path}: {exc}") from exc


def verify_encrypted_backup_file(encrypted_path: Path, plaintext_path: Path) -> None:
    source = encrypted_path.resolve()
    if not source.exists():
        raise BackupError(f"backup not found: {source}")
    meta = load_backup_meta(source)
    encryption = meta.get("encryption")
    if not meta.get("encrypted") or not isinstance(encryption, dict):
        raise BackupError(f"backup meta is not encrypted backup meta: {meta_path_for_backup(source)}")
    expected_ciphertext_sha = str(encryption.get("ciphertext_sha256") or meta.get("sha256") or "")
    if not expected_ciphertext_sha:
        raise BackupError(f"backup meta has no ciphertext sha256: {meta_path_for_backup(source)}")
    actual_ciphertext_sha = sha256_file(source)
    if actual_ciphertext_sha != expected_ciphertext_sha:
        raise BackupError(f"encrypted backup sha256 mismatch: expected {expected_ciphertext_sha}, got {actual_ciphertext_sha}")
    integrity_check(plaintext_path)
    expected_plaintext_sha = str(encryption.get("plaintext_sha256") or "")
    if not expected_plaintext_sha:
        raise BackupError(f"backup meta has no plaintext sha256: {meta_path_for_backup(source)}")
    actual_plaintext_sha = sha256_file(plaintext_path)
    if actual_plaintext_sha != expected_plaintext_sha:
        raise BackupError(f"decrypted backup sha256 mismatch: expected {expected_plaintext_sha}, got {actual_plaintext_sha}")


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


def replace_database_from_verified_backup(
    source: Path,
    destination: Path,
    *,
    cwd: Path | None = None,
    reported_backup_path: Path | None = None,
) -> RestoreResult:
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
        backup_path=reported_backup_path or source,
        before_restore=before_restore,
        integrity_check=final_check,
    )


def restore_backup(backup_path: Path, db_path: Path | None = None, *, cwd: Path | None = None) -> RestoreResult:
    source = backup_path.resolve()
    destination = resolve_db_path(db_path)
    if source == destination:
        raise BackupError("backup path is the same as destination database")
    verify_backup_file(source)
    return replace_database_from_verified_backup(source, destination, cwd=cwd)


def restore_encrypted_backup(
    backup_path: Path,
    db_path: Path | None = None,
    *,
    cwd: Path | None = None,
    age_command: str = "age",
) -> RestoreResult:
    source = backup_path.resolve()
    destination = resolve_db_path(db_path)
    if source == destination:
        raise BackupError("backup path is the same as destination database")
    require_age_command(age_command)
    destination.parent.mkdir(parents=True, exist_ok=True)
    plaintext_path = destination.with_name(f".{destination.name}.{os.getpid()}.decrypt.tmp")
    try:
        decrypt_file_with_age(source, plaintext_path, age_command)
        verify_encrypted_backup_file(source, plaintext_path)
        return replace_database_from_verified_backup(plaintext_path, destination, cwd=cwd, reported_backup_path=source)
    finally:
        try:
            plaintext_path.unlink()
        except FileNotFoundError:
            pass
        remove_sqlite_sidecars(plaintext_path)
