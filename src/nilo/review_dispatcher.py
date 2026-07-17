"""Compatibility alias for the legacy review transport adapter.

New core code depends on :mod:`nilo.review_ports` and
:mod:`nilo.review_service`.  This module name remains available so existing
CLI integrations and third-party imports keep working during migration.
"""

from __future__ import annotations

import sys

from .review_adapters import legacy as _legacy


sys.modules[__name__] = _legacy
