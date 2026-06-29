from __future__ import annotations

import argparse
from types import ModuleType


def register_roadmap(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    roadmap = sub.add_parser("roadmap")
    roadmap_sub = roadmap.add_subparsers(dest="roadmap_command", required=True)

    roadmap_status = roadmap_sub.add_parser("status")
    roadmap_status.add_argument("--project", required=True)
    roadmap_status.add_argument("--ai", action="store_true", help="Accepted for AI detail-command compatibility.")
    roadmap_status.set_defaults(func=handlers.cmd_roadmap_status)

    roadmap_assess = roadmap_sub.add_parser("assess")
    roadmap_assess.add_argument("--project", required=True)
    roadmap_assess.add_argument(
        "--file",
        "--output",
        dest="file",
        help="write generated roadmap assessment to this output file",
    )
    roadmap_assess.set_defaults(func=handlers.cmd_roadmap_assess)

    roadmap_summary = roadmap_sub.add_parser("summary")
    roadmap_summary.add_argument("--project", required=True)
    roadmap_summary.add_argument(
        "--file",
        "--output",
        dest="file",
        help="write generated roadmap summary to this output file",
    )
    roadmap_summary.set_defaults(func=handlers.cmd_roadmap_summary)

    roadmap_discuss = roadmap_sub.add_parser(
        "discuss",
        description="Generate roadmap discussion context without creating a pending roadmap revision.",
    )
    roadmap_discuss.add_argument("--project", required=True)
    roadmap_discuss.add_argument(
        "--file",
        "--output",
        dest="file",
        help="write generated roadmap discussion context to this output file",
    )
    roadmap_discuss.set_defaults(func=handlers.cmd_roadmap_discuss)

    roadmap_task_plan = roadmap_sub.add_parser("task-plan")
    roadmap_task_plan.add_argument("--commitment", required=True)
    roadmap_task_plan.add_argument(
        "--file",
        "--output",
        dest="file",
        help="write generated roadmap task plan to this output file",
    )
    roadmap_task_plan.set_defaults(func=handlers.cmd_roadmap_task_plan)

    roadmap_execute = roadmap_sub.add_parser("execute")
    roadmap_execute.add_argument("--project", required=True)
    roadmap_execute.add_argument("--overdrive", action="store_true")
    roadmap_execute.add_argument("--commitment", default="")
    roadmap_execute.add_argument("--max-failures", type=int, default=3)
    roadmap_execute.set_defaults(func=handlers.cmd_roadmap_execute)

    roadmap_export = roadmap_sub.add_parser("export")
    roadmap_export.add_argument("--project", required=True)
    roadmap_export.add_argument(
        "--file",
        "--output",
        dest="file",
        help="write generated human roadmap to this output file",
    )
    roadmap_export.set_defaults(func=handlers.cmd_roadmap_export)

    roadmap_import = roadmap_sub.add_parser("import")
    roadmap_import.add_argument("--project", required=True)
    roadmap_import.add_argument("--file")
    roadmap_import.set_defaults(func=handlers.cmd_roadmap_import)

    roadmap_adopt = roadmap_sub.add_parser("adopt")
    roadmap_adopt.add_argument("--project", required=True)
    roadmap_adopt.add_argument("--file", required=True)
    roadmap_adopt.add_argument("--reason", required=True)
    roadmap_adopt.add_argument("--actor", choices=["human"], required=True)
    roadmap_adopt.add_argument("--human-confirm", action="store_true")
    roadmap_adopt.add_argument("--decision-note", default="")
    roadmap_adopt.add_argument("--roadmap-file")
    roadmap_adopt.set_defaults(func=handlers.cmd_roadmap_adopt)

    roadmap_accept = roadmap_sub.add_parser("accept")
    roadmap_accept.add_argument("--revision", required=True)
    roadmap_accept.add_argument("--reason", required=True)
    roadmap_accept.add_argument("--actor", choices=["human"], required=True)
    roadmap_accept.add_argument("--human-confirm", action="store_true")
    roadmap_accept.add_argument("--decision-note", default="")
    roadmap_accept.set_defaults(func=handlers.cmd_roadmap_accept)

    roadmap_reject = roadmap_sub.add_parser("reject")
    roadmap_reject.add_argument("--revision", required=True)
    roadmap_reject.add_argument("--reason", required=True)
    roadmap_reject.add_argument("--actor", choices=["human"], required=True)
    roadmap_reject.add_argument("--human-confirm", action="store_true")
    roadmap_reject.add_argument("--decision-note", default="")
    roadmap_reject.set_defaults(func=handlers.cmd_roadmap_reject)

    roadmap_close = roadmap_sub.add_parser("close")
    roadmap_close.add_argument("--commitment", required=True)
    roadmap_close.add_argument("--reason", required=True)
    roadmap_close.add_argument("--actor", choices=["human", "ai"], required=True)
    roadmap_close.add_argument("--force", action="store_true")
    roadmap_close.add_argument("--human-confirm", action="store_true")
    roadmap_close.add_argument("--decision-note", default="")
    roadmap_close.set_defaults(func=handlers.cmd_roadmap_close)
