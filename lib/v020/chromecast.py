"""The controller's link to our own receiver on the Chromecast (contracts §3).

Stub: signatures are frozen, bodies raise NotImplementedError. Task D implements them, on top of
``lib/castv2.py`` (do not modify that module: the v0.1 relay depends on it).
"""

from __future__ import annotations

from .contracts import ClockEstimate, Sample


class LostDevice(RuntimeError):
    """A foreign sender took the Chromecast, or our app was stopped (§3.3)."""


class SenderLink:
    """Launch, connect, speak the custom namespace, notice when the device is taken."""

    def __init__(self, host: str, app_id: str, port: int = 8009, log=print) -> None:
        self.host, self.app_id, self.port, self.log = host, app_id, port, log

    # -- lifecycle ---------------------------------------------------------------------------
    def launch(self, timeout: float = 20.0) -> dict:
        """LAUNCH ``app_id`` and wait for the namespace to come up. Returns the receiver status."""
        raise NotImplementedError("task D")

    def connect(self, timeout: float = 10.0) -> None:
        """Attach to the running app's transport; must survive a receiver reconnect."""
        raise NotImplementedError("task D")

    def close(self) -> None:
        raise NotImplementedError("task D")

    # -- protocol ----------------------------------------------------------------------------
    def send(self, msg_type: str, **payload) -> int:
        """Send one controller message (§3.1), return its ``seq``."""
        raise NotImplementedError("task D")

    def drain(self) -> list[dict]:
        """All receiver messages received since the last call, in order."""
        raise NotImplementedError("task D")

    def ping(self, n: int = 5) -> ClockEstimate:
        """N clock pings, min-RTT selection, residual reported (§3.1)."""
        raise NotImplementedError("task D")

    def latest(self) -> Sample | None:
        """Most recent receiver ``state``, or None."""
        raise NotImplementedError("task D")

    def is_ours(self) -> bool:
        """True when the app running on the device is ours (§3.3)."""
        raise NotImplementedError("task D")


def probe(host: str, port: int = 8008, timeout: float = 4.0) -> dict:
    """Read-only device info (eureka_info). Launches nothing, plays nothing."""
    raise NotImplementedError("task D")
