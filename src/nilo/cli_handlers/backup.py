from __future__ import annotations

import argparse

from ..backup import (
    BackupError,
    configured_age_recipient,
    create_backup,
    load_backup_records,
    restore_backup,
    restore_encrypted_backup,
    save_age_recipient,
)


def cmd_backup(args: argparse.Namespace) -> None:
    if (args.recipient or args.save_recipient) and not args.encrypt:
        raise SystemExit("--recipient requires --encrypt; --save-recipient requires --encrypt")
    recipient = args.recipient
    try:
        if args.encrypt and not recipient:
            recipient = configured_age_recipient(args.db)
        result = create_backup(args.db, reason=args.reason, export_dir=args.export, encrypt=args.encrypt, recipient=recipient)
        saved_config = save_age_recipient(args.db, recipient or "") if args.save_recipient else None
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"backup: {result.backup_path}")
    print(f"meta: {result.meta_path}")
    print(f"integrity_check: {result.meta['integrity_check']}")
    print(f"sha256: {result.meta['sha256']}")
    if result.meta.get("encrypted"):
        encryption = result.meta["encryption"]
        print("encrypted: true")
        print(f"plaintext_sha256: {encryption['plaintext_sha256']}")
        print(f"ciphertext_sha256: {encryption['ciphertext_sha256']}")
    if saved_config is not None:
        print(f"saved_recipient: {saved_config}")
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
        if args.decrypt:
            result = restore_encrypted_backup(args.path, args.db)
        else:
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
