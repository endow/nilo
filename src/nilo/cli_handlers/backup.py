from __future__ import annotations

import argparse

from ..backup import BackupError, create_backup, load_backup_records, restore_backup


def cmd_backup(args: argparse.Namespace) -> None:
    try:
        result = create_backup(args.db, reason=args.reason, export_dir=args.export)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"backup: {result.backup_path}")
    print(f"meta: {result.meta_path}")
    print(f"integrity_check: {result.meta['integrity_check']}")
    print(f"sha256: {result.meta['sha256']}")
    if result.meta.get("exported_to"):
        exported = result.meta["exported_to"]
        print(f"exported_to: {exported['backup_path']}")
        print(f"exported_meta: {exported['meta_path']}")


def cmd_backups(args: argparse.Namespace) -> None:
    try:
        records = load_backup_records(args.db)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    if not records:
        print("backups: none")
        return
    print("created_at                 reason          size_bytes  sha256        integrity  status   path")
    for record in records:
        meta = record.meta
        created_at = str(meta.get("created_at", ""))
        reason = str(meta.get("reason", ""))
        size = str(meta.get("db_size_bytes", ""))
        sha = str(meta.get("sha256", ""))[:12]
        integrity = str(meta.get("integrity_check", ""))
        status = "present" if record.db_exists else "missing"
        path = str(record.backup_path)
        print(f"{created_at[:25]:25}  {reason[:14]:14}  {size:>10}  {sha:12}  {integrity[:9]:9}  {status:7}  {path}")


def cmd_restore(args: argparse.Namespace) -> None:
    try:
        result = restore_backup(args.path, args.db)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"restored: {result.restored_path}")
    print(f"from: {result.backup_path}")
    if result.before_restore is not None:
        print(f"before_restore_backup: {result.before_restore.backup_path}")
        print(f"before_restore_meta: {result.before_restore.meta_path}")
    else:
        print("before_restore_backup: none")
    print(f"integrity_check: {result.integrity_check}")
