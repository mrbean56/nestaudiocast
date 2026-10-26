"""The safety tiers of conftest.py actually hold (docs/dev.md)."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta

import pytest
from conftest import OfflineViolation

pytest_plugins = ["pytester"]


def test_offline_cannot_reach_the_lan():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OfflineViolation):
            s.connect(("192.168.2.80", 8009))  # refused before any packet leaves
        with pytest.raises(OfflineViolation):
            s.sendto(b"x", ("224.0.0.251", 5353))  # mDNS
    finally:
        s.close()


def test_offline_may_use_loopback():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("127.0.0.1", 9))
    finally:
        s.close()


TIERED = """
import pytest

@pytest.mark.lan
def test_lan(): pass

@pytest.mark.device
def test_device(): pass

@pytest.mark.audible
def test_audible(): pass

def test_plain(): pass
"""


def _run(pytester, monkeypatch, **env):
    for var in ("NAC_ALLOW_LAN", "NAC_ALLOW_DEVICE", "NAC_ALLOW_SOUND", "QUIET_START", "QUIET_END"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    here = os.path.dirname(__file__)
    pytester.makeconftest(open(os.path.join(here, "conftest.py")).read())
    pytester.makeini(
        "[pytest]\nmarkers =\n  offline: x\n  lan: x\n  device: x\n  audible: x\naddopts = --strict-markers\n"
    )
    pytester.makepyfile(TIERED)
    return pytester.runpytest_inprocess("-q")


def test_only_offline_runs_by_default(pytester, monkeypatch):
    _run(pytester, monkeypatch).assert_outcomes(passed=1, skipped=3)


def test_each_tier_needs_its_own_permission(pytester, monkeypatch):
    _run(pytester, monkeypatch, NAC_ALLOW_LAN="1").assert_outcomes(passed=2, skipped=2)


# contracts §7: QUIET_START..QUIET_END is the window in which sound MAY start; outside it is quiet.
def test_sound_refused_outside_the_allowed_window(pytester, monkeypatch):
    now = datetime.now()
    start = (now + timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=2)).strftime("%H:%M")
    _run(pytester, monkeypatch, NAC_ALLOW_SOUND="1", QUIET_START=start, QUIET_END=end).assert_outcomes(
        passed=1, skipped=3
    )


def test_sound_runs_inside_the_allowed_window(pytester, monkeypatch):
    now = datetime.now()
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    _run(pytester, monkeypatch, NAC_ALLOW_SOUND="1", QUIET_START=start, QUIET_END=end).assert_outcomes(
        passed=2, skipped=2
    )


# 192.0.2.1 is TEST-NET-1 (RFC 5737): never routed, and a UDP connect sends no packet anyway.
EARLY = """
import socket, unittest

def attempt():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return "connected"
    except AssertionError as e:          # OfflineViolation
        return "refused"
    finally:
        s.close()

AT_IMPORT = attempt()

class Early(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.at_class_setup = attempt()

    def test_import_and_class_setup_are_guarded(self):
        assert AT_IMPORT == "refused", AT_IMPORT
        assert self.at_class_setup == "refused", self.at_class_setup
        assert attempt() == "refused"
"""

LIFTED = """
import socket, pytest

@pytest.mark.lan
def test_an_allowed_tier_may_leave_loopback():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
    finally:
        s.close()

def test_and_the_guard_is_back_afterwards():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(AssertionError):
            s.connect(("192.0.2.1", 9))
    finally:
        s.close()
"""


def _run_source(pytester, monkeypatch, source, **env):
    for var in ("NAC_ALLOW_LAN", "NAC_ALLOW_DEVICE", "NAC_ALLOW_SOUND", "QUIET_START", "QUIET_END"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    here = os.path.dirname(__file__)
    pytester.makeconftest(open(os.path.join(here, "conftest.py")).read())
    pytester.makeini(
        "[pytest]\nmarkers =\n  offline: x\n  lan: x\n  device: x\n  audible: x\naddopts = --strict-markers\n"
    )
    pytester.makepyfile(source)
    return pytester.runpytest_inprocess("-q")


def test_imports_and_class_setup_are_guarded(pytester, monkeypatch):
    _run_source(pytester, monkeypatch, EARLY).assert_outcomes(passed=1)


def test_an_allowed_tier_lifts_the_guard_only_for_itself(pytester, monkeypatch):
    _run_source(pytester, monkeypatch, LIFTED, NAC_ALLOW_LAN="1").assert_outcomes(passed=2)
