from __future__ import annotations

import argparse
from pathlib import Path
from types import ModuleType

from ..backup import BACKUP_REASONS


def register_backup(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    backup = sub.add_parser("backup")
    backup.add_argument("--reason", choices=sorted(BACKUP_REASONS), default="manual")
    backup.add_argument("--export", type=Path, default=None, help="Copy the verified backup and meta file to this directory")
    backup.set_defaults(func=handlers.cmd_backup)

    backups = sub.add_parser("backups")
    backups.set_defaults(func=handlers.cmd_backups)

    restore = sub.add_parser("restore")
    restore.add_argument("path", type=Path)
    restore.set_defaults(func=handlers.cmd_restore)
