from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from ..review_ports import (
    ReviewAdapterError,
    ReviewAdapterErrorCode,
    ReviewCancelResult,
    ReviewDispatcherCapabilities,
    ReviewDispatchHandle,
    ReviewDispatchOutcome,
    ReviewDispatchRequest,
    ReviewDispatchStatus,
)
from ..secret import mask_secrets


class LocalCliReviewAdapter:
    """Synchronous argv-only process adapter with normalized failures."""

    def __init__(
        self,
        *,
        kind: str,
        command: Callable[[ReviewDispatchRequest], Sequence[str]],
        cwd: Path,
        capabilities: frozenset[str] = frozenset({"review"}),
    ) -> None:
        self._kind = kind
        self._command = command
        self._cwd = cwd
        self._capabilities = capabilities

    @property
    def kind(self) -> str:
        return self._kind

    def capabilities(self) -> ReviewDispatcherCapabilities:
        return ReviewDispatcherCapabilities(capabilities=self._capabilities)

    def dispatch(self, request: ReviewDispatchRequest) -> ReviewDispatchOutcome:
        argv = list(self._command(request))
        if not argv or not argv[0]:
            return self._failure(ReviewAdapterErrorCode.COMMAND_NOT_FOUND, "review command is empty")
        try:
            completed = subprocess.run(
                argv,
                cwd=self._cwd,
                input=request.prompt_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=request.timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            return self._failure(ReviewAdapterErrorCode.COMMAND_NOT_FOUND, f"command not found: {argv[0]}")
        except subprocess.TimeoutExpired:
            return self._failure(ReviewAdapterErrorCode.TIMEOUT, "review command timed out", status="timed_out")
        except OSError as exc:
            return self._failure(ReviewAdapterErrorCode.CONNECTION_FAILED, str(exc))
        diagnostics = {"exit_code": completed.returncode, "stderr": mask_secrets(completed.stderr)[-2000:]}
        if completed.returncode:
            error_code = self._classify_failure(completed.stderr)
            return ReviewDispatchOutcome(
                status="failed",
                error=ReviewAdapterError(error_code, "review command failed"),
                diagnostics=diagnostics,
            )
        if not completed.stdout.strip():
            return ReviewDispatchOutcome(
                status="failed",
                error=ReviewAdapterError(ReviewAdapterErrorCode.INVALID_RESPONSE, "review command returned an empty response"),
                diagnostics=diagnostics,
            )
        return ReviewDispatchOutcome(
            status="completed",
            result_payload={"body_md": mask_secrets(completed.stdout)},
            diagnostics=diagnostics,
        )

    def poll(self, handle: ReviewDispatchHandle) -> ReviewDispatchStatus:
        return self._failure(ReviewAdapterErrorCode.INVALID_RESPONSE, "synchronous adapter cannot be polled")

    def cancel(self, handle: ReviewDispatchHandle) -> ReviewCancelResult:
        outcome = self._failure(ReviewAdapterErrorCode.CANCELLED, "synchronous dispatch already exited", status="cancelled")
        return ReviewCancelResult(cancelled=True, outcome=outcome)

    def _failure(
        self,
        code: ReviewAdapterErrorCode,
        message: str,
        *,
        status: str = "failed",
    ) -> ReviewDispatchOutcome:
        return ReviewDispatchOutcome(
            status=status,  # type: ignore[arg-type]
            error=ReviewAdapterError(code, mask_secrets(message)),
            diagnostics={"adapter": self.kind},
        )

    @staticmethod
    def _classify_failure(stderr: str) -> ReviewAdapterErrorCode:
        normalized = stderr.casefold()
        if any(marker in normalized for marker in ("unauthorized", "authentication", "api key", "status 401", "status 403")):
            return ReviewAdapterErrorCode.AUTH_REQUIRED
        if any(marker in normalized for marker in ("connection refused", "connection failed", "network is unreachable")):
            return ReviewAdapterErrorCode.CONNECTION_FAILED
        return ReviewAdapterErrorCode.INTERNAL_ERROR
