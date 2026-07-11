from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .failure import record_failure_log
from .gitmeta import git_output, porcelain_path
from .snapshot import compact_snapshot, current_git_snapshot


PROJECT_BINDING_PATH = ".nilo/project.json"


class ProjectBoundaryError(RuntimeError):
    def __init__(self, message: str, *, code: str = "project_boundary_error", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class ProjectBinding:
    project_name: str
    project_root: Path
    repository_id: str
    allow_self_modification: bool
    tool_owner_repository: Path | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "project_name": self.project_name,
            "project_root": str(self.project_root),
            "repository_id": self.repository_id,
            "allow_self_modification": self.allow_self_modification,
        }
        if self.tool_owner_repository is not None:
            data["tool_owner_repository"] = str(self.tool_owner_repository)
        return data


@dataclass
class ProjectBoundary:
    cwd: Path
    git_root: Path | None
    project_root: Path
    nilo_path: Path
    db_path: Path
    binding_path: Path
    project_name: str
    repository_id: str
    allow_self_modification: bool
    tool_owner_repository: Path | None
    binding_exists: bool
    mismatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_project": self.project_name,
            "project_root": str(self.project_root),
            "git_root": str(self.git_root) if self.git_root else "",
            "nilo_db": str(self.db_path),
            "writable_scope": str(self.project_root),
            "self_modification": "enabled" if self.allow_self_modification else "disabled",
            "tool_owner_repository": str(self.tool_owner_repository) if self.tool_owner_repository else "",
            "repository_id": self.repository_id,
            "binding_path": str(self.binding_path),
            "binding_exists": self.binding_exists,
            "binding_mismatch": self.mismatch,
        }

    def text_lines(self) -> list[str]:
        lines = [
            (
                "Project boundary: "
                f"Current project: {self.project_name}; "
                f"Project root: {self.project_root}; "
                f"Git root: {self.git_root or ''}; "
                f"Nilo DB: {self.db_path}; "
                f"Writable scope: {self.project_root}; "
                f"Self modification: {'enabled' if self.allow_self_modification else 'disabled'}"
            ),
        ]
        if self.tool_owner_repository:
            lines.append(f"Tool owner repository: {self.tool_owner_repository}")
        return lines

    def should_print_text(self) -> bool:
        return self.binding_exists or self.db_path == (self.project_root / ".nilo" / "nilo.db").resolve()


@dataclass
class WriteFenceResult:
    ok: bool
    boundary: ProjectBoundary
    inspected_repositories: list[str]
    changed_files: list[str]
    outside_writable_scope: list[str]
    outside_write_targets: list[str]
    tool_owner_changes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "inspected_repositories": self.inspected_repositories,
            "changed_files": self.changed_files,
            "outside_writable_scope": self.outside_writable_scope,
            "outside_write_targets": self.outside_write_targets,
            "tool_owner_changes": self.tool_owner_changes,
            "boundary": self.boundary.to_dict(),
        }


def git_root_for_path(cwd: Path) -> Path | None:
    code, out, _ = git_output(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not out.strip():
        return None
    return Path(out.strip()).expanduser().resolve()


def default_binding_for_root(root: Path, *, tool_owner_repository: Path | None = None, infer_owner: bool = False) -> ProjectBinding:
    repository_id = root.name
    is_nilo = is_nilo_repository_id(repository_id)
    owner = tool_owner_repository or (infer_tool_owner_repository() if infer_owner else None)
    return ProjectBinding(
        project_name="Nilo" if is_nilo else repository_id,
        project_root=root.resolve(),
        repository_id=repository_id,
        allow_self_modification=is_nilo,
        tool_owner_repository=None if is_nilo else owner,
    )


def infer_tool_owner_repository() -> Path | None:
    source_root = Path(__file__).resolve()
    for parent in source_root.parents:
        if (parent / ".git").exists():
            return parent.resolve()
    return None


def is_nilo_repository_id(repository_id: str) -> bool:
    return repository_id.casefold() == "nilo"


def load_project_binding(root: Path) -> ProjectBinding | None:
    path = root / PROJECT_BINDING_PATH
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    project_root = Path(str(data.get("project_root") or root)).expanduser().resolve()
    owner = data.get("tool_owner_repository")
    repository_id = str(data.get("repository_id") or project_root.name)
    return ProjectBinding(
        project_name=str(data.get("project_name") or data.get("repository_id") or root.name),
        project_root=project_root,
        repository_id=repository_id,
        allow_self_modification=bool(data.get("allow_self_modification", False)),
        tool_owner_repository=Path(str(owner)).expanduser().resolve() if owner else None,
    )


def write_project_binding(root: Path, binding: ProjectBinding) -> Path:
    path = root / PROJECT_BINDING_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(binding.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_project_boundary(
    cwd: Path | None = None,
    *,
    db_path: Path | None = None,
    create_missing: bool = False,
    repair: bool = False,
    tool_owner_repository: Path | None = None,
) -> ProjectBoundary:
    actual_cwd = (cwd or Path.cwd()).expanduser().resolve()
    git_root = git_root_for_path(actual_cwd)
    root = git_root or actual_cwd
    binding = load_project_binding(root)
    binding_exists = binding is not None
    if binding is None or repair:
        binding = default_binding_for_root(root, tool_owner_repository=tool_owner_repository, infer_owner=create_missing or repair)
        if create_missing or repair:
            write_project_binding(root, binding)
            binding_exists = True
    project_root = binding.project_root.resolve()
    mismatch = bool(git_root and project_root != git_root.resolve())
    actual_db_path = (db_path or (project_root / ".nilo" / "nilo.db")).expanduser().resolve()
    return ProjectBoundary(
        cwd=actual_cwd,
        git_root=git_root,
        project_root=project_root,
        nilo_path=project_root / ".nilo",
        db_path=actual_db_path,
        binding_path=root / PROJECT_BINDING_PATH,
        project_name=binding.project_name,
        repository_id=binding.repository_id,
        allow_self_modification=binding.allow_self_modification,
        tool_owner_repository=binding.tool_owner_repository,
        binding_exists=binding_exists,
        mismatch=mismatch,
    )


def require_binding_safe_for_write(boundary: ProjectBoundary) -> None:
    if not boundary.mismatch:
        return
    raise ProjectBoundaryError(
        "\n".join(
            [
                "Nilo project binding mismatch.",
                "",
                "Configured project_root:",
                f"  {boundary.project_root}",
                "",
                "Current git root:",
                f"  {boundary.git_root}",
                "",
                "Nilo will not continue because this session may be bound to the wrong project.",
            ]
        ),
        code="project_binding_mismatch",
        details=boundary.to_dict(),
    )


def assert_self_development_allowed(boundary: ProjectBoundary) -> None:
    if (
        boundary.git_root
        and is_nilo_repository_id(boundary.repository_id)
        and boundary.allow_self_modification
        and boundary.project_root == boundary.git_root.resolve()
    ):
        return
    raise ProjectBoundaryError(
        "\n".join(
            [
                "Nilo self-development mode is only available in the Nilo repository.",
                f"Current git root: {boundary.git_root or ''}",
                f"repository_id: {boundary.repository_id}",
                f"allow_self_modification: {str(boundary.allow_self_modification).lower()}",
            ]
        ),
        code="self_development_not_allowed",
        details=boundary.to_dict(),
    )


def changed_file_paths(repo_root: Path) -> list[Path]:
    code, out, _ = git_output(["-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=all"], repo_root)
    if code != 0:
        return []
    paths: list[Path] = []
    for line in out.splitlines():
        path = porcelain_path(line).replace("\\", "/")
        if path:
            paths.append((repo_root / path).resolve())
    return sorted(set(paths))


def evaluate_write_fence(boundary: ProjectBoundary, *, include_tool_owner_repository: bool = False) -> WriteFenceResult:
    repo_root = boundary.git_root or boundary.project_root
    changed = changed_file_paths(repo_root)
    inspected = [str(repo_root.resolve())]
    if include_tool_owner_repository and boundary.tool_owner_repository is not None and boundary.tool_owner_repository != repo_root:
        inspected.append(str(boundary.tool_owner_repository.resolve()))
        changed.extend(changed_file_paths(boundary.tool_owner_repository))
        changed = sorted(set(changed))
    outside = [str(path) for path in changed if not is_relative_to(path, boundary.project_root)]
    owner_changes: list[str] = []
    if include_tool_owner_repository and boundary.tool_owner_repository is not None:
        owner_changes = [str(path) for path in changed if is_relative_to(path, boundary.tool_owner_repository)]
    outside_write_targets = (
        []
        if is_relative_to(boundary.db_path, boundary.project_root) or is_temporary_path(boundary.db_path)
        else [str(boundary.db_path)]
    )
    ok = not outside and not outside_write_targets and not (owner_changes and not self_modification_allowed(boundary))
    return WriteFenceResult(
        ok=ok,
        boundary=boundary,
        inspected_repositories=inspected,
        changed_files=[str(path) for path in changed],
        outside_writable_scope=outside,
        outside_write_targets=outside_write_targets,
        tool_owner_changes=owner_changes,
    )


def self_modification_allowed(boundary: ProjectBoundary) -> bool:
    return (
        is_nilo_repository_id(boundary.repository_id)
        and boundary.allow_self_modification
        and boundary.git_root is not None
        and boundary.project_root == boundary.git_root.resolve()
    )


def require_write_fence(boundary: ProjectBoundary, *, include_tool_owner_repository: bool = False) -> WriteFenceResult:
    require_binding_safe_for_write(boundary)
    result = evaluate_write_fence(boundary, include_tool_owner_repository=include_tool_owner_repository)
    if result.ok:
        return result
    if result.tool_owner_changes and not self_modification_allowed(boundary):
        message = [
            f"Nilo tool failure detected during {boundary.project_name} task.",
            "",
            "This appears to be a Nilo defect, not a project code issue.",
            "Nilo source modification is disabled in this project session.",
            "",
            "Repository identity:",
            f"  target_project_root: {boundary.project_root}",
            f"  target_git_root: {boundary.git_root or ''}",
            f"  tool_owner_repository: {boundary.tool_owner_repository or ''}",
            "  inspected_repositories:",
            *[f"    {path}" for path in result.inspected_repositories],
            "",
            "Suggested next step:",
            "Switch to the Nilo repository and start a separate self-development task.",
            "",
            "Changed files:",
            *[f"  {path}" for path in result.tool_owner_changes],
        ]
        raise ProjectBoundaryError("\n".join(message), code="nilo_self_modification_forbidden", details=result.to_dict())
    message = [
        "Write fence violation detected.",
        "",
        "Current project:",
        f"  {boundary.project_name}",
        "",
        "Repository identity:",
        f"  target_project_root: {boundary.project_root}",
        f"  target_git_root: {boundary.git_root or ''}",
        "  inspected_repositories:",
        *[f"    {path}" for path in result.inspected_repositories],
        "",
        "Writable scope:",
        f"  {boundary.project_root}",
        "",
        "Changed files outside writable scope:",
        *[f"  {path}" for path in result.outside_writable_scope],
        "",
        "Write targets outside writable scope:",
        *[f"  {path}" for path in result.outside_write_targets],
        "",
        "This task cannot be completed from the current project session.",
    ]
    raise ProjectBoundaryError("\n".join(message), code="write_fence_violation", details=result.to_dict())


def record_nilo_issue_for_task(store: Any, project_id: str, task_id: str, command: str, error: ProjectBoundaryError, boundary: ProjectBoundary) -> dict[str, Any]:
    message = "\n".join(
        [
            f"Nilo tool failure detected during {boundary.project_name} task.",
            "",
            f"command: {command}",
            f"target_project: {project_id}",
            f"project_root: {boundary.project_root}",
            f"git_root: {boundary.git_root or ''}",
            f"tool_owner_repository: {boundary.tool_owner_repository or ''}",
            f"nilo_db: {boundary.db_path}",
            f"error_code: {error.code}",
            f"inspected_repositories: {', '.join(error.details.get('inspected_repositories', []))}",
            "",
            str(error),
            "",
            "Why this is classified as a Nilo issue:",
            "The project boundary or write fence detected Nilo/tool repository modification from a session where self modification is disabled.",
            "",
            "Nilo source modification was forbidden for this project session.",
            "Next step: switch to the Nilo repository and create a separate self-development task.",
        ]
    )
    return record_failure_log(
        store,
        project_id,
        task_id,
        "",
        "NiloIssue",
        message,
        "high",
        source="project_boundary",
        actor="nilo",
        related_id=command,
        snapshot=compact_snapshot(current_git_snapshot(boundary.git_root or boundary.project_root)),
        status="open",
    )


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def temporary_roots() -> list[Path]:
    roots = [tempfile.gettempdir(), "/tmp"]
    roots.extend(value for key in ("TMPDIR", "TMP", "TEMP") if (value := os.environ.get(key)))
    resolved: list[Path] = []
    for root in roots:
        try:
            path = Path(root).expanduser().resolve()
        except OSError:
            continue
        if path not in resolved:
            resolved.append(path)
    return resolved


def is_temporary_path(path: Path) -> bool:
    return any(is_relative_to(path, root) for root in temporary_roots())


def boundary_warning_lines(boundary: ProjectBoundary) -> list[str]:
    lines: list[str] = []
    if not boundary.binding_exists and boundary.db_path == (boundary.project_root / ".nilo" / "nilo.db").resolve():
        lines.append(f"warning: missing project binding: {boundary.binding_path}")
    if boundary.mismatch:
        lines.extend(
            [
                "warning: Nilo project binding mismatch.",
                f"configured project_root: {boundary.project_root}",
                f"current git root: {boundary.git_root}",
            ]
        )
    return lines


def project_boundary_prompt(boundary: ProjectBoundary) -> str:
    tool_owner_forbidden = []
    if boundary.tool_owner_repository and not self_modification_allowed(boundary):
        tool_owner_forbidden.extend(
            [
                f"- Do not modify {boundary.tool_owner_repository}",
                "- Do not modify Nilo source code from this session",
                "- If a Nilo defect is found, record it as ToolFailure/NiloIssue and stop",
            ]
        )
    return "\n".join(
        [
            f"Project boundary: current={boundary.project_name}; writable={boundary.project_root}; Nilo self-modification={'enabled' if self_modification_allowed(boundary) else 'disabled'}.",
            "Forbidden: No writes outside the current writable repository.",
            "External files explicitly provided by the user may be read as read-only references.",
            "Do not modify sibling repositories, parent directories, or another project's .nilo database.",
            *tool_owner_forbidden,
        ]
    )
