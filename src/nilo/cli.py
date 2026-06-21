from __future__ import annotations

import argparse
import json
import locale
import subprocess
import sys
from pathlib import Path

from .cli_support import cli_quote, make_id, read_text_or_exit
from .design_residue import parse_design_residue
from .failure import deterministic_id
from .gitmeta import git_output, head_commit
from .guard import evaluate_evidence
from .instruction import build_autoscore_prompt, build_instruction, build_review_prompt, build_rules_derive_prompt, build_understanding_prompt
from .project_model import default_project_row, project_row_from_args
from .quality import parse_quality_review
from .quality_logic import (
    normalize_required_scores,
    parse_scores,
    required_scores_for_task,
    validate_known_scores,
    validate_required_scores,
)
from .report import claimed_status, extract_changed_files
from .reviewer_registry import CLAUDE_CODE_REGISTER_REVIEWER_JSON, CODEX_REGISTER_REVIEWER_JSON
from .roadmap_render import (
    render_roadmap_assess_markdown,
    render_roadmap_discuss_markdown,
    render_roadmap_task_plan_markdown,
    roadmap_revision_source_label,
    task_create_command,
    task_plan_candidates,
)
from .secret import mask_secrets
from .store import Store
from .task_logic import completion_status, is_task_completed_status, outcome_status, projected_task_status, require_ai_completion_evidence, split_task_specs
from .timeutil import now_iso
from .verification import run_local_verification


AGENT_TARGET_FILES = {
    "codex": "AGENTS.md",
    "claude-code": "CLAUDE.md",
}
NILO_BLOCK_BEGIN = "<!-- BEGIN NILO MANAGED BLOCK -->"
NILO_BLOCK_END = "<!-- END NILO MANAGED BLOCK -->"
CLAUDE_CODE_REVIEWER_PROTOCOL_HEADING = "## Nilo MCP Reviewer Protocol"


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path. Defaults to .nilo/nilo.db")


def requires_understanding_gate(task: dict) -> bool:
    if task["task_type"] in ("research", "design", "review", "verification", "documentation"):
        return False
    return bool(task["requires_understanding_check"]) or task["risk_level"] == "high"


def understanding_approved(store: Store, task_id: str) -> bool:
    latest = store.latest_for_task("understanding_checks", task_id)
    return bool(latest and latest["status"] == "approved_to_implement")


def prompt_quality_review(args: argparse.Namespace) -> tuple[str, list[str], list[str]]:
    print("quality_summary:")
    summary = input("> ").strip()
    issues: list[str] = []
    print("quality_issues: enter one issue per line; submit an empty line to finish")
    while True:
        issue = input("> ").strip()
        if not issue:
            break
        issues.append(issue)
    scores = list(args.score or [])
    print("quality_scores: enter key=value; submit an empty line to finish")
    while True:
        score = input("> ").strip()
        if not score:
            break
        scores.append(score)
    return summary, issues, scores


def print_quality_review(review: dict, prefix: str = "") -> None:
    print(f"{prefix}latest_quality_review: {review['id']} ({review['reviewer']})")
    print(f"{prefix}quality_summary: {review['summary']}")
    if review["issues"]:
        print(f"{prefix}quality_issues:")
        for issue in review["issues"]:
            print(f"{prefix}- {issue}")
    if review["scores"]:
        print(f"{prefix}quality_scores:")
        for key, score in review["scores"].items():
            print(f"{prefix}- {key}: {score}")




























































def git_commit_log(cwd: Path, base_commit: str, latest_head: str) -> list[dict]:
    if base_commit == latest_head:
        return []
    completed = subprocess.run(
        ["git", "log", "--format=%H%x00%s", f"{base_commit}..{latest_head}"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return []
    commits = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        commit_hash, _, subject = line.partition("\x00")
        commits.append({"hash": commit_hash, "subject": subject})
    return commits


def git_changed_files(cwd: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return []
    files: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path.replace("\\", "/"))
    return sorted(set(files))


def commit_changed_files(cwd: Path, files: list[str], message: str) -> tuple[int, str, str]:
    add_code, _, add_err = git_output(["add", "--", *files], cwd)
    if add_code != 0:
        return add_code, "", add_err
    return git_output(["commit", "-m", message], cwd)
























def markdown_title(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("##"):
            return stripped[2:].strip()
    return ""


def markdown_sections(markdown: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            current = stripped.lstrip("#").strip().lower()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def section_text(sections: dict[str, list[str]], names: tuple[str, ...]) -> str:
    for name in names:
        lines = sections.get(name.lower())
        if lines is not None:
            text = "\n".join(line.strip() for line in lines).strip()
            if text:
                return text
    return ""


def section_bullets(sections: dict[str, list[str]], names: tuple[str, ...]) -> list[str]:
    for name in names:
        lines = sections.get(name.lower())
        if lines is None:
            continue
        bullets = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                bullets.append(stripped[2:].strip())
        if bullets:
            return bullets
    return []


def parse_roadmap_proposal(markdown: str) -> dict:
    sections = markdown_sections(markdown)
    return {
        "title": markdown_title(markdown),
        "intent": section_text(sections, ("Intent", "目的", "Rationale", "理由")),
        "success_criteria": section_bullets(sections, ("Success Criteria", "成功条件", "Acceptance Criteria")),
        "non_goals": section_bullets(sections, ("Non Goals", "Non-goals", "非目的")),
        "autonomy_scope": section_bullets(sections, ("Autonomy Scope", "AI に任せてよい範囲", "Scope")),
        "review_gates": section_bullets(sections, ("Review Gates", "人間レビューが必要", "人間レビューが必要な境界")),
        "evidence_policy": section_bullets(sections, ("Evidence Policy", "証跡ポリシー")),
    }


def build_claude_code_reviewer_protocol() -> str:
    register_json = json.dumps(CLAUDE_CODE_REGISTER_REVIEWER_JSON, ensure_ascii=False, indent=2)
    return f"""{CLAUDE_CODE_REVIEWER_PROTOCOL_HEADING}

When acting as the `claude-code` reviewer through Nilo MCP, always refresh reviewer availability before claiming any review.

Before calling `claim_next_review`, call `register_reviewer` with:

```json
{register_json}
```

Then call `claim_next_review`.

Do not use `reviewer-start` for Claude Code reviews. It is heartbeat-only and not a real reviewer worker.

Do not use `reviewer-worker --result-file` as a substitute for Claude Code review.

Do not create or import results under the `claude-code` reviewer name unless this Claude Code session actually claimed the review through Nilo MCP.

The required flow is:

1. `register_reviewer`
2. `claim_next_review`
3. generate a real review response
4. `import_review_result`

A connected Nilo MCP server does not by itself make the reviewer available. The reviewer becomes available only after this Claude Code session calls `register_reviewer` with dispatch-capable metadata.
"""


def build_codex_reviewer_protocol() -> str:
    register_json = json.dumps(CODEX_REGISTER_REVIEWER_JSON, ensure_ascii=False, indent=2)
    return f"""{CLAUDE_CODE_REVIEWER_PROTOCOL_HEADING}

When acting as the `codex` reviewer through Nilo MCP, always refresh reviewer availability before claiming any review.

Before calling `claim_next_review`, call `register_reviewer` with:

```json
{register_json}
```

Then call `claim_next_review`.

Do not use `reviewer-start` for Codex reviews. It is heartbeat-only and not a real reviewer worker.

Do not use `reviewer-worker --result-file` as a substitute for Codex review.

Do not create or import results under the `codex` reviewer name unless this Codex session actually claimed the review through Nilo MCP.

The required flow is:

1. `register_reviewer`
2. `claim_next_review`
3. generate a real review response
4. `import_review_result`

A connected Nilo MCP server does not by itself make the reviewer available. The reviewer becomes available only after this Codex session calls `register_reviewer` with dispatch-capable metadata.
"""


def build_agent_instruction_block(project: dict, target: str = "codex") -> str:
    project_id = project["id"]
    reviewer_protocol = ""
    if target == "codex":
        reviewer_protocol = "\n" + build_codex_reviewer_protocol() + "\n"
    elif target == "claude-code":
        reviewer_protocol = "\n" + build_claude_code_reviewer_protocol() + "\n"
    return f"""{NILO_BLOCK_BEGIN}
## Nilo 必須プロトコル

このプロジェクトでは Nilo を AI 作業の状態管理装置として使う。日常運用の表の入口（daily surface）は `nilo init` / `nilo start` / `nilo status` / `nilo next` / `nilo check` / `nilo report` / `nilo done` / `nilo reject` に寄せ、roadmap / review / quality / rules / MCP などは必要時に使う裏側の機能として扱う。
{reviewer_protocol}
## 必須手順

1. 現在地を確認する。Nilo MCP が利用可能な場合は、作業開始前に `get_agent_work_context(project_id="{project_id}")` / `get_next_step(project_id="{project_id}")` で現在地と次の許可 action を確認する。Nilo MCP が設定済みでも callable tool として見えない場合は、まず tool discovery / `tool_search` で Nilo MCP の lazy loading を試す。それでも Nilo MCP tool が露出しない、または起動に失敗している場合は、「Nilo MCP が current session にロードされていない」と報告したうえで `nilo status --project {project_id}` と `nilo next --project {project_id}` を CLI fallback として実行し、先頭の next action だけに従う。
2. 明確なユーザー依頼があり active task が無い場合は、ユーザーに Nilo 操作を依頼せず、`nilo start --project {project_id} ...` で依頼内容に対応する task を作成してから進める。task 作成は裏側の作業として扱い、ユーザー向け説明は依頼された変更内容を中心にする。
3. active task に着手する前に必ず `nilo instruct --task <task_id>` を実行し、指示・完了条件・禁止事項に従う。
4. `next` / `next_actions` が複数ある場合は先頭の next action だけを実行する。迷ったらコマンドを推測せず、`status` / `next` を再確認し、先頭の next action だけを実行して、再度 status を報告して停止する。
5. 検証結果と報告を Nilo に戻す。通常は `nilo check --task <task_id> "<command>"` と `nilo report --task <task_id> --file .nilo/reports/<task_id>.md` を使い、必要な場合だけ `nilo report import` 相当の裏側の取り込みや MCP の `submit_agent_report` / `record_test_result` を使う。
6. 完了・commit・force・roadmap close は human gate として扱い、人間の明示指示なしに進めない。

## AI 間依頼プロトコル

- AI エージェント間の作業依頼・レビュー依頼は Nilo MCP 経由だけにする。相手エージェントのローカル CLI やプロセス起動コマンドは直接実行しない。
- 「Claudeにレビューして」「Codexにレビューして」という通常の AI 間レビュー依頼では、依頼する側は `request_task_review` を直接使わず、高レベル API の `dispatch_review` / `run_agent_review` / `request_and_run_review` のいずれかを使う。
- `request_task_review` は低レベル API として残すが、review request 作成だけで reviewer process 起動、claim、review 実行、`import_review_result`、final status 確認までは行わないため、通常の AI 間レビュー依頼では使わない。
- AI 間依頼に必要な MCP tool が callable tool として見えない場合は、代替 CLI に逃げず、「Nilo MCP が current session にロードされていない」と報告して停止する。

## 禁止事項

- 対応タスクなしに勝手に実装へ進まない。明確なユーザー依頼がある場合は先に task を作成してから進める
- 検証していない成果を検証済みまたは完了として報告しない
- ユーザーの明示指示なしに `nilo task complete` や `nilo roadmap close` を実行しない
- ユーザーの明示許可なしに `--commit` を使わない
- ユーザーの明示許可なしに `--force` や人間承認を代替する操作を使わない
{NILO_BLOCK_END}
"""


def remove_markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    removed = False
    while index < len(lines):
        if lines[index].strip() == heading:
            removed = True
            index += 1
            while index < len(lines) and not lines[index].startswith("## "):
                index += 1
            while output and not output[-1].strip():
                output.pop()
            if output:
                output.append("\n")
            continue
        output.append(lines[index])
        index += 1
    if not removed:
        return text
    return "".join(output).rstrip() + ("\n" if output else "")


def upsert_nilo_managed_block(text: str, block: str) -> str:
    begin = text.find(NILO_BLOCK_BEGIN)
    end = text.find(NILO_BLOCK_END)
    if begin == -1 and end == -1:
        separator = "" if not text.strip() else "\n\n"
        return text.rstrip() + separator + block
    if begin == -1 or end == -1 or end < begin:
        raise SystemExit("malformed Nilo managed block")
    end += len(NILO_BLOCK_END)
    return text[:begin].rstrip() + "\n\n" + block + text[end:].lstrip()




















































from .project_logic import (
    accepted_roadmap_commitments,
    closed_roadmap_commitments,
    command_covers_expected_test,
    command_runs_broad_test_suite,
    diff_aware_verification_summary,
    expected_test_paths_for_file,
    handson_language,
    handson_text,
    human_roadmap_summary,
    human_roadmap_path_for_project,
    next_actions_for_task,
    normalize_command_path,
    pending_roadmap_revisions,
    print_human_project_status,
    print_project_summary_text,
    print_roadmap_agent_next_actions,
    print_roadmap_agent_state,
    project_commit_mapping,
    project_current_phase,
    project_design_residue,
    project_level_next_actions,
    project_roadmap_position,
    project_summary_data,
    project_tasks_and_statuses,
    project_work_state,
    recent_project_history,
    related_tasks_for_commitment,
    render_handson_active_task_next_steps,
    render_handson_markdown,
    render_handson_next_action,
    render_handson_roadmap_position,
    roadmap_agent_next_actions,
    roadmap_agent_state,
    roadmap_assessments,
    roadmap_commitment_assessment,
    roadmap_proposal_path_for_commitment,
    roadmap_task_evidence,
    task_status_counts,
    unexecuted_verifications_for_task,
    unresolved_blocking_review_findings,
    verification_summary,
    verification_working_tree_state,
    verification_working_tree_summary,
    write_handson_markdown,
)
from .cli_handlers.knowledge import cmd_rules_list, cmd_success_list
from .cli_handlers.mcp import cmd_mcp_doctor, cmd_mcp_ping, cmd_mcp_reviewer_claim, cmd_mcp_reviewer_start, cmd_mcp_reviewer_worker, cmd_mcp_serve
from .cli_handlers.overdrive import cmd_roadmap_execute, cmd_run
from .cli_handlers.project import cmd_project_create, cmd_project_export_handson, cmd_project_status, cmd_project_summary
from .cli_handlers.facade import (
    cmd_facade_check,
    cmd_facade_done,
    cmd_facade_next,
    cmd_facade_reject,
    cmd_facade_report,
    cmd_facade_start,
    cmd_facade_status,
)
from .cli_handlers.quality import (
    cmd_quality_autoscore_import,
    cmd_quality_autoscore_prepare,
    cmd_quality_quick,
    cmd_quality_schema_list,
    cmd_quality_schema_set,
    cmd_review_dispatch,
    cmd_review_doctor,
    cmd_review_human_launch_claude,
    cmd_review_init,
    cmd_review_delegate,
    cmd_review_import,
    cmd_review_finding_update,
    cmd_review_prepare,
    cmd_review_quick,
    cmd_review_request,
    cmd_review_status,
    cmd_review_template,
    cmd_review_wait,
    cmd_review_withdraw,
)
from .cli_handlers.roadmap import (
    cmd_roadmap_accept,
    cmd_roadmap_adopt,
    cmd_roadmap_assess,
    cmd_roadmap_close,
    cmd_roadmap_discuss,
    cmd_roadmap_export,
    cmd_roadmap_import,
    cmd_roadmap_reject,
    cmd_roadmap_status,
    cmd_roadmap_summary,
    cmd_roadmap_task_plan,
)
from .cli_handlers.task import cmd_task_complete, cmd_task_create, cmd_task_list, cmd_task_split, cmd_task_start, cmd_task_status, cmd_task_update
from .cli_handlers.todo import cmd_todo_add, cmd_todo_list, cmd_todo_promote, cmd_todo_show, cmd_todo_start, cmd_todo_triage
from .cli_handlers.workflow import cmd_agent_install, cmd_init, cmd_instruct, cmd_outcome_record, cmd_report_import, cmd_understanding_approve, cmd_understanding_import, cmd_understanding_prepare, cmd_verification_run


def build_parser() -> argparse.ArgumentParser:
    from .cli_parsers import build_parser as build_cli_parser

    return build_cli_parser(add_common, sys.modules[__name__])


TOP_LEVEL_COMMANDS = {
    "agent",
    "check",
    "done",
    "init",
    "instruct",
    "mcp",
    "next",
    "outcome",
    "project",
    "quality",
    "reject",
    "report",
    "review",
    "roadmap",
    "rules",
    "run",
    "start",
    "status",
    "success",
    "task",
    "todo",
    "understanding",
    "verification",
}


def route_natural_language_intent(argv: list[str]) -> bool:
    remaining = list(argv)
    db_path: Path | None = None
    if len(remaining) >= 2 and remaining[0] == "--db":
        db_path = Path(remaining[1])
        remaining = remaining[2:]
    if not remaining or remaining[0] in TOP_LEVEL_COMMANDS or remaining[0].startswith("-"):
        return False

    utterance = " ".join(remaining).strip()
    normalized = utterance.casefold()
    wants_claude_code = "claude" in normalized or "cluade" in normalized
    wants_codex = "codex" in normalized
    wants_review = "レビュー" in utterance or "review" in normalized
    if not ((wants_claude_code or wants_codex) and wants_review):
        return False

    reviewer = "claude-code" if wants_claude_code else "codex"
    wants_lightweight = any(marker in normalized for marker in ["quick", "light", "軽く", "軽い", "軽量", "さっと", "簡単"])
    wants_formal = any(marker in normalized for marker in ["dispatch", "formal", "gate", "completion", "正式", "完了前"])
    if wants_lightweight and not wants_formal:
        cmd_review_quick(
            argparse.Namespace(
                db=db_path,
                project=Path.cwd().name,
                task=None,
                actor="codex",
                reviewer=reviewer,
                reason=utterance,
                should_import=True,
                timeout=120.0,
                auto_configure=True,
                config=None,
            )
        )
        return True

    cmd_review_dispatch(
        argparse.Namespace(
            db=db_path,
            project=Path.cwd().name,
            task=None,
            actor="codex",
            reviewer=reviewer,
            reason=utterance,
            auto_start=True,
            auto_configure=True,
            config=None,
        )
    )
    return True


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if route_natural_language_intent(raw_argv):
        return
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    args.func(args)
