from __future__ import annotations

from .. import __version__
from .backup import cmd_backup, cmd_backups, cmd_backups_prune, cmd_restore
from .facade import (
    cmd_facade_check,
    cmd_facade_done,
    cmd_facade_next,
    cmd_facade_queue,
    cmd_facade_reject,
    cmd_facade_cancel,
    cmd_facade_report,
    cmd_facade_start,
    cmd_facade_status,
    cmd_facade_work,
)
from .failure import cmd_failure_ignore, cmd_failure_list, cmd_failure_resolve, cmd_failure_shadow_report, cmd_failure_show, cmd_failure_summary
from .doctor import (
    cmd_doctor,
    cmd_doctor_ai_context,
    cmd_doctor_completions,
    cmd_doctor_performance,
    cmd_doctor_state,
    cmd_doctor_transitions,
    cmd_doctor_workflow,
)
from .mcp import cmd_mcp_doctor, cmd_mcp_ping, cmd_mcp_reviewer_claim, cmd_mcp_reviewer_start, cmd_mcp_reviewer_worker, cmd_mcp_serve
from .overdrive import cmd_roadmap_execute, cmd_run
from .project import cmd_project_create, cmd_project_export_handson, cmd_project_export_recipes, cmd_project_import_recipes, cmd_project_status, cmd_project_summary
from .quality import (
    cmd_quality_autoscore_import,
    cmd_quality_autoscore_prepare,
    cmd_quality_quick,
    cmd_quality_schema_list,
    cmd_quality_schema_set,
    cmd_review_dispatch,
    cmd_review_doctor,
    cmd_review_claude,
    cmd_review_claude_doctor,
    cmd_review_human_launch_claude,
    cmd_review_init,
    cmd_review_delegate,
    cmd_review_import,
    cmd_review_finding_update,
    cmd_review_prepare,
    cmd_review_quick,
    cmd_review_request,
    cmd_review_run,
    cmd_review_status,
    cmd_review_template,
    cmd_review_wait,
    cmd_review_waive,
    cmd_review_withdraw,
)
from .recipe import cmd_recipe_approve_public, cmd_recipe_doctor, cmd_recipe_list, cmd_recipe_run, cmd_recipe_show
from .release import cmd_release_cancel, cmd_release_prepare, cmd_release_publish, cmd_release_resume, cmd_release_run
from .roadmap import (
    cmd_roadmap_accept,
    cmd_roadmap_adopt,
    cmd_roadmap_assess,
    cmd_roadmap_close,
    cmd_roadmap_discuss,
    cmd_roadmap_import,
    cmd_roadmap_reject,
    cmd_roadmap_status,
    cmd_roadmap_summary,
    cmd_roadmap_task_plan,
)
from .runtime import (
    cmd_agent_install,
    cmd_help_ai,
    cmd_init,
    cmd_migrate,
)
from .task import (
    cmd_evidence_show,
    cmd_review_show,
    cmd_task_analytics,
    cmd_task_complete,
    cmd_task_completion_invalidate,
    cmd_task_create,
    cmd_task_list,
    cmd_task_split,
    cmd_task_start,
    cmd_task_status,
    cmd_task_update,
)
from .test import cmd_test_plan, cmd_test_rerun_failed, cmd_test_run
from .todo import cmd_todo_add, cmd_todo_list, cmd_todo_promote, cmd_todo_show, cmd_todo_start, cmd_todo_triage
from .upgrade import cmd_upgrade
from .view import cmd_view
from .workflow import (
    cmd_instruct,
    cmd_outcome_record,
    cmd_report_import,
    cmd_report_validate,
    cmd_understanding_approve,
    cmd_understanding_import,
    cmd_understanding_prepare,
    cmd_update_check,
    cmd_verification_run,
)
from .workspace import cmd_workspace_add, cmd_workspace_list, cmd_workspace_remove, cmd_workspace_show


def nilo_version() -> str:
    return __version__
