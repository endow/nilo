from __future__ import annotations

from .store import Store


def parse_scores(values: list[str]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"score must be key=value: {value}")
        key, raw_score = value.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"score key must not be empty: {value}")
        try:
            score = int(raw_score)
        except ValueError:
            raise SystemExit(f"score must be an integer 1-5: {value}") from None
        if score < 1 or score > 5:
            raise SystemExit(f"score must be 1-5: {value}")
        scores[key] = score
    return scores


def validate_required_scores(scores: dict[str, int], required_scores: list[str], strict: bool) -> None:
    if not strict:
        return
    required = [score.strip() for score in required_scores if score.strip()]
    missing = sorted(set(required) - set(scores))
    if missing:
        raise SystemExit(f"missing required quality scores: {', '.join(missing)}")


def validate_known_scores(scores: dict[str, int], known_scores: list[str], allow_unknown: bool) -> None:
    if allow_unknown or not known_scores:
        return
    unknown = sorted(set(scores) - set(known_scores))
    if unknown:
        raise SystemExit(f"unknown quality scores: {', '.join(unknown)}")


def required_scores_for_task(store: Store, task: dict, extra_required_scores: list[str] | None = None) -> list[str]:
    schema = store.get("quality_score_schemas", task["project_id"])
    project_required_scores = schema["required_scores"] if schema else []
    return normalize_required_scores(project_required_scores + (extra_required_scores or []))


def normalize_required_scores(values: list[str]) -> list[str]:
    required_scores: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.strip()
        if not key:
            raise SystemExit("required score key must not be empty")
        if key in seen:
            continue
        required_scores.append(key)
        seen.add(key)
    return required_scores
