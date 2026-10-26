"""One JSONL event per line — ``evidence/sender-<date>.jsonl`` (contracts §9).

Two rules are enforced here, not left to the caller:

* every URL goes through :func:`v020.resolve.redact` before it is serialised. A googlevideo URL
  is a short-lived token; ``scheme://host/<6 chars>…`` is the only form allowed in a log or an
  evidence file.
* a field whose *name* says it holds a URL is redacted even when it does not look like one
  (``contentId``, ``sid`` are ids, not tokens, and are kept as they are).

The object is also callable, so it can be handed to the seams, which expect a ``log=`` callable.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from v020.resolve import redact

_URL_LIKE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_URL_NAME = re.compile(r"(url|uri|link|manifest|stream)$", re.IGNORECASE)


def redact_value(value: Any, key: str = "") -> Any:
    """Recursively redact URLs in a JSON-able structure."""
    if isinstance(value, str):
        if _URL_LIKE.match(value) or (_URL_NAME.search(key) and len(value) > 24):
            return redact(value) if _URL_LIKE.match(value) else f"<redacted:{len(value)} chars>"
        return value
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, key) for v in value]
    if isinstance(value, float):
        # JSON has no NaN; keep a readable marker instead of writing invalid JSON.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    return value


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


class EventLog:
    """Append-only JSONL writer, one line per event, flushed on every line."""

    def __init__(self, path: str | None = None, *, directory: str | None = None,
                 echo: bool = False, clock=time.time, mono=time.monotonic) -> None:
        if path is None:
            base = directory or os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "evidence")
            path = os.path.join(base, f"sender-{time.strftime('%Y-%m-%d', time.localtime())}.jsonl")
        self.path = path
        self.echo = echo
        self.clock = clock
        self.mono = mono
        self.count = 0
        self.last_event: str | None = None
        self.last: dict | None = None
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._fh = open(path, "a", buffering=1, encoding="utf-8")

    # -- writing ----------------------------------------------------------------------------
    def event(self, name: str, **fields: Any) -> dict:
        """Write one event; returns the record (already redacted)."""
        now = self.clock()
        record: dict[str, Any] = {
            "t": _iso(now),
            "mono": round(self.mono(), 3),
            "event": str(name),
        }
        record.update({k: redact_value(v, k) for k, v in fields.items()})
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            self.count += 1
            self.last_event = str(name)
            self.last = record
        if self.echo:
            print(line, flush=True)
        return record

    #: seams take a plain callable
    def __call__(self, name: str, **fields: Any) -> dict:
        return self.event(name, **fields)

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001 - closing twice must not raise
                pass


class NullLog:
    """A logger that keeps the last event name and writes nothing (tests, dry runs)."""

    def __init__(self) -> None:
        self.path = None
        self.count = 0
        self.last_event: str | None = None
        self.last: dict | None = None
        self.records: list[dict] = []

    def event(self, name: str, **fields: Any) -> dict:
        record = {"event": str(name), **{k: redact_value(v, k) for k, v in fields.items()}}
        self.count += 1
        self.last_event = str(name)
        self.last = record
        self.records.append(record)
        return record

    def __call__(self, name: str, **fields: Any) -> dict:
        return self.event(name, **fields)

    def close(self) -> None:
        pass
