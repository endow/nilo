from __future__ import annotations

import sys
from pathlib import Path

from .backup import BackupError, create_backup
from .command_runner import CommandResult, RunCommand, package_location, run_command as run_shell_command
from .store import default_db_path


def run_command(command: list[str], cwd: Path) -> CommandResult:
    return run_shell_command(command, cwd)


def repo_root_from_package(run: RunCommand = run_command) -> tuple[Path | None, str]:
    start = package_location()
    result = run(["git", "rev-parse", "--show-toplevel"], start)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve(), ""

    package_error = result.stderr.strip()
    cwd_result = run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    if cwd_result.returncode == 0 and cwd_result.stdout.strip():
        cwd_repo = Path(cwd_result.stdout.strip()).resolve()
        pyproject = cwd_repo / "pyproject.toml"
        if pyproject.exists() and 'name = "nilo"' in pyproject.read_text(encoding="utf-8", errors="ignore"):
            return cwd_repo, ""
    if cwd_result.stderr.strip():
        package_error = package_error or cwd_result.stderr.strip()
    if package_error:
        return None, package_error
    return None, "not a git repository"


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


def is_ancestor(repo: Path, ancestor: str, descendant: str, run: RunCommand = run_command) -> bool:
    result = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise UpgradeError("Upgrade stopped: git history relationship could not be determined.", result)


def backup_database(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    try:
        return create_backup(db_path, reason="before-upgrade").backup_path
    except BackupError as exc:
        raise OSError(str(exc)) from exc


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
            print(f"Nilo is up to date with {upstream}.")
            if not dry_run:
                from .update_check import reset_update_check_state

                reset_update_check_state()
            return 0

        local_is_behind = is_ancestor(repo, local_rev, remote_rev, run)
        remote_is_behind = is_ancestor(repo, remote_rev, local_rev, run)
        if remote_is_behind and not local_is_behind:
            print("Already up to date.")
            print()
            print(f"Local branch already contains {upstream}.")
            if dry_run:
                print("Dry run: no update operations would be run.")
            print("Done.")
            if not dry_run:
                from .update_check import reset_update_check_state

                reset_update_check_state()
            return 0
        if not local_is_behind:
            print("Upgrade stopped: local branch has diverged from upstream.")
            print()
            print("Run:")
            print("  git status")
            print("  git log --oneline --graph --decorate --left-right HEAD...@{u}")
            print()
            print("Resolve the branch relationship before running:")
            print("  nilo upgrade")
            return 1

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

        print()
        print("Done.")
        print(f"Nilo was updated from {local_rev[:12]} to {remote_rev[:12]}.")
        from .update_check import reset_update_check_state

        reset_update_check_state()
        return 0
    except UpgradeError as exc:
        print(str(exc))
        print_failed_command(exc.result)
        return 1
