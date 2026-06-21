from __future__ import annotations

import argparse
from types import ModuleType


def register_quality(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    quality = sub.add_parser("quality")
    quality_sub = quality.add_subparsers(dest="quality_command", required=True)

    quality_quick = quality_sub.add_parser("quick")
    quality_quick.add_argument("--task", required=True)
    quality_quick.add_argument("--summary")
    quality_quick.add_argument("--issue", action="append", default=[])
    quality_quick.add_argument("--score", action="append", default=[])
    quality_quick.add_argument("--required-score", action="append", default=[])
    quality_quick.add_argument("--strict-scores", action="store_true")
    quality_quick.add_argument("--interactive", action="store_true")
    quality_quick.add_argument("--reviewer", default="human")
    quality_quick.set_defaults(func=handlers.cmd_quality_quick)

    quality_autoscore = quality_sub.add_parser("autoscore")
    quality_autoscore_sub = quality_autoscore.add_subparsers(dest="quality_autoscore_command", required=True)
    quality_autoscore_prepare = quality_autoscore_sub.add_parser("prepare")
    quality_autoscore_prepare.add_argument("--task", required=True)
    quality_autoscore_prepare.add_argument("--required-score", action="append", default=[])
    quality_autoscore_prepare.set_defaults(func=handlers.cmd_quality_autoscore_prepare)
    quality_autoscore_import = quality_autoscore_sub.add_parser("import")
    quality_autoscore_import.add_argument("--task", required=True)
    quality_autoscore_import.add_argument("--file")
    quality_autoscore_import.add_argument("--required-score", action="append", default=[])
    quality_autoscore_import.add_argument("--strict-scores", action="store_true")
    quality_autoscore_import.add_argument("--allow-unknown-scores", action="store_true")
    quality_autoscore_import.add_argument("--reviewer", default="ai_autoscore")
    quality_autoscore_import.set_defaults(func=handlers.cmd_quality_autoscore_import)

    quality_schema = quality_sub.add_parser("schema")
    quality_schema_sub = quality_schema.add_subparsers(dest="quality_schema_command", required=True)
    quality_schema_set = quality_schema_sub.add_parser("set")
    quality_schema_set.add_argument("--project", required=True)
    quality_schema_set.add_argument("--required-score", action="append", default=[])
    quality_schema_set.set_defaults(func=handlers.cmd_quality_schema_set)
    quality_schema_list = quality_schema_sub.add_parser("list")
    quality_schema_list.add_argument("--project", required=True)
    quality_schema_list.set_defaults(func=handlers.cmd_quality_schema_list)
