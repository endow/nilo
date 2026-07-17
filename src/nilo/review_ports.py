from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


ReviewDispatchStatusValue = Literal[
    "accepted",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "unavailable",
]


class ReviewAdapterErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    COMMAND_NOT_FOUND = "command_not_found"
    AUTH_REQUIRED = "auth_required"
    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class SnapshotRef:
    git_head: str = ""
    git_diff_hash: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SnapshotRef:
        return cls(
            git_head=str(value.get("git_head") or ""),
            git_diff_hash=str(value.get("git_diff_hash") or ""),
            values=dict(value),
        )

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.values)
        result.update(git_head=self.git_head, git_diff_hash=self.git_diff_hash)
        return result


@dataclass(frozen=True)
class ReviewDispatcherCapabilities:
    capabilities: frozenset[str] = frozenset()
    available: bool = True
    supports_poll: bool = False
    supports_cancel: bool = False

    def supports(self, capability: str) -> bool:
        return not capability or capability in self.capabilities


@dataclass(frozen=True)
class ReviewDispatchRequest:
    request_id: str
    task_id: str
    reviewer: str
    snapshot: SnapshotRef
    prompt_path: Path | None = None
    prompt_text: str | None = None
    timeout_seconds: float = 120.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewDispatchHandle:
    request_id: str
    adapter_kind: str
    external_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewAdapterError:
    code: ReviewAdapterErrorCode
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewDispatchOutcome:
    status: ReviewDispatchStatusValue
    external_id: str | None = None
    result_payload: Mapping[str, Any] | None = None
    error: ReviewAdapterError | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


ReviewDispatchStatus = ReviewDispatchOutcome


@dataclass(frozen=True)
class ReviewCancelResult:
    cancelled: bool
    outcome: ReviewDispatchOutcome


@runtime_checkable
class ReviewDispatcher(Protocol):
    @property
    def kind(self) -> str: ...

    def capabilities(self) -> ReviewDispatcherCapabilities: ...

    def dispatch(self, request: ReviewDispatchRequest) -> ReviewDispatchOutcome: ...

    def poll(self, handle: ReviewDispatchHandle) -> ReviewDispatchStatus: ...

    def cancel(self, handle: ReviewDispatchHandle) -> ReviewCancelResult: ...
