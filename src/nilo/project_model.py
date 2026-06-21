from __future__ import annotations

import argparse

from .failure import deterministic_id


def project_row_from_args(args: argparse.Namespace, created_at: str) -> dict:
    return {
        "id": args.id or deterministic_id("project", [args.name, created_at]),
        "name": args.name,
        "tech_stack": args.tech_stack,
        "rules": args.rule,
        "default_completion_criteria": args.criterion,
        "available_models": args.model,
        "fallback_models": args.fallback_model,
        "requires_local_execution": args.requires_local_execution,
        "created_at": created_at,
    }


def default_project_row(project_id: str, created_at: str) -> dict:
    return {
        "id": project_id,
        "name": project_id,
        "tech_stack": [],
        "rules": [],
        "default_completion_criteria": [],
        "available_models": [],
        "fallback_models": [],
        "requires_local_execution": False,
        "created_at": created_at,
    }
