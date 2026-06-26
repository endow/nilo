from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .gitmeta import git_output


class WorkspaceResolutionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        error: str = "workspace_resolution_error",
        registered_workspaces: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.registered_workspaces = registered_workspaces or []

    def response(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self.error, "message": str(self)}
        if self.registered_workspaces:
            result["registered_workspaces"] = self.registered_workspaces
        return result


def registry_path() -> Path:
    return Path.home() / ".nilo" / "workspaces.json"


def load_workspaces(path: Path | None = None) -> dict[str, dict[str, str]]:
    actual_path = path or registry_path()
    if not actual_path.exists():
        return {}
    try:
        data = json.loads(actual_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceResolutionError(f"invalid workspace registry: {actual_path}: {exc.msg}") from exc
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        raise WorkspaceResolutionError(f"invalid workspace registry: {actual_path}: workspaces must be an object")
    result: dict[str, dict[str, str]] = {}
    for name, entry in workspaces.items():
        if isinstance(name, str) and isinstance(entry, dict) and isinstance(entry.get("root"), str):
            result[name] = {"root": entry["root"]}
    return result


def save_workspaces(workspaces: dict[str, dict[str, str]], path: Path | None = None) -> None:
    actual_path = path or registry_path()
    actual_path.parent.mkdir(parents=True, exist_ok=True)
    actual_path.write_text(json.dumps({"workspaces": workspaces}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def workspace_db_path(root: Path) -> Path:
    return root / ".nilo" / "nilo.db"


def workspace_entry(name: str, root: str) -> dict[str, str]:
    resolved_root = resolve_project_root(root)
    return {"root": str(resolved_root)}


def add_workspace(name: str, root: str, *, force: bool = False, path: Path | None = None) -> dict[str, str]:
    workspaces = load_workspaces(path)
    if name in workspaces and not force:
        raise WorkspaceResolutionError(f"workspace already registered: {name}; use --force to overwrite", error="workspace_exists")
    entry = workspace_entry(name, root)
    workspaces[name] = entry
    save_workspaces(workspaces, path)
    return entry


def remove_workspace(name: str, path: Path | None = None) -> None:
    workspaces = load_workspaces(path)
    if name not in workspaces:
        raise WorkspaceResolutionError(f"workspace not registered: {name}", error="workspace_not_found", registered_workspaces=sorted(workspaces))
    del workspaces[name]
    save_workspaces(workspaces, path)


def show_workspace(name: str, path: Path | None = None) -> dict[str, str]:
    workspaces = load_workspaces(path)
    if name not in workspaces:
        raise WorkspaceResolutionError(f"workspace not registered: {name}", error="workspace_not_found", registered_workspaces=sorted(workspaces))
    root = resolve_project_root(workspaces[name]["root"])
    return {"name": name, "root": str(root), "db": str(workspace_db_path(root))}


def list_workspace_entries(path: Path | None = None) -> list[dict[str, str]]:
    entries = []
    for name, entry in sorted(load_workspaces(path).items()):
        root = Path(entry["root"]).expanduser()
        resolved_root = resolve_project_root_if_possible(root) if root.exists() and root.is_dir() else root
        entries.append(
            {
                "name": name,
                "root": str(resolved_root),
                "db": str(workspace_db_path(resolved_root)),
            }
        )
    return entries


def resolve_workspace_context(
    *,
    project_root: str | None = None,
    workspace: str | None = None,
    db_path: Path | None = None,
    default_cwd: Path | None = None,
) -> dict[str, str]:
    if project_root:
        root = resolve_project_root(project_root)
        return _context_for_root(root, "project_root")
    if workspace:
        workspaces = load_workspaces()
        if workspace not in workspaces:
            registered = sorted(workspaces)
            raise WorkspaceResolutionError(
                f"workspace not registered: {workspace}",
                error="workspace_not_found",
                registered_workspaces=registered,
            )
        root = resolve_project_root(workspaces[workspace]["root"])
        context = _context_for_root(root, "workspace")
        context["workspace"] = workspace
        return context
    if db_path is not None:
        resolved_db = db_path.expanduser().resolve()
        root = _root_from_db_path(resolved_db) or (default_cwd or Path.cwd()).expanduser().resolve()
        context = _context_for_root(resolve_project_root_if_possible(root), "db_path")
        context["db_path"] = str(resolved_db)
        return context
    root = resolve_project_root_if_possible(default_cwd or Path.cwd())
    return _context_for_root(root, "server_cwd")


def resolve_project_root(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise WorkspaceResolutionError(f"project_root not found: {path}", error="project_root_not_found")
    if not path.is_dir():
        raise WorkspaceResolutionError(f"project_root is not a directory: {path}", error="project_root_not_directory")
    return resolve_project_root_if_possible(path)


def resolve_project_root_if_possible(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    git_root = _git_root_for_path(path)
    if git_root:
        return Path(git_root)
    return path


def _context_for_root(root: Path, source: str) -> dict[str, str]:
    resolved_root = root.expanduser().resolve()
    git_root = _git_root_for_path(resolved_root)
    repository_name = resolved_root.name
    return {
        "project_root": str(resolved_root),
        "git_root": git_root,
        "db_path": str(workspace_db_path(resolved_root).resolve()),
        "project_id": repository_name,
        "repository_name": repository_name,
        "source": source,
    }


def _git_root_for_path(path: Path) -> str:
    code, out, _ = git_output(["rev-parse", "--show-toplevel"], path)
    if code == 0 and out.strip():
        return str(Path(out.strip()).expanduser().resolve())
    return ""


def _root_from_db_path(db_path: Path) -> Path | None:
    parts = db_path.parts
    if len(parts) >= 3 and parts[-2:] == (".nilo", "nilo.db"):
        return Path(*parts[:-2])
    return None
