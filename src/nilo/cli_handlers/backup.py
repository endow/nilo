from __future__ import annotations

import argparse

from ..backup import BackupError, create_backup


def cmd_backup(args: argparse.Namespace) -> None:
    try:
        result = create_backup(args.db, reason=args.reason)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"backup: {result.backup_path}")
    print(f"meta: {result.meta_path}")
    print(f"integrity_check: {result.meta['integrity_check']}")
    print(f"sha256: {result.meta['sha256']}")
