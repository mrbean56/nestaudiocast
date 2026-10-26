"""``python -m sender.service`` — wire the seams and serve contracts §5.

Modes
-----
``--offline`` (also ``SENDER_OFFLINE=1``)  every seam is the in-memory fake of
    ``tests/v020/fakes``; no socket is opened to a speaker, a group or a Chromecast, and a
    resolver stub stands in for task B. This is the mode the tests, the smoke run and
    ``--self-test`` use, and it is the only mode that can start today.
``--live`` (the default of the unit)  the real ``lib/v020`` seams. They are frozen stubs until
    tasks B/D/E/F land, so a share records ``*_unavailable`` and the service keeps answering.

``--cleanup``  the ``ExecStopPost`` path: stop only the sessions *we* recorded and undo only the
    mutes *we* set (contracts §8), then exit.

Offline-only environment knobs (not configuration keys of §6): ``SENDER_OFFLINE``,
``SENDER_STATE_DIR``, ``SENDER_EVIDENCE_DIR``, ``SENDER_LOG_ECHO``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from v020.contracts import OWNER_FILE

from . import __version__
from .api import SenderAPI
from .config import Config, ConfigError, load_config
from .group_lock import GroupLock, boot_cleanup, session_record_path
from .log import EventLog
from .session import DEFAULT_TICK_S, SessionManager

#: Offline runs accept loopback as well as the LAN, so a self-test can talk to the server.
LOOPBACK = ("127.0.0.1", "::1")


@dataclass
class Service:
    """One wired owner app: the seams, the session machine, the HTTP surface and the log."""

    config: Config
    log: Any
    offline: bool
    sender: SessionManager
    api: SenderAPI
    link: Any = None
    group: Any = None
    proxy: Any = None
    controller: Any = None
    state_dir: str = ""
    evidence_dir: str = ""
    extra: dict = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    ticker: threading.Thread | None = None

    @property
    def record_path(self) -> str:
        return session_record_path(self.state_dir)

    def boot_cleanup(self) -> dict:
        """Startup / ExecStopPost cleanup (contracts §8)."""
        return boot_cleanup(self.record_path, group=self.group, link=self.link,
                            proxy=self.proxy, log=self.log)

    def start(self) -> "Service":
        """Start the observation loop (the thing that makes `ended` become `idle`)."""
        if self.ticker is None:
            self.ticker = run_ticker(self.sender, self.stop_event)
        return self

    def close(self) -> None:
        """Stop the loop, the session and the lock; the log belongs to the caller."""
        self.stop_event.set()
        ticker, self.ticker = self.ticker, None
        if ticker is not None:
            ticker.join(timeout=2.0)
        self.sender.close()


def build_seams(config: Config, *, offline: bool, log):
    """``(link, group, controller, proxy, resolver)`` — the five injected collaborators."""
    if offline:
        from .offline import seams as offline_seams

        link, group, proxy, resolver = offline_seams()
    else:
        from v020.chromecast import SenderLink
        from v020.group import CastGroup
        from v020.media_proxy import MediaProxy
        from v020.resolve import resolve as resolver

        link = SenderLink(config.chromecast, config.app_id, log=log)
        group = CastGroup(config.group, log=log)
        proxy = MediaProxy(config.http_bind, config.http_port, log=log)

    from v020.sync import Controller

    controller = Controller(delta_v2=config.delta_v2, rate_max=config.rate_max, log=log)
    return link, group, controller, proxy, resolver


def build_service(config: Config, *, offline: bool, log, state_dir: str | None = None,
                  extra_allowed: tuple[str, ...] | None = None,
                  clock=time.monotonic, wall=time.time, spawn=None,
                  quiet=None, tick_s: float = DEFAULT_TICK_S,
                  start_ticker: bool = True) -> Service:
    """Wire everything. No packet, no listener yet — ``api.make_server()`` does that."""
    state_dir = state_dir or config.state_dir
    os.makedirs(state_dir, exist_ok=True)
    link, group, controller, proxy, resolver = build_seams(config, offline=offline, log=log)
    lock = GroupLock(os.path.join(state_dir, os.path.basename(OWNER_FILE)), log=log)
    sender = SessionManager(
        link=link, group=group, controller=controller, proxy=proxy, config=config, log=log,
        resolver=resolver, quiet=quiet, lock=lock, state_dir=state_dir, clock=clock, wall=wall,
        spawn=spawn, tick_s=tick_s)
    allowed = LOOPBACK if offline else ()
    if extra_allowed:
        allowed = tuple(allowed) + tuple(extra_allowed)
    api = SenderAPI(sender, config=config, log=log, proxy=proxy,
                    receiver_dir=config.receiver_dir, assets_dir=config.assets_dir,
                    extra_allowed=allowed, ui_title="cast-sender")
    service = Service(config=config, log=log, offline=offline, sender=sender, api=api,
                      link=link, group=group, proxy=proxy, controller=controller,
                      state_dir=state_dir, evidence_dir=config.evidence_dir)
    return service.start() if start_ticker else service


def run_ticker(sender: SessionManager, stop: threading.Event, *,
               tick_s: float = DEFAULT_TICK_S) -> threading.Thread:
    """The observation loop: ≤1 s per poll, so a foreign takeover is seen within one poll."""

    def loop() -> None:
        while not stop.wait(tick_s):
            try:
                sender.tick()
            except Exception as exc:  # noqa: BLE001 - one bad tick must not stop the app
                try:
                    sender._log("tick_error", error=f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass

    thread = threading.Thread(target=loop, name="cast-sender-tick", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sender.service", description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=None,
                        help="configuration file (default: /etc/castsender.conf if present)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true",
                      help="fakes only: no packet leaves the process (default in tests)")
    mode.add_argument("--live", action="store_true",
                      help="use the real lib/v020 seams (stubs until tasks B/D/E/F land)")
    parser.add_argument("--bind", default=None, help="override HTTP_BIND (tests only)")
    parser.add_argument("--port", type=int, default=None, help="override HTTP_PORT (tests only)")
    parser.add_argument("--state-dir", default=os.environ.get("SENDER_STATE_DIR"))
    parser.add_argument("--evidence-dir", default=os.environ.get("SENDER_EVIDENCE_DIR"))
    parser.add_argument("--cleanup", action="store_true",
                        help="run the §8 boot cleanup from state/session.json and exit")
    parser.add_argument("--self-test", action="store_true",
                        help="run the offline self-test (HTTP matrix + 10 min session) and exit")
    parser.add_argument("--evidence", default=None,
                        help="with --self-test: write the JSON result to this path")
    parser.add_argument("--once", action="store_true",
                        help="bind, answer one status request, and exit (smoke check)")
    return parser.parse_args(argv)


def resolve_offline(args: argparse.Namespace) -> bool:
    if args.offline:
        return True
    if args.live:
        return False
    return os.environ.get("SENDER_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def load(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    if args.bind:
        overrides["http_bind"] = args.bind
    if args.port:
        overrides["http_port"] = int(args.port)
    if args.state_dir:
        overrides["state_dir"] = os.path.abspath(args.state_dir)
    if args.evidence_dir:
        overrides["evidence_dir"] = os.path.abspath(args.evidence_dir)
    return load_config(args.config, **overrides)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load(args)
    except (ConfigError, OSError) as exc:
        print(json.dumps({"error": "config", "detail": str(exc)}), file=sys.stderr)
        return 2
    offline = resolve_offline(args)

    if args.self_test:
        from .selftest import run

        result = run(evidence_path=args.evidence)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    log = EventLog(directory=config.evidence_dir,
                   echo=os.environ.get("SENDER_LOG_ECHO", "") == "1")
    service = build_service(config, offline=offline, log=log)
    service.boot_cleanup()
    log("service_starting", version=__version__, offline=offline, pid=os.getpid(),
        config=config.as_mapping(), state_dir=service.state_dir,
        evidence=os.path.basename(str(getattr(log, "path", ""))))

    if args.cleanup:
        result = service.boot_cleanup()
        print(json.dumps({"cleanup": result}, indent=2, ensure_ascii=False))
        log("service_cleanup", **result)
        log.close()
        return 0

    server = service.api.make_server()
    log("service_ready", bind=server.server_address[0], port=server.server_address[1],
        lan_only=True, offline=offline)

    def shutdown(*_args) -> None:
        service.stop_event.set()
        log("service_stopping", signal="term")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if args.once:
        import http.client

        service.stop_event.set()
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/status")
        reply = connection.getresponse()
        body = reply.read().decode()
        connection.close()
        server.shutdown()
        print(json.dumps({"status_code": reply.status, "body": json.loads(body)}, indent=2))
        service.close()
        return 0 if reply.status == 200 else 1

    try:
        server.serve_forever()
    finally:
        service.stop_event.set()
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        log("service_stopped", pid=os.getpid())
        service.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
