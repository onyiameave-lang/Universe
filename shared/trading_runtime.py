"""Shared runtime policy helpers for long-running trading processes."""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("shared.trading_runtime")


def optional_positive_int(name: str, default: Optional[int] = None) -> Optional[int]:
    """Read an optional positive integer; blank means no limit."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    if value < 1:
        log.warning("Invalid %s=%r; expected a positive integer; using %s", name, raw, default)
        return default
    return value
