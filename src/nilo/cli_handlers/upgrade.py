from __future__ import annotations

import argparse

from ..upgrade import run_upgrade


def cmd_upgrade(args: argparse.Namespace) -> None:
    code = run_upgrade(dry_run=args.dry_run, db_path=args.db)
    if code != 0:
        raise SystemExit(code)
