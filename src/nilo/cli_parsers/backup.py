from __future__ import annotations

import argparse
from types import ModuleType

from ..backup import BACKUP_REASONS


def register_backup(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    backup = sub.add_parser("backup")
    backup.add_argument("--reason", choices=sorted(BACKUP_REASONS), default="manual")
    backup.set_defaults(func=handlers.cmd_backup)
