from __future__ import annotations

import argparse
from pathlib import Path

from ..agent_installation import (
    install_agent_runtime_files,
    legacy_agent_block_files,
    migrate_legacy_agent_blocks,
)
from ..cli import (
    AGENT_TARGET_FILES,
)
from ..project_model import default_project_row
from ..project_boundary import (
    resolve_project_boundary,
)
from ..store import Store
from ..timeutil import now_iso


def cmd_agent_install(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        targets = list(AGENT_TARGET_FILES) if args.target == "all" else [args.target]
        install_agent_blocks(project, targets)
    finally:
        store.close()


def install_agent_blocks(
    project: dict, targets: list[str], *, warn_unmanaged: bool = True
) -> None:
    result = install_agent_runtime_files(
        project, targets, warn_unmanaged=warn_unmanaged
    )
    for path in result.updated_paths:
        print(f"updated: {path}")
    for warning in result.warnings:
        print(f"warning: {warning}")


def print_legacy_agent_block_guidance(files: list[Path]) -> None:
    if not files:
        return
    names = ", ".join(path.name for path in files)
    print(
        f"warning: deprecated Nilo managed block remains in tracked agent file(s): {names}"
    )
    print("Nilo no longer updates tracked CLAUDE.md / AGENTS.md files by default.")
    print("To remove the old generated block and refresh local runtime files, run:")
    print("  nilo migrate --apply")


def project_for_current_directory(store: Store) -> dict:
    project_id = Path.cwd().name
    project = store.get("projects", project_id)
    return project if project else default_project_row(project_id, now_iso())


def cmd_help_ai(args: argparse.Namespace) -> None:
    print(
        "\n".join(
            [
                "Nilo AI normal work:",
                '- Start normal development requests with `nilo work "<user request>"`; it creates or selects the task and prints a compact session card.',
                "- Fallback: Start with `nilo status --ai` and `nilo next` when `nilo work` stops or background context is needed.",
                "- Follow the first action shown by `nilo next`; use `nilo next --verbose` only when background context is needed.",
                "- Detailed evidence, roadmap, failure, review, and recipe context is retained, but fetched on demand instead of expanded every turn.",
                "- Set `NILO_AI_CONTEXT_MAX_CHARS` before starting Nilo to cap compact AI text; overflow keeps the action card and points to detail commands.",
                '- When a recipe/workflow is active, generic continuation commands such as "進めて", "続けて", or "next" continue that workflow only.',
                "- Do not switch to unrelated project tasks unless the user explicitly asks to proceed to another task.",
                "- For release recipes, tag/push/GitHub release/package publish are public operations and require explicit user approval.",
                "- If a release recipe is blocked by verification failure, fix it inside the current release task, pass full check, then resume release; do not create another task.",
                "- Do not treat commit-created git head changes as stale evidence if the commit was created by Nilo from the verified dirty tree.",
                "- Do not treat stale, missing, or failed evidence as complete.",
                "- Do not treat unresolved review findings as complete.",
                '- After verification, record it with `nilo check --task <task_id> "..." --mode quick|targeted|full`.',
                "- Do not use `nilo work --check` for verification-only recording; it also records AI completion when verification succeeds.",
                "- Omit `--task` only when Nilo can safely infer exactly one unfinished verification target.",
                "- Use quick for narrow smoke checks, targeted for changed modules or focused test groups, and full for releases or broad-risk changes.",
                "- Treat timeouts as guardrails around a chosen scope, not as the main way to make full-suite verification practical.",
                "- Final completion acceptance remains a human decision.",
                "- Run Claude review only when the user explicitly requests it in the current request: `nilo review claude --task <task_id> --user-requested`.",
                "- Do not create or launch a review autonomously as a completion condition.",
                "- Use `nilo review claude --with-mcp` only when Claude needs optional Nilo MCP context tools.",
                "- MCP is not the normal entrypoint; use available Nilo MCP tools for state inspection, reviewer workers, or MCP-based evidence recording.",
                "- `nilo review dispatch`, `nilo review quick`, `nilo review delegate`, and MCP reviewer worker orchestration are legacy/advanced/fallback paths.",
                "",
                "MCP:",
                "- Do not trust MCP only because it is callable; first confirm its identity matches the current repository.",
                "- `expected_project` is a repository identity guard, usually the repository directory name, not an arbitrary Nilo DB project id.",
                "- `repository_mismatch` returns `ok: false` and no normal status payload.",
                "- MCP multi-workspace: when working across repositories, pass `project_root` or `workspace` to the MCP tool.",
                "- MCP multi-workspace priority: project_root, workspace, then MCP server default cwd.",
                "- Check MCP response identity and use only results whose repository / db_path match the target repository.",
                "- If the default repository differs from the target and the target root is known, retry with `project_root` instead of asking the human.",
                "- On unresolved mismatch, use CLI fallback without asking the human: run `nilo status --ai` and then `nilo next` from the target repository cwd.",
                "- Do not ask humans for missing values that Nilo output or project state can safely infer uniquely.",
                "- For release recipe `target_version`, use the next patch when the current version and latest git tag match, or when no SemVer tag exists and the current version is unique.",
                "- Ask only for multiple candidates, contradictory state, or pre-publication/destructive-operation confirmation.",
                "",
                "Work size:",
                "- Before implementation, judge whether the request is small or large.",
                "- Small work can proceed as a normal task.",
                "- A coherent bug fix can proceed as a normal task even when it touches several files.",
                "- For large work, recommend roadmap planning to the human instead of creating it automatically.",
                "",
                "Use roadmap first when the work:",
                "- has multiple features or implementation tracks,",
                "- has unclear scope or several independent completion criteria,",
                "- changes DB schema or migrations with broad data or compatibility impact,",
                "- adds or changes CLI commands together with broader workflow behavior,",
                "- changes status / next / AI-facing output together with broader workflow behavior,",
                "- requires README/docs/tests together across a broad change,",
                "- affects compatibility or safety-sensitive flows with broad impact.",
                "",
                "Roadmap flow:",
                "- Wait for human approval before creating a roadmap.",
                "- `nilo roadmap discuss` to generate discussion context without changing state.",
                "- Draft a RoadmapProposal from that context.",
                "- Import it with `nilo roadmap import` or accept immediately with `nilo roadmap adopt`.",
                "- human accepts pending revisions with `nilo roadmap accept`",
                "- `nilo roadmap task-plan`",
                "- then proceed task by task.",
                "",
                "Useful commands:",
                '- nilo work "<user request>"',
                "- nilo status --ai",
                "- nilo status --ai --verbose",
                "- nilo status --ai --json",
                "- nilo task status --task <task_id> --ai",
                "- nilo evidence show --task <task_id> --ai",
                "- nilo review status --task <task_id> --format json",
                "- nilo roadmap status --project <project_id> --ai",
                "- nilo failure list --project <project_id>",
                "- nilo doctor ai-context",
            ]
        )
    )


def cmd_migrate(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        files = legacy_agent_block_files()
        if not files:
            print("tracked agent instructions: no deprecated Nilo managed blocks found")
        else:
            print("tracked agent instructions: deprecated Nilo managed blocks found")
            for path in files:
                print(f"- {path.name}")
        if not args.apply:
            if files:
                print(
                    "Run `nilo migrate --apply` to remove these blocks and refresh local runtime files."
                )
            return

        project = project_for_current_directory(store)
        try:
            result = migrate_legacy_agent_blocks(project)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        for path in result.updated_paths:
            print(f"updated: {path}")
        for warning in result.warnings:
            print(f"warning: {warning}")
    finally:
        store.close()


def cmd_init(args: argparse.Namespace) -> None:
    project_id = Path.cwd().name
    boundary = resolve_project_boundary(
        create_missing=True, repair=bool(getattr(args, "repair_project_binding", False))
    )
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        if project:
            print(f"project exists: {project_id}")
        else:
            project = default_project_row(project_id, now_iso())
            store.insert("projects", project)
            print(f"created project: {project_id}")
        print(f"project binding: {boundary.binding_path}")
        install_agent_blocks(project, list(AGENT_TARGET_FILES))
        print_legacy_agent_block_guidance(legacy_agent_block_files())
    finally:
        store.close()
