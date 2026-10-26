"""YouTube URL -> playable streams, with no media kept on disk (contracts §4).

Stub: signatures are frozen, bodies raise NotImplementedError. Task B implements them.
"""

from __future__ import annotations

import re

from .contracts import ResolvedMedia

_REDACT_TAIL = 6


class ResolveError(RuntimeError):
    """The URL cannot be resolved (private video, age gate, live stream, no avc1 track…)."""


def redact(url: str) -> str:
    """``scheme://host/<first 6 chars>…`` — the only form allowed in a log or an evidence file."""
    m = re.match(r"^(?P<scheme>[a-z0-9+.-]+)://(?P<host>[^/?#]+)(?P<path>[^?#]*)", url)
    if not m:
        return "<unparsable-url>"
    tail = m.group("path").lstrip("/")[:_REDACT_TAIL]
    return f"{m.group('scheme')}://{m.group('host')}/{tail}…"


def resolve(url: str, *, max_height: int = 1080, timeout: float = 30.0) -> ResolvedMedia:
    """Extract the best ``avc1`` ≤ ``max_height`` and an AAC audio track for ``url``.

    Returns both tracks from the same extraction (§4: same video id, same moment, so encoder
    priming is identical for the receiver and for the group). ``expires_at`` is the earliest
    expiry of the two source URLs; the proxy refreshes before it.
    """
    raise NotImplementedError("task B")


def video_id_of(url: str) -> str:
    """11-char YouTube id, or raise ResolveError. Never a prefix match (A33: a test whose
    predicate was wrong passed happily)."""
    raise NotImplementedError("task B")
