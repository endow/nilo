from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from run_cli_group import GROUPS as CLI_GROUPS
except ModuleNotFoundError:
    from tests.run_cli_group import GROUPS as CLI_GROUPS

CLI_REVIEW_SHARDS = ["cli:review-core", "cli:review-dispatch", "cli:review-mcp", "cli:review-workflow"]
CLI_ROADMAP_SHARDS = ["cli:roadmap-assess", "cli:roadmap-discuss", "cli:roadmap-import", "cli:roadmap-lifecycle"]
TARGETED_CLI_GROUPS = {"smoke", "compat"}


@dataclass(frozen=True)
class TestShard:
    name: str
    command: tuple[str, ...]
    description: str = ""

    def command_text(self) -> str:
        return " ".join(_quote_command_part(part) for part in self.command)


def _quote_command_part(part: str) -> str:
    if not part:
        return '""'
    if any(char.isspace() for char in part) or any(char in part for char in '"\'\\'):
        return repr(part)
    return part


CLI_SHARDS: dict[str, TestShard] = {
    f"cli:{group}": TestShard(
        name=f"cli:{group}",
        command=(sys.executable, "tests/run_cli_group.py", group),
        description=f"tests.test_cli focused group: {group}",
    )
    for group in sorted([group for group in CLI_GROUPS if group != "workflow"])
}

INTEGRATION_SHARDS: dict[str, TestShard] = {
    "integration:git": TestShard(
        name="integration:git",
        command=(sys.executable, "-m", "unittest", "tests.test_cli_git_integration", "tests.test_version_advisor"),
        description="git/subprocess-backed CLI and version advisor integration tests",
    ),
    "integration:workflow": TestShard(
        name="integration:workflow",
        command=(sys.executable, "tests/run_cli_group.py", "workflow"),
        description="cross-feature CLI workflows that intentionally chain commands",
    ),
}


UNIT_MODULES: dict[str, tuple[str, ...]] = {
    "unit:backup": ("tests.test_backup",),
    "unit:review_dispatcher": ("tests.test_review_dispatcher",),
    "unit:mcp": ("tests.test_mcp_server",),
    "unit:snapshot": ("tests.test_snapshot_policy",),
    "unit:gitmeta": ("tests.test_gitmeta",),
    "unit:upgrade": ("tests.test_upgrade", "tests.test_update_check"),
    "unit:verification": ("tests.test_verification",),
    "unit:status": ("tests.test_status_surface",),
    "unit:other": (
        "tests.test_failure_patterns",
        "tests.test_failure_ledger",
        "tests.test_guard",
        "tests.test_human_status",
        "tests.test_project_boundary",
        "tests.test_release_workflow",
        "tests.test_review",
        "tests.test_shard_runner",
        "tests.test_shards",
        "tests.test_state_audit",
        "tests.test_transitions",
        "tests.test_write_paths",
    ),
}

UNIT_SHARDS: dict[str, TestShard] = {
    name: TestShard(
        name=name,
        command=(sys.executable, "-m", "unittest", *modules),
        description=" ".join(modules),
    )
    for name, modules in UNIT_MODULES.items()
}

SHARDS: dict[str, TestShard] = {**CLI_SHARDS, **INTEGRATION_SHARDS, **UNIT_SHARDS}
TARGETED_SHARD_NAMES = {f"cli:{group}" for group in TARGETED_CLI_GROUPS}
FULL_SHARD_NAMES = sorted(name for name in SHARDS if name not in TARGETED_SHARD_NAMES)


def all_shards() -> list[TestShard]:
    return [SHARDS[name] for name in FULL_SHARD_NAMES]


def shard_names() -> list[str]:
    return sorted(SHARDS)


def get_shard(name: str) -> TestShard:
    try:
        return SHARDS[name]
    except KeyError as exc:
        raise KeyError(f"unknown shard: {name}") from exc


def resolve_shards(names: list[str]) -> list[TestShard]:
    return [get_shard(name) for name in names]


def auto_jobs(shard_count: int, cpu_count: int | None = None) -> int:
    if shard_count <= 0:
        return 1
    cpus = cpu_count or 2
    return max(1, min(cpus, shard_count, 8))


def changed_files(cwd: Path) -> list[str]:
    files: set[str] = set()
    for args in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--name-only", "--staged"],
        ["git", "diff", "--name-only", "HEAD"],
    ):
        completed = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            path = line.strip().replace("\\", "/")
            if path:
                files.add(path)
    return sorted(files)


def shards_for_changed_files(paths: list[str]) -> list[str]:
    selected: set[str] = set()
    unknown = False
    for raw_path in paths:
        path = raw_path.replace("\\", "/")
        if path.startswith("tests/run_cli_group.py") or path.startswith("tests/run_shards.py") or path.startswith("tests/test_shards.py"):
            selected.update(["cli:smoke", "cli:compat", "unit:other"])
        elif path.startswith("tests/test_cli.py"):
            selected.update(["cli:smoke", "cli:compat", "integration:git"])
        elif path.startswith("tests/test_cli_git_integration.py"):
            selected.add("integration:git")
        elif path.startswith("tests/test_backup") or path == "src/nilo/backup.py" or path.startswith("src/nilo/cli_parsers/backup") or path.startswith("src/nilo/cli_handlers/backup"):
            selected.add("unit:backup")
        elif path.startswith("src/nilo/cli_handlers/recipe") or path.startswith("src/nilo/cli_parsers/recipe"):
            selected.add("cli:recipe")
        elif path.startswith("src/nilo/cli_handlers/roadmap") or path.startswith("src/nilo/cli_parsers/roadmap"):
            selected.update(CLI_ROADMAP_SHARDS)
        elif path.startswith("src/nilo/cli_handlers/project") or path.startswith("src/nilo/cli_parsers/project"):
            selected.add("cli:project")
        elif path.startswith("src/nilo/cli_handlers/task") or path.startswith("src/nilo/cli_parsers/task"):
            selected.add("cli:task")
        elif path.startswith("src/nilo/cli_handlers/todo") or path.startswith("src/nilo/cli_parsers/todo"):
            selected.add("cli:todo")
        elif path.startswith("src/nilo/cli_handlers/quality") or path.startswith("src/nilo/cli_parsers/quality"):
            selected.add("cli:quality")
        elif path.startswith("src/nilo/cli_handlers/review") or path.startswith("src/nilo/cli_parsers/review"):
            selected.update(CLI_REVIEW_SHARDS)
        elif path.startswith("src/nilo/cli_handlers/report") or path.startswith("src/nilo/cli_parsers/report"):
            selected.add("cli:report")
        elif path.startswith("src/nilo/cli_handlers/backup") or path.startswith("src/nilo/cli_parsers/backup"):
            selected.add("unit:backup")
        elif path.startswith("src/nilo/cli_handlers/facade") or path.startswith("src/nilo/cli_parsers/facade"):
            selected.add("cli:status")
        elif path.startswith("src/nilo/cli_handlers/overdrive") or path.startswith("src/nilo/cli_parsers/overdrive"):
            selected.add("cli:status")
        elif path.startswith("src/nilo/cli_handlers/mcp") or path.startswith("src/nilo/cli_parsers/mcp"):
            selected.update(["unit:mcp", "cli:compat"])
        elif path.startswith("tests/test_review_dispatcher") or path.startswith("src/nilo/review_dispatcher") or path.startswith("src/nilo/reviewer_registry"):
            selected.update(["cli:review-dispatch", "unit:review_dispatcher"])
        elif path.startswith("tests/test_review") or path.startswith("src/nilo/review_") or path == "src/nilo/review.py" or path.startswith("src/nilo/cli_handlers/quality") or path.startswith("src/nilo/cli_parsers/quality"):
            selected.update([*CLI_REVIEW_SHARDS, "unit:review_dispatcher"])
        elif path.startswith("tests/test_mcp") or path.startswith("src/nilo/mcp"):
            selected.update(["unit:mcp", "cli:compat"])
        elif path.startswith("tests/test_snapshot") or path.startswith("src/nilo/snapshot"):
            selected.add("unit:snapshot")
        elif path.startswith("tests/test_gitmeta") or path.startswith("src/nilo/gitmeta"):
            selected.add("unit:gitmeta")
        elif path.startswith("tests/test_version_advisor") or path.startswith("src/nilo/version_advisor"):
            selected.add("integration:git")
        elif path.startswith("tests/test_upgrade") or path.startswith("tests/test_update_check") or path.startswith("src/nilo/upgrade") or path.startswith("src/nilo/update_check"):
            selected.add("unit:upgrade")
        elif path.startswith("tests/test_verification") or path.startswith("src/nilo/verification"):
            selected.update(["cli:verification", "unit:verification"])
        elif path.startswith("src/nilo/recipe"):
            selected.add("cli:recipe")
        elif path.startswith("src/nilo/roadmap"):
            selected.update(CLI_ROADMAP_SHARDS)
        elif path.startswith("src/nilo/project"):
            selected.add("cli:project")
        elif path.startswith("src/nilo/report"):
            selected.add("cli:report")
        elif path.startswith("src/nilo/store"):
            selected.add("cli:store")
        elif path.startswith("src/nilo/task") or path.startswith("src/nilo/transitions"):
            selected.add("cli:task")
        elif path.startswith("src/nilo/quality"):
            selected.add("cli:quality")
        elif path.startswith("src/nilo/guard") or path.startswith("src/nilo/secret"):
            selected.update(["cli:guard", "unit:other"])
        elif path.startswith("src/nilo/cli") or path.startswith("src/nilo/cli_handlers") or path.startswith("src/nilo/cli_parsers"):
            selected.update(["cli:smoke", "cli:compat"])
        elif path.startswith("tests/"):
            selected.add("unit:other")
        elif path.startswith("README") or path.startswith("docs/"):
            selected.add("cli:compat")
        else:
            unknown = True
    if unknown or not selected:
        selected.update(["cli:smoke", "cli:compat", "unit:other"])
    return sorted(selected)
