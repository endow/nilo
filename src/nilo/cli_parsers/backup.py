from __future__ import annotations

import argparse
from pathlib import Path
from types import ModuleType

from ..backup import BACKUP_REASONS


def register_backup(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    backup = sub.add_parser("backup")
    backup.add_argument("--reason", choices=sorted(BACKUP_REASONS), default="manual")
    backup.add_argument("--export", type=Path, default=None, help="Copy the verified backup and meta file to this directory")
    backup.add_argument("--encrypt", action="store_true", help="Encrypt the backup with age")
    backup.add_argument("--recipient", default=None, help="age recipient for --encrypt")
    backup.add_argument("--save-recipient", action="store_true", help="Save --recipient to .nilo/config.toml as backup.age_recipient")
    backup.set_defaults(func=handlers.cmd_backup)

    backups = sub.add_parser("backups")
    backups.set_defaults(func=handlers.cmd_backups)

    restore = sub.add_parser("restore")
    restore.add_argument("--decrypt", action="store_true", help="Decrypt an age-encrypted backup before restore")
    restore.add_argument("path", type=Path)
    restore.set_defaults(func=handlers.cmd_restore)
