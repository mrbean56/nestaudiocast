"""The kitchen group, as v0.2 uses it: parked, then released, then left alone (contracts §1).

Stub: signatures are frozen, bodies raise NotImplementedError. Task E implements them.

Behaviour that must survive from v0.1 (A6, A37, evidence/27, evidence/28):
``lib/groupguard.require_safe_group`` on every connect and reconnect; mute never zeroes a level;
a restart decision needs two consecutive non-PLAYING reads (stale-read hysteresis); the playhead
estimate rejects contradicted samples and reports its residual.
"""

from __future__ import annotations

from .contracts import GroupSample


class UnsafeGroup(RuntimeError):
    """The group contains something that is not an audio speaker (contracts §0)."""


class CastGroup:
    """Audio sink: the Default Media Receiver on a speakers-only group."""

    def __init__(self, name: str = "Cuisine", host: str | None = None,
                 port: int | None = None, log=print) -> None:
        self.name, self.log = name, log
        self.host, self.port = host, port

    def connect(self) -> None:
        """Resolve by name over mDNS, refuse an unsafe group, connect, keepalive."""
        raise NotImplementedError("task E")

    def reconnect(self) -> None:
        """Re-resolve + re-guard after a link error (A22)."""
        raise NotImplementedError("task E")

    def prepare(self, url: str, content_type: str, at_time: float) -> bool:
        """LOAD parked in PAUSE at ``at_time`` (evidence/27: PAUSE->SEEK->PLAY σ 30 ms)."""
        raise NotImplementedError("task E")

    def play(self) -> None:
        """Release the parked position. The only instant that decides the start (§1)."""
        raise NotImplementedError("task E")

    def pause(self) -> None:
        raise NotImplementedError("task E")

    def seek(self, at_time: float) -> None:
        raise NotImplementedError("task E")

    def set_volume(self, level: float, muted: bool | None = None) -> None:
        """Mirror a level. Never sets a level to 0 to silence (A37)."""
        raise NotImplementedError("task E")

    def stop(self) -> None:
        """Stop the session this class created; leave every other session alone."""
        raise NotImplementedError("task E")

    def sample(self) -> GroupSample | None:
        """One playhead reading, with outliers rejected (§2, A15)."""
        raise NotImplementedError("task E")

    def playhead(self, wall: float) -> float | None:
        """Media position at controller time ``wall``, from the filtered estimator."""
        raise NotImplementedError("task E")

    def close(self) -> None:
        raise NotImplementedError("task E")
