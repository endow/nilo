from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..view_model import overview
from ..view_server import run_view_server


def cmd_view(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    if args.format == "json":
        data = overview(args.db, project_id)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    run_view_server(
        db_path=args.db,
        project_id=project_id,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
