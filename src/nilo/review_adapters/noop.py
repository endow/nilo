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


class NoopReviewAdapter:
    @property
    def kind(self) -> str:
        return "noop"

    def capabilities(self) -> ReviewDispatcherCapabilities:
        return ReviewDispatcherCapabilities(available=False, supports_cancel=True)

    def dispatch(self, request: ReviewDispatchRequest) -> ReviewDispatchOutcome:
        return ReviewDispatchOutcome(
            status="unavailable",
            error=ReviewAdapterError(ReviewAdapterErrorCode.UNAVAILABLE, "review adapter is unavailable"),
            diagnostics={"adapter": self.kind},
        )

    def poll(self, handle: ReviewDispatchHandle) -> ReviewDispatchStatus:
        return ReviewDispatchOutcome(
            status="unavailable",
            external_id=handle.external_id,
            error=ReviewAdapterError(ReviewAdapterErrorCode.UNAVAILABLE, "review adapter is unavailable"),
            diagnostics={"adapter": self.kind},
        )

    def cancel(self, handle: ReviewDispatchHandle) -> ReviewCancelResult:
        outcome = ReviewDispatchOutcome(status="cancelled", external_id=handle.external_id)
        return ReviewCancelResult(cancelled=True, outcome=outcome)
