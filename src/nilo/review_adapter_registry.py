from __future__ import annotations

from dataclasses import dataclass

from .review_ports import ReviewDispatcher


class ReviewAdapterResolutionError(LookupError):
    pass


@dataclass(frozen=True)
class RegisteredReviewAdapter:
    adapter: ReviewDispatcher
    reviewers: frozenset[str]


class ReviewAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, RegisteredReviewAdapter] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        adapter: ReviewDispatcher,
        *,
        reviewers: tuple[str, ...] | list[str] = (),
        aliases: dict[str, str] | None = None,
    ) -> None:
        kind = adapter.kind.strip()
        if not kind:
            raise ValueError("review adapter kind must not be empty")
        if kind in self._adapters:
            raise ValueError(f"review adapter already registered: {kind}")
        names = frozenset(name.strip() for name in reviewers if name.strip())
        self._adapters[kind] = RegisteredReviewAdapter(adapter, names)
        for alias, target in (aliases or {}).items():
            self._aliases[alias.strip()] = target.strip()

    def resolve(self, reviewer: str, capability: str = "", *, kind: str = "") -> ReviewDispatcher:
        canonical = self._aliases.get(reviewer, reviewer)
        candidates = self._adapters.values()
        if kind:
            registered = self._adapters.get(kind)
            candidates = (registered,) if registered else ()
        for registered in candidates:
            if registered is None:
                continue
            capabilities = registered.adapter.capabilities()
            if not capabilities.available or not capabilities.supports(capability):
                continue
            if registered.reviewers and canonical not in registered.reviewers:
                continue
            return registered.adapter
        raise ReviewAdapterResolutionError(
            f"no available review adapter for reviewer={canonical!r}, capability={capability!r}, kind={kind!r}"
        )

    def adapters(self) -> tuple[ReviewDispatcher, ...]:
        return tuple(item.adapter for item in self._adapters.values())
