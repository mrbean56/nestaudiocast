"""Quiet hours — contracts §7.

`QUIET_START…QUIET_END` is the window in which audible playback **may** start; outside it the
policy decides (``refuse`` = the share answers ``quiet_refused`` and nothing plays,
``video_only`` = the video plays and the group is never loaded). Empty window = quiet hours
disabled. The rule is about *starting*: a session already playing when the window opens is not
stopped, and nothing in this module looks at a running session.

The window wraps midnight when ``QUIET_START > QUIET_END`` (22:00…06:00). A zero-length window
(``QUIET_START == QUIET_END``) gates nothing, which is the same as disabled; saying so out loud
beats a window that silently refuses every share.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from v020.contracts import QUIET_POLICIES

#: The share may proceed; no audible restriction applies.
DECISION_OK = "ok"

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text: str) -> int | None:
    """``"HH:MM"`` -> minutes since local midnight; ``""`` -> None. Raises ValueError."""
    value = (text or "").strip()
    if not value:
        return None
    match = _HHMM.match(value)
    if not match:
        raise ValueError(f"not HH:MM: {text!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def format_hhmm(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes_of(at: float | int | datetime | None) -> int:
    """Local minutes since midnight for an epoch float, a datetime, or now."""
    if isinstance(at, datetime):
        return at.hour * 60 + at.minute
    if at is None:
        at = time.time()
    local = time.localtime(float(at))
    return local.tm_hour * 60 + local.tm_min


class QuietWindow:
    """The `QUIET_START`/`QUIET_END`/`QUIET_POLICY` triple of contracts §6."""

    def __init__(self, start: str = "", end: str = "", policy: str = "refuse") -> None:
        if policy not in QUIET_POLICIES:
            raise ValueError(f"QUIET_POLICY must be one of {QUIET_POLICIES}, got {policy!r}")
        self.start_text = (start or "").strip()
        self.end_text = (end or "").strip()
        self.start = parse_hhmm(self.start_text)
        self.end = parse_hhmm(self.end_text)
        if (self.start is None) != (self.end is None):
            raise ValueError("QUIET_START and QUIET_END go together (both set, or both empty)")
        self.policy = policy

    # -- window ---------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.start is not None

    @property
    def wraps_midnight(self) -> bool:
        return self.enabled and self.start > self.end

    def allowed(self, at: float | int | datetime | None = None) -> bool:
        """True when an audible start is allowed at ``at`` (local time)."""
        if not self.enabled:
            return True
        if self.start == self.end:
            # A zero-length window gates nothing; refusing everything would be a trap.
            return True
        minute = _minutes_of(at)
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def decision(self, at: float | int | datetime | None = None) -> str:
        """``ok``, or the policy when ``at`` is outside the allowed window."""
        return DECISION_OK if self.allowed(at) else self.policy

    def describe(self, at: float | int | datetime | None = None) -> dict:
        """The ``quiet`` field of ``GET /api/status`` (contracts §5 leaves its shape open)."""
        return {
            "enabled": self.enabled,
            "start": format_hhmm(self.start),
            "end": format_hhmm(self.end),
            "policy": self.policy,
            "allowed_now": self.allowed(at),
            "decision": self.decision(at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging help
        if not self.enabled:
            return "QuietWindow(disabled)"
        return f"QuietWindow({self.start_text}…{self.end_text}, {self.policy})"
