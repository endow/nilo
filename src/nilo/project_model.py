from __future__ import annotations

import argparse
from pathlib import Path

from .failure import deterministic_id
from .project_language import ensure_primary_language_rule, infer_primary_language_from_files


def project_row_from_args(args: argparse.Namespace, created_at: str) -> dict:
    rules = ensure_primary_language_rule(args.rule, infer_primary_language_from_files(Path.cwd()))
    return {
        "id": args.id or deterministic_id("project", [args.name, created_at]),
        "name": args.name,
        "tech_stack": args.tech_stack,
        "rules": rules,
        "default_completion_criteria": args.criterion,
        "available_models": args.model,
        "fallback_models": args.fallback_model,
        "requires_local_execution": args.requires_local_execution,
        "created_at": created_at,
    }


def default_project_row(project_id: str, created_at: str) -> dict:
    rules = [f"primary_language: {infer_primary_language_from_files(Path.cwd())}"]
    return {
        "id": project_id,
        "name": project_id,
        "tech_stack": [],
        "rules": rules,
        "default_completion_criteria": [],
        "available_models": [],
        "fallback_models": [],
        "requires_local_execution": False,
        "created_at": created_at,
    }
