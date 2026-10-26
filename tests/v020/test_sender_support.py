"""Shared fixtures for the sender tests. Not a test module: it holds no TestCase.

Everything here is offline: the fakes of ``tests/v020/fakes``, a hand-advanced clock, a
temporary ``state/`` and ``evidence/``. Nothing binds a device port and nothing is audible.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
LIB = os.path.join(ROOT, "lib")
HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (ROOT, LIB, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from v020 import contracts as C  # noqa: E402
from sender.api import SenderAPI  # noqa: E402
from sender.config import Config  # noqa: E402
from sender.log import EventLog, NullLog  # noqa: E402
from sender.offline import FakeClock, HostileController, TrimController, seams  # noqa: E402
from sender.session import SessionManager  # noqa: E402

YOUTUBE = "https://www.youtube.com/watch?v=QzomK1fdSUg"


def make_config(root: str, **overrides) -> Config:
    """A §6 configuration in a throwaway directory."""
    values = dict(
        http_bind="127.0.0.1",
        http_port=0,
        state_dir=os.path.join(root, "state"),
        evidence_dir=os.path.join(root, "evidence"),
        receiver_dir=os.path.join(root, "receiver"),
        assets_dir=os.path.join(root, "assets"),
    )
    values.update(overrides)
    return Config(**values).validate()


def read_log(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def events_named(path: str, name: str) -> list[dict]:
    return [event for event in read_log(path) if event.get("event") == name]


_FIXTURES: list = []


def close_fixtures() -> None:
    """Close every fixture a test built: the session, then its JSONL file."""
    while _FIXTURES:
        fixture = _FIXTURES.pop()
        try:
            fixture.sender.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            fixture.log.close()
        except Exception:  # noqa: BLE001
            pass


class TempDir:
    """unittest mixin: one throwaway directory per test, and every log closed after it."""

    def setUp(self) -> None:  # noqa: D401 - unittest hook
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="cast-sender-test-")
        _FIXTURES.clear()

    def tearDown(self) -> None:
        close_fixtures()
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()


def make_sender(root: str, *, controller=None, quiet=None, spawn=None, config=None,
                clock=None, log=None, link_clock=True, state_dir=None) -> types.SimpleNamespace:
    """A SessionManager wired to the fakes, with the helpers a test needs."""
    config = config or make_config(root)
    clock = clock or FakeClock()
    link, group, proxy, resolver = seams(clock=clock if link_clock else None)
    controller = TrimController() if controller is None else controller
    log = log if log is not None else EventLog(directory=config.evidence_dir)
    state_dir = state_dir or config.state_dir
    sender = SessionManager(
        link=link, group=group, controller=controller, proxy=proxy, config=config, log=log,
        resolver=resolver, quiet=quiet, clock=clock, wall=getattr(clock, "now", None),
        spawn=spawn if spawn is not None else (lambda fn: fn()), state_dir=state_dir)
    fixture = types.SimpleNamespace(
        sender=sender, link=link, group=group, proxy=proxy, resolver=resolver,
        controller=controller, clock=clock, config=config, log=log, state_dir=state_dir,
        record_path=os.path.join(state_dir, "session.json"),
        lock_path=os.path.join(state_dir, "group.owner"))
    _FIXTURES.append(fixture)
    return fixture


def tick(fixture, count: int, dt: float = 0.25) -> None:
    for _ in range(count):
        fixture.clock.advance(dt)
        fixture.sender.tick()


def make_api(fixture, *, extra_allowed=(), receiver_dir=None, assets_dir=None) -> SenderAPI:
    return SenderAPI(fixture.sender, config=fixture.config, log=fixture.log,
                     proxy=fixture.proxy, receiver_dir=receiver_dir or fixture.config.receiver_dir,
                     assets_dir=assets_dir or fixture.config.assets_dir,
                     extra_allowed=extra_allowed)


__all__ = [
    "C", "EventLog", "FakeClock", "HostileController", "NullLog", "SenderAPI",
    "SessionManager", "TempDir", "TrimController", "YOUTUBE", "close_fixtures", "events_named",
    "make_api", "make_config", "make_sender", "read_log", "seams", "tick",
]
