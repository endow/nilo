from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Callable

from . import __version__
from .store import default_db_path


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


RunCommand = Callable[[list[str], Path], CommandResult]


def nilo_version() -> str:
    try:
        return metadata.version("nilo")
    except metadata.PackageNotFoundError:
        return __version__


def run_command(command: list[str], cwd: Path) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n"))


def package_location() -> Path:
    return Path(__file__).resolve().parent


def repo_root_from_package(run: RunCommand = run_command) -> tuple[Path | None, str]:
    start = package_location()
    result = run(["git", "rev-parse", "--show-toplevel"], start)
    if result.returncode != 0 or not result.stdout.strip():
        return None, result.stderr.strip()
    return Path(result.stdout.strip()).resolve(), ""


def current_branch(repo: Path, run: RunCommand = run_command) -> str:
    result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    if result.returncode != 0 or not result.stdout.strip():
        raise UpgradeError("Upgrade stopped: current branch could not be determined.", result)
    return result.stdout.strip()


def status_porcelain(repo: Path, run: RunCommand = run_command) -> str:
    result = run(["git", "status", "--porcelain"], repo)
    if result.returncode != 0:
        raise UpgradeError("Upgrade stopped: git status failed.", result)
    return result.stdout.strip()


def upstream_ref(repo: Path, branch: str, run: RunCommand = run_command) -> str:
    result = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return f"origin/{branch}"


def git_rev(repo: Path, ref: str, run: RunCommand = run_command) -> str:
    result = run(["git", "rev-parse", ref], repo)
    if result.returncode != 0 or not result.stdout.strip():
        raise UpgradeError(f"Upgrade stopped: git revision could not be resolved for {ref}.", result)
    return result.stdout.strip()


def backup_database(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"nilo-{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


class UpgradeError(RuntimeError):
    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


def print_failed_command(result: CommandResult | None) -> None:
    if result is None:
        return
    print(f"return_code: {result.returncode}")
    if result.stderr:
        print(result.stderr)
    elif result.stdout:
        print(result.stdout)


def run_upgrade(*, dry_run: bool = False, db_path: Path | None = None, run: RunCommand = run_command) -> int:
    print("Nilo upgrade")
    print()
    current_version = nilo_version()
    print(f"Current version: {current_version}")

    repo, repo_error = repo_root_from_package(run)
    if repo is None:
        print()
        print("This Nilo installation does not appear to be an editable git checkout.")
        print()
        print("Please update manually:")
        print("  cd /path/to/nilo")
        print("  git pull --ff-only")
        print("  pip install -e .")
        if repo_error:
            print()
            print(repo_error)
        return 1

    print(f"Repository: {repo}")
    try:
        branch = current_branch(repo, run)
        print(f"Branch: {branch}")
        print()
        print("Checking repository state...")
        local_changes = status_porcelain(repo, run)
        if local_changes:
            print("Local changes detected.")
            print("Nilo will not upgrade because local changes may be overwritten.")
            print()
            print("Run:")
            print("  git status")
            print()
            print("Then commit, stash, or discard the changes before running:")
            print("  nilo upgrade")
            print()
            print("Upgrade stopped: local changes detected.")
            return 1
        print("OK: no local changes")

        print()
        print("Checking remote updates...")
        fetch = run(["git", "fetch"], repo)
        if fetch.returncode != 0:
            raise UpgradeError("Upgrade failed: git fetch did not complete.", fetch)

        upstream = upstream_ref(repo, branch, run)
        local_rev = git_rev(repo, "HEAD", run)
        remote_rev = git_rev(repo, upstream, run)
        if local_rev == remote_rev:
            print("Already up to date.")
            print()
            if dry_run:
                print("Dry run: no update operations would be run.")
            print("Done.")
            print(f"Nilo is already {current_version}.")
            return 0

        print(f"Update available: {local_rev[:12]} -> {remote_rev[:12]}")
        if dry_run:
            print()
            print("Dry run: would run:")
            print("  git pull --ff-only")
            print(f"  {sys.executable} -m pip install -e {repo}")
            print("  nilo migrate --apply")
            print()
            print("Done.")
            return 0

        print()
        print("Pulling changes...")
        pull = run(["git", "pull", "--ff-only"], repo)
        if pull.returncode != 0:
            raise UpgradeError("Upgrade failed: git pull --ff-only did not complete.", pull)
        print("OK: repository updated")

        print()
        print("Reinstalling Nilo...")
        reinstall = run([sys.executable, "-m", "pip", "install", "-e", str(repo)], repo)
        if reinstall.returncode != 0:
            raise UpgradeError("Upgrade failed: package reinstall did not complete.", reinstall)
        print("OK: package reinstalled")

        resolved_db = (db_path or default_db_path()).resolve()
        print()
        print("Backing up database...")
        try:
            backup_path = backup_database(resolved_db)
        except OSError as exc:
            print(f"Upgrade stopped: database backup failed. {exc}")
            return 1
        if backup_path is None:
            print("Skipped: no database found.")
        else:
            try:
                display_backup = backup_path.relative_to(repo)
            except ValueError:
                display_backup = backup_path
            print(f"OK: backup created at {display_backup}")

        print()
        print("Running migrations...")
        migrate_command = [sys.executable, "-m", "nilo", "--db", str(resolved_db), "migrate", "--apply"]
        migrate = run(migrate_command, repo)
        if migrate.returncode != 0:
            raise UpgradeError("Upgrade failed: migrations did not complete.", migrate)
        print("OK: migrations completed")

        updated_version = installed_version_after_upgrade(repo, run)
        print()
        print("Done.")
        print(f"Nilo is now {updated_version}.")
        return 0
    except UpgradeError as exc:
        print(str(exc))
        print_failed_command(exc.result)
        return 1


def installed_version_after_upgrade(repo: Path, run: RunCommand = run_command) -> str:
    result = run([sys.executable, "-m", "nilo", "--version"], repo)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().replace("nilo ", "", 1)
    return nilo_version()
