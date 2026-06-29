from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Any

from .timeutil import now_iso


STATE_FILE = "update-check.json"
CHECK_INTERVAL = timedelta(days=1)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


RunCommand = Callable[[list[str], Path], CommandResult]


@dataclass(frozen=True)
class UpdateCheckResult:
    status: str
    current_version: str = ""
    latest_version: str = ""
    reason: str = ""

    @property
    def update_available(self) -> bool:
        return self.status == "update_available"


def run_command(command: list[str], cwd: Path) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n"))


def state_path() -> Path:
    return Path.home() / ".nilo" / STATE_FILE


def default_state() -> dict[str, Any]:
    return {
        "lastCheckedAt": "",
        "lastNotifiedVersion": "",
        "lastStatus": "",
        "lastCurrentVersion": "",
        "lastLatestVersion": "",
    }


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = path or state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()
    if not isinstance(data, dict):
        return default_state()
    state = default_state()
    state.update({key: data.get(key) for key in state})
    return state


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    path = path or state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def reset_update_check_state(path: Path | None = None) -> None:
    try:
        (path or state_path()).unlink(missing_ok=True)
    except OSError:
        return


def is_disabled() -> bool:
    return os.environ.get("NILO_NO_UPDATE_CHECK") == "1"


def is_ci() -> bool:
    return os.environ.get("CI", "").casefold() == "true"


def is_interactive_output() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def should_auto_check(state: dict[str, Any], *, now: datetime | None = None) -> bool:
    checked_at = state.get("lastCheckedAt")
    if not checked_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(checked_at))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc).astimezone()
    return now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc) >= CHECK_INTERVAL


def package_location() -> Path:
    return Path(__file__).resolve().parent


def repo_root_from_package(run: RunCommand = run_command) -> tuple[Path | None, str]:
    result = run(["git", "rev-parse", "--show-toplevel"], package_location())
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve(), ""

    package_error = result.stderr.strip()
    cwd_result = run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    if cwd_result.returncode == 0 and cwd_result.stdout.strip():
        cwd_repo = Path(cwd_result.stdout.strip()).resolve()
        pyproject = cwd_repo / "pyproject.toml"
        if pyproject.exists() and 'name = "nilo"' in pyproject.read_text(encoding="utf-8", errors="ignore"):
            return cwd_repo, ""
    reason = package_error or cwd_result.stderr.strip() or "not a git repository"
    return None, reason


def git_latest_tag(repo: Path, ref: str | None, run: RunCommand) -> tuple[str, str]:
    command = ["git", "describe", "--tags", "--abbrev=0"]
    if ref:
        command.append(ref)
    result = run(command, repo)
    if result.returncode != 0 or not result.stdout.strip():
        return "", result.stderr.strip() or result.stdout.strip() or "tag not found"
    return result.stdout.strip(), ""


def is_ancestor(repo: Path, ancestor: str, descendant: str, run: RunCommand) -> tuple[bool | None, str]:
    result = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo)
    if result.returncode == 0:
        return True, ""
    if result.returncode == 1:
        return False, ""
    return None, result.stderr.strip() or "git merge-base failed"


def check_for_update(*, run: RunCommand = run_command, record_checked: bool = True) -> UpdateCheckResult:
    repo, repo_error = repo_root_from_package(run)
    if repo is None:
        return UpdateCheckResult("skipped", reason=repo_error or "not a git-based installation")

    if record_checked:
        state = load_state()
        state["lastCheckedAt"] = now_iso()
        save_state(state)

    fetch = run(["git", "fetch", "--tags", "--quiet"], repo)
    if fetch.returncode != 0:
        return UpdateCheckResult("skipped", reason=fetch.stderr.strip() or "git fetch failed")

    current, current_error = git_latest_tag(repo, None, run)
    if not current:
        return UpdateCheckResult("skipped", reason=current_error or "current tag not found")

    latest, latest_error = git_latest_tag(repo, "origin/main", run)
    if not latest:
        return UpdateCheckResult("skipped", reason=latest_error or "upstream tag not found")

    if current == latest:
        status = "up_to_date"
    else:
        behind, behind_error = is_ancestor(repo, current, latest, run)
        if behind is None:
            return UpdateCheckResult("skipped", current_version=current, latest_version=latest, reason=behind_error)
        status = "update_available" if behind else "up_to_date"
    if record_checked:
        state = load_state()
        state["lastStatus"] = status
        state["lastCurrentVersion"] = current
        state["lastLatestVersion"] = latest
        save_state(state)
    return UpdateCheckResult(status, current_version=current, latest_version=latest)


def update_message(result: UpdateCheckResult, *, language: str = "ja") -> str:
    if result.update_available:
        if language == "en":
            return f"Nilo update available: {result.current_version} -> {result.latest_version}\nRun: nilo upgrade"
        return f"Nilo の更新があります: {result.current_version} -> {result.latest_version}\n更新するには: nilo upgrade"
    if result.status == "up_to_date":
        return "Nilo is up to date."
    reason = result.reason or "not a git-based installation"
    return f"Nilo update check skipped: {reason}."


def should_suppress_for_version(state: dict[str, Any], latest_version: str) -> bool:
    return state.get("lastNotifiedVersion") == latest_version


def record_update_notified(latest_version: str) -> None:
    state = load_state()
    state["lastNotifiedVersion"] = latest_version
    save_state(state)


def auto_update_notice(*, run: RunCommand = run_command) -> str:
    if is_disabled() or is_ci() or not is_interactive_output():
        return ""
    state = load_state()
    if not should_auto_check(state):
        return ""
    result = check_for_update(run=run)
    if not result.update_available:
        return ""
    state = load_state()
    if should_suppress_for_version(state, result.latest_version):
        return ""
    record_update_notified(result.latest_version)
    return update_message(result)


def cached_instruction_note() -> str:
    if is_disabled() or is_ci():
        return ""
    state = load_state()
    if state.get("lastStatus") != "update_available":
        return ""
    current = str(state.get("lastCurrentVersion") or "")
    latest = str(state.get("lastLatestVersion") or "")
    if not current or not latest:
        return ""
    repo, _ = repo_root_from_package()
    if repo is not None:
        local_current, _ = git_latest_tag(repo, None, run_command)
        if local_current == latest:
            return ""
    return (
        "Nilo note:\n"
        f"Nilo の更新があります: {current} -> {latest}。\n"
        "AI agent は自動で `nilo upgrade` を実行してはいけません。\n"
        "人間に「nilo upgrade で更新できます」と短く伝えてください。\n"
    )
