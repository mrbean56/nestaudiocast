"""Safety tiers for the v0.2 suite (docs/dev.md).

Every test belongs to exactly one tier. Unmarked tests are ``offline``, and an offline test
cannot reach the LAN: connect/sendto to anything but loopback raises, so "no packet to a
speaker" is enforced by the runner, not by each test's good manners.

The other tiers only run when explicitly allowed:

    lan      NAC_ALLOW_LAN=1      read-only traffic to LAN devices
    device   NAC_ALLOW_DEVICE=1   may launch an app on the Chromecast
    audible  NAC_ALLOW_SOUND=1    may make sound; also refused outside the window in
                                  which contracts §7 lets sound start (QUIET_START..QUIET_END,
                                  from the environment or /etc/castsender.conf)
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TIERS = ("offline", "lan", "device", "audible")
ALLOW = {"lan": "NAC_ALLOW_LAN", "device": "NAC_ALLOW_DEVICE", "audible": "NAC_ALLOW_SOUND"}
CASTSENDER_CONF = "/etc/castsender.conf"


def _tier(item) -> str:
    found = [t for t in TIERS if item.get_closest_marker(t)]
    if len(found) > 1:
        raise pytest.UsageError(f"{item.nodeid}: one safety tier per test, got {found}")
    return found[0] if found else "offline"


def _quiet_now() -> str | None:
    """A description of the window if sound may not start now (§7), else None. Unset = never quiet."""
    from sender.config import parse_env_file
    from sender.quiet import QuietWindow

    values = {}
    if os.path.exists(CASTSENDER_CONF):
        values = parse_env_file(CASTSENDER_CONF)
    start = os.environ.get("QUIET_START", values.get("QUIET_START", ""))
    end = os.environ.get("QUIET_END", values.get("QUIET_END", ""))
    window = QuietWindow(start, end)
    if window.enabled and not window.allowed():
        return f"{start}-{end}"
    return None


def pytest_collection_modifyitems(config, items):
    for item in items:
        tier = _tier(item)
        if tier == "offline":
            if not item.get_closest_marker("offline"):
                item.add_marker(pytest.mark.offline)
            continue
        var = ALLOW[tier]
        if os.environ.get(var) != "1":
            item.add_marker(pytest.mark.skip(reason=f"tier '{tier}' needs {var}=1"))
            continue
        if tier == "audible":
            quiet = _quiet_now()
            if quiet:
                item.add_marker(pytest.mark.skip(reason=f"quiet hours: sound may only start in {quiet}"))


def _loopback(address) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return True
    host = address[0]
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname: resolving it would already be a packet


class OfflineViolation(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _offline_guard(request, monkeypatch):
    if _tier(request.node) != "offline":
        yield
        return

    def guard(name, original):
        def wrapper(self, *args):
            address = args[-1] if name == "sendto" else args[0]
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address):
                raise OfflineViolation(
                    f"offline test tried {name}({address!r}); mark it lan/device/audible"
                )
            return original(self, *args)

        return wrapper

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, guard(name, getattr(socket.socket, name)))
    yield
