from __future__ import annotations

import json
import re
from pathlib import Path

from .gitmeta import git_output
from .report import parse_sections, section_value
from .snapshot import current_git_snapshot, evidence_status


VALID_FINDING_STATUSES = {"unresolved", "addressed", "accepted-risk"}
VALID_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
NO_FINDINGS_SENTINELS = {
    "no findings",
    "no finding",
    "none",
    "なし",
    "指摘なし",
}


def parse_review_result(markdown: str) -> tuple[str, str, list[dict]]:
    markdown = extract_review_result_body(markdown)
    json_result = parse_jsonish_review_result(markdown)
    if json_result:
        return json_result
    sections = parse_sections(markdown)
    verdict = (
        section_value(sections, ["Verdict", "判定"])
        or parse_labeled_value(markdown, ["verdict", "判定"])
        or "commented"
    )
    verdict = normalize_verdict(verdict)
    summary = section_value(sections, ["Summary", "概要"]) or parse_labeled_value(markdown, ["summary", "概要"])
    findings_body = review_section(markdown, ["Findings", "指摘"])
    findings = parse_findings(findings_body)
    if not summary:
        summary = markdown.strip()
    return verdict, summary.strip(), findings


def extract_review_result_body(markdown: str) -> str:
    fenced = list(re.finditer(r"```(?:markdown|md|json)?\s*\n(.*?)\n```", markdown, flags=re.IGNORECASE | re.DOTALL))
    for match in fenced:
        body = match.group(1).strip()
        if looks_like_review_result(body):
            return body
    marker = re.search(r"^#\s*ReviewResult\s*$", markdown, flags=re.IGNORECASE | re.MULTILINE)
    if marker:
        return markdown[marker.start() :].strip()
    return markdown


def looks_like_review_result(markdown: str) -> bool:
    text = markdown.strip()
    if not text:
        return False
    if re.search(r"^#\s*ReviewResult\s*$", text, flags=re.IGNORECASE | re.MULTILINE):
        return True
    if re.search(r"^\s*(verdict|summary|findings)\s*[:：]", text, flags=re.IGNORECASE | re.MULTILINE):
        return True
    if text.startswith("{"):
        return parse_jsonish_review_result(text) is not None
    return False


def parse_jsonish_review_result(markdown: str) -> tuple[str, str, list[dict]] | None:
    text = markdown.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not any(key in data for key in {"verdict", "summary", "findings", "body"}):
        return None
    verdict = normalize_verdict(str(data.get("verdict") or "commented"))
    summary = str(data.get("summary") or data.get("body") or "").strip()
    raw_findings = data.get("findings") or []
    findings: list[dict] = []
    if isinstance(raw_findings, list):
        for index, raw in enumerate(raw_findings, start=1):
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "unresolved").lower().replace("_", "-")
            if status not in VALID_FINDING_STATUSES:
                status = "unresolved"
            severity = str(raw.get("severity") or "medium").lower()
            if severity not in VALID_FINDING_SEVERITIES:
                severity = "medium"
            blocking = raw.get("blocking")
            findings.append(
                {
                    "title": str(raw.get("title") or f"Finding {index}"),
                    "severity": severity,
                    "status": status,
                    "file_path": str(raw.get("file_path") or raw.get("file") or raw.get("path") or ""),
                    "line": str(raw.get("line") or ""),
                    "blocking": bool(blocking) if blocking is not None else status == "unresolved" and severity in {"high", "critical"},
                    "description": str(raw.get("description") or raw.get("body") or "").strip(),
                }
            )
    elif isinstance(raw_findings, str):
        findings = parse_findings(raw_findings)
    return verdict, summary or text, findings


def review_section(markdown: str, candidates: list[str]) -> str:
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    for index, heading in enumerate(headings):
        title = heading.group(1).strip()
        if not any(candidate.lower() in title.lower() for candidate in candidates):
            continue
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        return markdown[start:end].strip()
    return ""


def normalize_verdict(value: str) -> str:
    normalized = value.strip().splitlines()[0].strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "approved": "approved",
        "approve": "approved",
        "ok": "approved",
        "changes_requested": "changes_requested",
        "change_requested": "changes_requested",
        "request_changes": "changes_requested",
        "rejected": "rejected",
        "reject": "rejected",
        "commented": "commented",
        "needs_review": "commented",
    }
    return aliases.get(normalized, normalized or "commented")


def parse_labeled_value(markdown: str, labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else ""


def parse_findings(text: str) -> list[dict]:
    normalized = normalize_no_findings_text(text)
    if not normalized or normalized in NO_FINDINGS_SENTINELS:
        return []
    chunks = re.split(r"(?m)^###\s+", text)
    findings: list[dict] = []
    for index, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        title = lines[0].strip() if chunks[0].strip() or index > 0 else f"F{index + 1}"
        body_lines = lines[1:] if title else lines
        labels, description = parse_finding_labels(body_lines)
        status = labels.get("status", "unresolved").lower().replace("_", "-")
        if status not in VALID_FINDING_STATUSES:
            status = "unresolved"
        severity = labels.get("severity", "medium").lower()
        if severity not in VALID_FINDING_SEVERITIES:
            severity = "medium"
        if "blocking" in labels:
            blocking = parse_bool(labels["blocking"])
        else:
            blocking = status == "unresolved" and severity in {"high", "critical"}
        findings.append(
            {
                "title": title or labels.get("title", f"Finding {len(findings) + 1}"),
                "severity": severity,
                "status": status,
                "file_path": labels.get("file", labels.get("path", "")),
                "line": labels.get("line", ""),
                "blocking": blocking,
                "description": description.strip(),
            }
        )
    return findings


def normalize_no_findings_text(text: str) -> str:
    return re.sub(r"[\s。．.]+", " ", text.strip().lower()).strip()


def parse_finding_labels(lines: list[str]) -> tuple[dict[str, str], str]:
    labels: dict[str, str] = {}
    description_lines: list[str] = []
    label_re = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z_][A-Za-z0-9_-]*)\s*[:：]\s*(.*?)\s*$")
    for line in lines:
        match = label_re.match(line)
        if match and match.group(1).lower() in {"severity", "status", "file", "path", "line", "blocking", "title"}:
            labels[match.group(1).lower()] = match.group(2).strip()
        else:
            description_lines.append(line)
    return labels, "\n".join(description_lines)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "blocking", "blocker"}


def build_review_context(
    task: dict,
    request: dict,
    report: dict | None,
    evidence_check: dict | None,
    verification_run: dict | None,
    cwd: Path,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", [])) or "- 未設定"
    report_body = report["body_md"] if report else "未提出"
    computed_evidence_status = evidence_status(verification_run, current_git_snapshot(cwd))
    if verification_run:
        verification = (
            f"command: {verification_run['command']}\n"
            f"exit_code: {verification_run['exit_code']}\n"
            f"timed_out: {bool(verification_run['timed_out'])}\n"
            f"stdout:\n{verification_run['stdout']}\n"
            f"stderr:\n{verification_run['stderr']}"
        )
    else:
        verification = "未実行"
    diff = review_diff(task, verification_run, cwd)
    untracked = untracked_file_preview(cwd)
    return f"""# Review Request

## Request
- id: {request["id"]}
- from: {request["requester"]}
- to: {request["reviewer"]}
- status: {request["status"]}
- reason: {request["reason"]}

## Task
- id: {task["id"]}
- title: {task["title"]}
- type: {task["task_type"]}
- risk: {task["risk_level"]}

## Description
{task.get("description") or "未設定"}

## Acceptance Criteria
{acceptance}

## Implementation Report
{report_body}

## EvidenceStatus
status: {computed_evidence_status}

## Verification History
{verification}

## Git Diff
```diff
{diff}
```

## Untracked File Preview
{untracked}

## Review Guidance
- コード変更は禁止
- 実装者の自己申告ではなく、task、acceptance criteria、diff、verification を主情報として判断する
- AI review は verification ではないため、未検証事項は未検証として指摘する
- blocking な問題は severity: high または blocking: true として出す

## Output Format
# ReviewResult

## Verdict
approved | changes_requested | commented

## Summary

## Findings
### F1
severity: high | medium | low
status: unresolved | addressed | accepted-risk
file: path/to/file
line: 123
blocking: true | false

Description of the finding.
"""


def build_review_result_template(request: dict) -> str:
    return f"""# ReviewResult

review_id: {request["id"]}
task_id: {request["task_id"]}

## Verdict
commented

## Summary

Write a concise review summary.

## Findings
### F1
severity: medium
status: unresolved
file:
line:
blocking: false

Describe the finding. Delete this sample finding if there are no findings.

## Import Command
```bash
nilo review import --task {request["task_id"]} --review {request["id"]} --file .nilo/reviews/{request["id"]}.md
```
"""


def current_diff(cwd: Path) -> str:
    parts: list[str] = []
    for args in (["diff", "--no-ext-diff"], ["diff", "--cached", "--no-ext-diff"]):
        code, out, err = git_output(args, cwd)
        if code == 0 and out.strip():
            parts.append(out)
        elif code != 0 and err.strip():
            parts.append(f"# git {' '.join(args)} failed: {err}")
    return "\n\n".join(parts) or "# no working tree diff"


def parse_untracked_status(stdout: str) -> list[str]:
    files: list[str] = []
    for entry in [item for item in stdout.split("\0") if item]:
        if entry.startswith("?? "):
            files.append(entry[3:].strip().replace("\\", "/"))
    return sorted(set(path for path in files if path))


def untracked_file_preview(cwd: Path, max_bytes: int = 20000) -> str:
    code, out, err = git_output(["-c", "core.quotepath=false", "status", "--porcelain", "-z"], cwd)
    if code != 0:
        return f"- untracked files unavailable: {err.strip() or 'git status failed'}"
    files = parse_untracked_status(out)
    if not files:
        return "- none"
    sections: list[str] = []
    for path in files:
        full_path = cwd / path
        try:
            if not full_path.is_file():
                sections.append(f"### {path}\n- omitted: not a regular file")
                continue
            size = full_path.stat().st_size
            if size > max_bytes:
                sections.append(f"### {path}\n- omitted: file is {size} bytes, larger than {max_bytes} byte preview limit")
                continue
            data = full_path.read_bytes()
            if b"\0" in data:
                sections.append(f"### {path}\n- omitted: binary-looking file")
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            sections.append(f"### {path}\n```text\n{text}\n```")
        except OSError as exc:
            sections.append(f"### {path}\n- omitted: {exc}")
    return "\n\n".join(sections)


def review_diff(task: dict, verification_run: dict | None, cwd: Path) -> str:
    diff = current_diff(cwd)
    if diff != "# no working tree diff":
        return diff
    base_commit = task.get("base_commit")
    git_head = verification_run.get("git_head") if verification_run else None
    if not base_commit or not git_head or base_commit == git_head:
        return diff
    code, out, err = git_output(["diff", "--no-ext-diff", f"{base_commit}..{git_head}"], cwd)
    if code == 0 and out.strip():
        return out
    if code != 0 and err.strip():
        return f"# git diff {base_commit}..{git_head} failed: {err}"
    return diff
