from __future__ import annotations

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


class McpReviewAdapter:
    """Asynchronous MCP-worker transport boundary.

    Worker registration, heartbeat and leases remain transport state; core only
    receives accepted/running/unavailable/cancelled outcomes.
    """

    def __init__(self, *, available: bool, capabilities: frozenset[str] = frozenset({"review"})) -> None:
        self._available = available
        self._capabilities = capabilities

    @property
    def kind(self) -> str:
        return "mcp_worker"

    def capabilities(self) -> ReviewDispatcherCapabilities:
        return ReviewDispatcherCapabilities(
            capabilities=self._capabilities,
            available=self._available,
            supports_poll=True,
            supports_cancel=True,
        )

    def dispatch(self, request: ReviewDispatchRequest) -> ReviewDispatchOutcome:
        if not self._available:
            return ReviewDispatchOutcome(
                status="unavailable",
                error=ReviewAdapterError(ReviewAdapterErrorCode.UNAVAILABLE, "MCP reviewer worker is unavailable"),
            )
        return ReviewDispatchOutcome(status="accepted", external_id=request.request_id)

    def poll(self, handle: ReviewDispatchHandle) -> ReviewDispatchStatus:
        return ReviewDispatchOutcome(status="running", external_id=handle.external_id)

    def cancel(self, handle: ReviewDispatchHandle) -> ReviewCancelResult:
        outcome = ReviewDispatchOutcome(status="cancelled", external_id=handle.external_id)
        return ReviewCancelResult(cancelled=True, outcome=outcome)
