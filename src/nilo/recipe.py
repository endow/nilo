from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RECIPE_VERSION = 1
RECIPE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
VARIABLE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
REQUIRED_FIELDS = {"schema_version", "name", "title", "summary", "instruction", "acceptance"}
KNOWN_FIELDS = REQUIRED_FIELDS | {
    "description",
    "variables",
    "verification",
    "review",
    "completion_contract",
    "tags",
    "owner",
    "stability",
}


@dataclass(frozen=True)
class RecipeDiagnostic:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class RecipeSource:
    name: str
    title: str
    layer: str
    source_id: str
    content_hash: str
    status: str
    diagnostics: tuple[RecipeDiagnostic, ...]
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "layer": self.layer,
            "source_id": self.source_id,
            "content_hash": self.content_hash,
            "status": self.status,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "data": self.data,
        }


def recipe_directories(project_root: Path | None = None, user_root: Path | None = None) -> list[tuple[str, Path]]:
    root = project_root or Path.cwd()
    home = user_root or Path.home()
    return [
        ("project", root / ".nilo" / "recipes"),
        ("user", home / ".nilo" / "recipes"),
        ("builtin", Path(__file__).parent / "builtin_recipes"),
    ]


def discover_recipes(project_root: Path | None = None, user_root: Path | None = None) -> dict[str, list[RecipeSource]]:
    sources: list[RecipeSource] = []
    for layer, directory in recipe_directories(project_root, user_root):
        sources.extend(_load_layer(layer, directory))

    duplicates: set[tuple[str, str]] = set()
    by_layer_name: dict[tuple[str, str], list[RecipeSource]] = {}
    for source in sources:
        if source.name:
            by_layer_name.setdefault((source.layer, source.name), []).append(source)
    for key, matches in by_layer_name.items():
        if len(matches) > 1:
            duplicates.add(key)

    marked: list[RecipeSource] = []
    for source in sources:
        if (source.layer, source.name) in duplicates:
            marked.append(_with_status(source, "duplicate", RecipeDiagnostic("error", "duplicate_name", f"duplicate recipe name in {source.layer} layer: {source.name}")))
        else:
            marked.append(source)

    effective: list[RecipeSource] = []
    selected_names: set[str] = set()
    final_sources: list[RecipeSource] = []
    for source in marked:
        if source.status in {"invalid", "duplicate"} or not source.name:
            final_sources.append(source)
            continue
        if source.name in selected_names:
            final_sources.append(_with_status(source, "shadowed", RecipeDiagnostic("info", "shadowed", f"recipe shadowed by higher precedence layer: {source.name}")))
            continue
        selected_names.add(source.name)
        effective.append(source)
        final_sources.append(source)

    return {"all_sources": final_sources, "effective_recipes": effective}


def recipe_to_json(data: dict[str, list[RecipeSource]], include_all: bool = False) -> str:
    key = "all_sources" if include_all else "effective_recipes"
    return json.dumps([source.to_dict() for source in data[key]], ensure_ascii=False, indent=2)


def _load_layer(layer: str, directory: Path) -> list[RecipeSource]:
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.recipe.yml"))
    return [_load_recipe_file(layer, path) for path in files if path.is_file()]


def _load_recipe_file(layer: str, path: Path) -> RecipeSource:
    source_id = str(path) if layer != "builtin" else path.name
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        return RecipeSource("", "", layer, source_id, "", "invalid", (RecipeDiagnostic("error", "unreadable", str(exc)),), {})
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    try:
        parsed = parse_recipe_yaml(body)
    except ValueError as exc:
        return RecipeSource("", "", layer, source_id, content_hash, "invalid", (RecipeDiagnostic("error", "parse_error", str(exc)),), {})
    if not isinstance(parsed, dict):
        return RecipeSource("", "", layer, source_id, content_hash, "invalid", (RecipeDiagnostic("error", "invalid_document", "recipe document must be a mapping"),), {})
    diagnostics = validate_recipe(parsed)
    status = "invalid" if any(item.severity == "error" for item in diagnostics) else "valid"
    name = str(parsed.get("name", "")) if isinstance(parsed.get("name", ""), str) else ""
    title = str(parsed.get("title", "")) if isinstance(parsed.get("title", ""), str) else ""
    return RecipeSource(name, title, layer, source_id, content_hash, status, tuple(diagnostics), parsed)


def _with_status(source: RecipeSource, status: str, diagnostic: RecipeDiagnostic) -> RecipeSource:
    return RecipeSource(
        source.name,
        source.title,
        source.layer,
        source.source_id,
        source.content_hash,
        status,
        (*source.diagnostics, diagnostic),
        source.data,
    )


def validate_recipe(data: dict[str, Any]) -> list[RecipeDiagnostic]:
    diagnostics: list[RecipeDiagnostic] = []
    for field in sorted(REQUIRED_FIELDS - set(data)):
        diagnostics.append(RecipeDiagnostic("error", "missing_required_field", f"missing required field: {field}"))
    for field in sorted(set(data) - KNOWN_FIELDS):
        diagnostics.append(RecipeDiagnostic("warning", "unknown_field", f"unknown top-level field: {field}"))

    if data.get("schema_version") != RECIPE_VERSION:
        diagnostics.append(RecipeDiagnostic("error", "unsupported_schema_version", "schema_version must be 1"))
    name = data.get("name")
    if not isinstance(name, str) or not RECIPE_NAME_PATTERN.match(name):
        diagnostics.append(RecipeDiagnostic("error", "invalid_name", "name must be lowercase kebab-case"))
    for field in ["title", "summary", "instruction"]:
        if field in data and not isinstance(data[field], str):
            diagnostics.append(RecipeDiagnostic("error", "invalid_field_type", f"{field} must be a string"))
    for field in ["description", "owner"]:
        if field in data and not isinstance(data[field], str):
            diagnostics.append(RecipeDiagnostic("error", "invalid_field_type", f"{field} must be a string"))
    acceptance = data.get("acceptance")
    if "acceptance" in data and (not isinstance(acceptance, list) or not acceptance or not all(isinstance(item, str) and item.strip() for item in acceptance)):
        diagnostics.append(RecipeDiagnostic("error", "invalid_acceptance", "acceptance must be a non-empty list of strings"))
    variables = data.get("variables")
    if variables is not None:
        diagnostics.extend(_validate_variables(variables))
    verification = data.get("verification")
    if verification is not None and not isinstance(verification, list):
        diagnostics.append(RecipeDiagnostic("error", "invalid_verification", "verification must be a list"))
    review = data.get("review")
    if review is not None and not isinstance(review, dict):
        diagnostics.append(RecipeDiagnostic("error", "invalid_review", "review must be a mapping"))
    completion_contract = data.get("completion_contract")
    if completion_contract is not None and not isinstance(completion_contract, dict):
        diagnostics.append(RecipeDiagnostic("error", "invalid_completion_contract", "completion_contract must be a mapping"))
    tags = data.get("tags")
    if tags is not None and (not isinstance(tags, list) or not all(isinstance(item, str) for item in tags)):
        diagnostics.append(RecipeDiagnostic("error", "invalid_tags", "tags must be a list of strings"))
    stability = data.get("stability")
    if stability is not None and stability not in {"experimental", "stable", "deprecated"}:
        diagnostics.append(RecipeDiagnostic("error", "invalid_stability", "stability must be experimental, stable, or deprecated"))
    return diagnostics


def _validate_variables(value: Any) -> list[RecipeDiagnostic]:
    diagnostics: list[RecipeDiagnostic] = []
    if not isinstance(value, dict):
        return [RecipeDiagnostic("error", "invalid_variables", "variables must be a mapping")]
    for name, spec in value.items():
        if not isinstance(name, str) or not VARIABLE_NAME_PATTERN.match(name):
            diagnostics.append(RecipeDiagnostic("error", "invalid_variable_name", f"invalid variable name: {name}"))
            continue
        if not isinstance(spec, dict):
            diagnostics.append(RecipeDiagnostic("error", "invalid_variable", f"variable must be a mapping: {name}"))
            continue
        var_type = spec.get("type")
        if var_type not in {"string", "boolean", "integer", "enum"}:
            diagnostics.append(RecipeDiagnostic("error", "invalid_variable_type", f"unsupported variable type for {name}: {var_type}"))
        if var_type == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values:
                diagnostics.append(RecipeDiagnostic("error", "invalid_enum_values", f"enum variable needs non-empty values: {name}"))
        if "default" in spec and var_type in {"string", "boolean", "integer"} and not _matches_type(spec["default"], var_type):
            diagnostics.append(RecipeDiagnostic("error", "invalid_default", f"default does not match type for {name}"))
    return diagnostics


def _matches_type(value: Any, var_type: str) -> bool:
    if var_type == "string":
        return isinstance(value, str)
    if var_type == "boolean":
        return isinstance(value, bool)
    if var_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return True


def parse_recipe_yaml(markdown: str) -> Any:
    lines = _yaml_lines(markdown)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError(f"unexpected content near line {lines[index][2]}")
    return value


def _yaml_lines(text: str) -> list[tuple[int, str, int]]:
    result: list[tuple[int, str, int]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        result.append((indent, raw.strip(), line_no))
    return result


def _parse_block(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][0] != indent:
        raise ValueError(f"invalid indentation near line {lines[index][2]}")
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_dict(lines, index, indent)


def _parse_dict(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    data: dict[str, Any] = {}
    while index < len(lines):
        current_indent, content, line_no = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"unexpected indentation near line {line_no}")
        if content.startswith("- "):
            break
        key, separator, rest = content.partition(":")
        if not separator or not key.strip():
            raise ValueError(f"expected key/value near line {line_no}")
        key = key.strip()
        rest = rest.strip()
        if rest == "|":
            value, index = _parse_block_scalar(lines, index + 1, indent)
        elif rest:
            value = _parse_scalar(rest)
            index += 1
        else:
            if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                value = {}
                index += 1
            else:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
        data[key] = value
    return data, index


def _parse_list(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        current_indent, content, line_no = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"unexpected indentation near line {line_no}")
        if not content.startswith("- "):
            break
        rest = content[2:].strip()
        if not rest:
            if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                items.append("")
                index += 1
            else:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
                items.append(value)
        elif ":" in rest and not rest.startswith(("\"", "'")):
            key, _, value = rest.partition(":")
            mapping: dict[str, Any] = {key.strip(): _parse_scalar(value.strip()) if value.strip() else {}}
            index += 1
            if index < len(lines) and lines[index][0] > indent and not lines[index][1].startswith("- "):
                nested, index = _parse_dict(lines, index, lines[index][0])
                mapping.update(nested)
            items.append(mapping)
        else:
            items.append(_parse_scalar(rest))
            index += 1
    return items, index


def _parse_block_scalar(lines: list[tuple[int, str, int]], index: int, parent_indent: int) -> tuple[str, int]:
    values: list[str] = []
    scalar_indent: int | None = None
    while index < len(lines):
        indent, content, _line_no = lines[index]
        if indent <= parent_indent:
            break
        if scalar_indent is None:
            scalar_indent = indent
        values.append(" " * max(indent - scalar_indent, 0) + content)
        index += 1
    return "\n".join(values).rstrip() + ("\n" if values else ""), index


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if re.match(r"^-?\d+$", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item.strip()) for item in inner.split(",")]
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value
