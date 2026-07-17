#!/usr/bin/env python3
"""Report static Nilo table references in production Python sources."""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "nilo"


def schema_tables() -> set[str]:
    # Import only after resolving the repository so the script also works from
    # an installed checkout without requiring PYTHONPATH from the caller.
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from nilo.schema_catalog import SCHEMA_CATALOG

    return set(SCHEMA_CATALOG)


def literal_table(call: ast.Call) -> str | None:
    if not call.args:
        return None
    value = call.args[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def collect_usage() -> dict[str, dict[str, set[str]]]:
    tables = schema_tables()
    usage: dict[str, dict[str, set[str]]] = {
        table: defaultdict(set) for table in tables
    }
    store_methods = {
        "get": "read",
        "list_where": "read",
        "latest_for_task": "read",
        "insert": "create",
        "update": "update",
    }
    table_patterns = {
        table: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])")
        for table in tables
    }
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        covered_lines: dict[str, set[int]] = defaultdict(set)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            operation = store_methods.get(node.func.attr)
            table = literal_table(node)
            if operation and table in tables:
                usage[table][operation].add(f"{relative}:{node.lineno}")
                covered_lines[table].add(node.lineno)
        for lineno, line in enumerate(source.splitlines(), 1):
            for table, pattern in table_patterns.items():
                if lineno not in covered_lines[table] and pattern.search(line):
                    usage[table]["raw/reference"].add(f"{relative}:{lineno}")
    return usage


def render_markdown() -> str:
    usage = collect_usage()
    lines = [
        "# Schema usage report",
        "",
        "Static AST/text analysis of `src/nilo/**/*.py`. Dynamic table names may require manual review.",
        "",
    ]
    for table in sorted(usage):
        lines.append(f"## `{table}`")
        for operation in ("create", "read", "update", "raw/reference"):
            locations = sorted(usage[table].get(operation, set()))
            lines.append(f"- {operation}: {', '.join(f'`{item}`' for item in locations) if locations else 'none found'}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write Markdown to this path instead of stdout")
    args = parser.parse_args()
    report = render_markdown()
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
