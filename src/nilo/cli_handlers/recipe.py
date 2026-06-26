from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ..cli_support import make_id
from ..failure import deterministic_id
from ..recipe import RecipeSource, bump_patch, discover_recipes, recipe_to_json
from ..store import Store
from ..timeutil import now_iso
from ..version_advisor import advise_version_bump


def cmd_recipe_list(args: argparse.Namespace) -> None:
    data = discover_recipes(_project_root(args.project))
    if args.format == "json":
        print(recipe_to_json(data, include_all=args.all))
        return
    sources = data["all_sources"] if args.all else data["effective_recipes"]
    if not sources:
        print("recipes: none")
        return
    print("recipes:")
    for source in sources:
        suffix = f" [{source.status}]" if args.all and source.status != "valid" else ""
        stability = source.data.get("stability", "")
        stability_text = f" stability={stability}" if stability else ""
        print(f"- {source.name} ({source.layer}){suffix}: {source.title}{stability_text}")


def cmd_recipe_show(args: argparse.Namespace) -> None:
    data = discover_recipes(_project_root(args.project))
    source = _find_recipe(data["all_sources"] if args.source else data["effective_recipes"], args.name, args.source)
    if not source and not args.source:
        source = _find_recipe(data["all_sources"], args.name)
    if not source:
        raise SystemExit(f"recipe not found: {args.name}")
    if args.format == "json":
        print(json.dumps(source.to_dict(), ensure_ascii=False, indent=2))
        return
    _print_recipe(source)


def cmd_recipe_doctor(args: argparse.Namespace) -> None:
    data = discover_recipes(_project_root(args.project))
    has_errors = _has_errors(data["all_sources"])
    if args.format == "json":
        print(recipe_to_json(data, include_all=True))
        if has_errors:
            raise SystemExit(1)
        return
    sources = data["all_sources"]
    if not sources:
        print("recipe_doctor: no recipes discovered")
        return
    print("recipe_doctor:")
    for source in sources:
        print(f"- {source.name or '<unknown>'} ({source.layer}) {source.status}: {source.source_id}")
        for diagnostic in source.diagnostics:
            print(f"  - {diagnostic.severity}: {diagnostic.code}: {diagnostic.message}")
    if has_errors:
        raise SystemExit(1)


def cmd_recipe_run(args: argparse.Namespace) -> None:
    project = args.project or Path.cwd().name
    project_root = _project_root(project)
    data = discover_recipes(project_root)
    source = _find_recipe(data["effective_recipes"], args.name)
    if not source:
        raise SystemExit(f"recipe not found: {args.name}")
    rendered, variable_messages = _render_task_fields(source, project, args.var, args.title, project_root)
    for message in variable_messages:
        print(message)
    if args.dry_run:
        print("recipe_run: dry-run")
        _print_rendered_task(rendered)
        return
    task_id = _create_recipe_task(args, project, source, rendered)
    print(task_id)


def _has_errors(sources: list[RecipeSource]) -> bool:
    return any(diagnostic.severity == "error" for source in sources for diagnostic in source.diagnostics)


def _project_root(project: str) -> Path:
    if not project:
        return Path.cwd()
    candidate = Path(project)
    if candidate.exists():
        return candidate
    if project == Path.cwd().name:
        return Path.cwd()
    raise SystemExit(f"recipe project path not found: {project}")


def _find_recipe(sources: list[RecipeSource], name: str, layer: str = "") -> RecipeSource | None:
    for source in sources:
        if source.name == name and (not layer or source.layer == layer):
            return source
    return None


def _print_recipe(source: RecipeSource) -> None:
    data = source.data
    print(f"name: {source.name}")
    print(f"title: {source.title}")
    print(f"layer: {source.layer}")
    print(f"source: {source.source_id}")
    print(f"status: {source.status}")
    if data.get("summary"):
        print(f"summary: {data['summary']}")
    if data.get("instruction"):
        print("instruction:")
        print(data["instruction"].rstrip())
    if data.get("acceptance"):
        print("acceptance:")
        for item in data["acceptance"]:
            print(f"- {item}")
    for field in ["variables", "verification", "review", "completion_contract"]:
        if field in data:
            print(f"{field}:")
            print(_format_value(data[field]))
    if source.diagnostics:
        print("diagnostics:")
        for diagnostic in source.diagnostics:
            print(f"- {diagnostic.severity}: {diagnostic.code}: {diagnostic.message}")


def _format_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _render_task_fields(source: RecipeSource, project_id: str, raw_vars: list[str], title_override: str, project_root: Path) -> tuple[dict[str, Any], list[str]]:
    values, variable_messages = _recipe_variables(source, project_id, raw_vars, project_root)
    data = source.data
    title = _render_string(title_override or data["title"], values)
    instruction = _render_value(data["instruction"], values)
    acceptance = [_render_string(item, values) for item in data["acceptance"]]
    description_parts = [
        f"Recipe: {source.name}",
        f"Recipe source: {source.layer}:{source.source_id}",
        "",
        "Instruction:",
        str(instruction).rstrip(),
    ]
    for field, heading in [
        ("verification", "Verification requirements"),
        ("review", "Review requirements"),
        ("completion_contract", "Completion contract"),
    ]:
        if field in data:
            description_parts.extend(["", f"{heading}:", _format_value(_render_value(data[field], values))])
    return {"title": title, "description": "\n".join(description_parts).rstrip(), "acceptance": acceptance}, variable_messages


def _recipe_variables(source: RecipeSource, project_id: str, raw_vars: list[str], project_root: Path) -> tuple[dict[str, Any], list[str]]:
    variables = source.data.get("variables") or {}
    values: dict[str, Any] = {"project_id": project_id}
    messages: list[str] = []
    supplied: dict[str, str] = {}
    for raw in raw_vars:
        key, separator, value = raw.partition("=")
        if not separator or not key:
            raise SystemExit(f"invalid --var; expected key=value: {raw}")
        supplied[key] = value
    for name, spec in variables.items():
        if name in supplied:
            values[name] = _coerce_variable(name, supplied[name], spec)
        elif name in values:
            values[name] = _coerce_variable(name, str(values[name]), spec)
        elif source.name == "release" and name == "target_version":
            resolution = advise_version_bump(project_root)
            if resolution.get("resolved"):
                values[name] = _coerce_variable(name, str(resolution["recommended_version"]), spec)
                messages.extend(_release_target_version_resolved_messages(resolution))
            elif spec.get("required"):
                raise SystemExit(_release_target_version_unresolved_message(resolution))
        elif "default" in spec:
            values[name] = spec["default"]
        elif spec.get("required"):
            raise SystemExit(f"missing required recipe variable: {name}")
    for name, value in supplied.items():
        if name not in variables:
            values[name] = value
    return values, messages


def _release_target_version_resolved_messages(resolution: dict[str, Any]) -> list[str]:
    latest_tag = resolution.get("latest_tag") or "なし"
    messages = [
        "target_version が未指定です。",
        f"現在バージョン: {resolution.get('current_version', '')}",
        f"最新タグ: {latest_tag}",
        f"候補: patch {resolution.get('patch_candidate', '')}, minor {resolution.get('minor_candidate', '')}",
        f"推奨: {resolution.get('recommended_version', '')} ({resolution.get('recommended_bump_type', '')})",
        "理由:",
    ]
    for reason in resolution.get("reasons", []):
        messages.append(f"- {reason}")
    messages.extend([
        "この値を使って release レシピを続行します。",
    ])
    return messages


def _release_target_version_unresolved_message(resolution: dict[str, Any]) -> str:
    current = resolution.get("current_version") or "不明"
    latest = resolution.get("latest_tag") or "なし"
    example_base = current if current != "不明" else "0.1.9"
    patch_candidate = resolution.get("patch_candidate") or bump_patch(example_base) or "0.1.10"
    minor_candidate = resolution.get("minor_candidate") or ""
    recommended_version = resolution.get("recommended_version") or patch_candidate
    bump_type = resolution.get("recommended_bump_type") or "patch"
    lines = [
        "target_version が未指定です。",
        "target_version を自動採用できませんでした。",
        f"現在バージョン: {current}",
        f"最新タグ: {latest}",
        "",
    ]
    if minor_candidate:
        lines.extend(["候補:", f"- patch: {patch_candidate}", f"- minor: {minor_candidate}", ""])
    lines.extend([f"推奨: {recommended_version} ({bump_type})", "", "理由:"])
    reasons = resolution.get("reasons") or [resolution.get("reason", "unknown")]
    for reason in reasons:
        lines.append(f"- {reason}")
    warnings = resolution.get("warnings") or []
    if warnings:
        lines.extend(["", "警告:"])
        for warning in warnings:
            lines.append(f"- {warning}")
    lines.extend(
        [
            "",
            "推奨で進める場合:",
            f"nilo recipe run release --project nilo --var target_version={recommended_version}",
            "",
            "patch で進める場合:",
            f"nilo recipe run release --project nilo --var target_version={patch_candidate}",
        ]
    )
    return "\n".join(lines)

def _coerce_variable(name: str, value: str, spec: dict[str, Any]) -> Any:
    var_type = spec.get("type", "string")
    if var_type == "boolean":
        lowered = value.lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        raise SystemExit(f"invalid boolean value for recipe variable {name}: {value}")
    if var_type == "integer":
        try:
            return int(value)
        except ValueError as exc:
            raise SystemExit(f"invalid integer value for recipe variable {name}: {value}") from exc
    if var_type == "enum" and value not in spec.get("values", []):
        allowed = ", ".join(str(item) for item in spec.get("values", []))
        raise SystemExit(f"invalid enum value for recipe variable {name}: {value} (allowed: {allowed})")
    return value


def _render_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_string(value, variables)
    if isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, variables) for key, item in value.items()}
    return value


def _render_string(value: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise SystemExit(f"missing recipe variable: {name}")
        return str(variables[name])

    return re.sub(r"(?<![\${])\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})", replace, value)


def _print_rendered_task(rendered: dict[str, Any]) -> None:
    print(f"title: {rendered['title']}")
    print("description:")
    print(rendered["description"])
    print("acceptance:")
    for item in rendered["acceptance"]:
        print(f"- {item}")


def _create_recipe_task(args: argparse.Namespace, project: str, source: RecipeSource, rendered: dict[str, Any]) -> str:
    store = Store(args.db)
    try:
        if not store.get("projects", project):
            raise SystemExit(f"project not found: {project}")
        created_at = now_iso()
        task_id = deterministic_id("task", [project, rendered["title"], created_at])
        task_row = {
            "id": task_id,
            "project_id": project,
            "title": rendered["title"],
            "description": rendered["description"],
            "acceptance_criteria": rendered["acceptance"],
            "parent_task_id": None,
            "split_index": None,
            "task_type": args.task_type,
            "risk_level": args.risk,
            "requires_understanding_check": False,
            "roadmap_commitment_id": args.commitment or "",
            "roadmap_item_id": "",
            "status": "planned",
            "assigned_model_profile": "",
            "degradation_mode": "normal",
            "mode": "normal",
            "base_commit": None,
            "created_at": created_at,
        }
        provenance_row = {
                "id": make_id("recipe_provenance"),
                "task_id": task_id,
                "recipe_name": source.name,
                "source_layer": source.layer,
                "source_id": source.source_id,
                "content_hash": source.content_hash,
                "rendered_fields": rendered,
                "recipe_snapshot": source.to_dict(),
                "created_at": now_iso(),
        }
        with store.conn:
            _insert_row_without_commit(store, "tasks", task_row)
            _insert_row_without_commit(store, "recipe_task_provenance", provenance_row)
        return task_id
    finally:
        store.close()


def _insert_row_without_commit(store: Store, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    store.conn.execute(sql, [Store._encode(row[col]) for col in cols])
