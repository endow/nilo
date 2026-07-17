"""Compatibility alias for the Claude CLI review adapter."""

from __future__ import annotations

import sys

from .review_adapters import claude_cli as _claude_cli


sys.modules[__name__] = _claude_cli
