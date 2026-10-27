"""The controller's link to our own receiver on the Chromecast (contracts §3).

``SenderLink`` sits on top of :mod:`lib/castv2.py` (which the v0.1 relay depends on, so it is
not modified here) and is the only place that speaks the custom namespace
``urn:x-cast:com.beansden.nestaudiocast.v1`` to the receiver page.

What it owns, in one place:

* **launch / connect** — ``LAUNCH`` the frozen ``app_id`` on the ``receiver`` namespace, then
  attach to the app's transport and wait for the receiver's ``hello``. ``connect()`` re-attaches
  to an app that is already running and survives a receiver reconnect (new transport, new
  ``hello``, clock re-based). It never launches: a foreign app or an idle device raises
  :class:`LostDevice` or :class:`LinkDown` instead (§3.3, "never relaunches by itself").
* **the protocol** — :meth:`SenderLink.send` stamps a strictly increasing ``seq`` on every
  controller message, :meth:`SenderLink.drain` hands back the receiver messages in arrival
  order out of a **bounded** inbox (A23: the v0.1 inbox grew without bound),
  :meth:`SenderLink.latest` turns the last ``state`` into a typed ``Sample``.
* **the clock** — ``ping(n)`` runs ``n`` clock round trips, keeps the minimum-RTT one (the
  sample least polluted by queueing), and reports the residual, which is the observed
  disagreement between the pings. A receiver clock that jumps (page reload, NTP step) is
  **flagged and not followed** until the caller re-bases it: :attr:`SenderLink.clock_flagged`,
  :attr:`SenderLink.clock_jump_ms`, :meth:`SenderLink.reset_clock`.
* **presence** — :meth:`SenderLink.is_ours` answers in one ``GET_STATUS``; an unsolicited
  ``RECEIVER_STATUS`` for another app, or an app that stopped, is also caught while reading
  (§3.3). The loss is sticky (:attr:`SenderLink.lost_reason`) and every command that needs the
  device then raises :class:`LostDevice`; only :meth:`SenderLink.reconnect` /
  :meth:`SenderLink.launch` clear it.

``log`` is called as ``log(event, **fields)``; the default ``print`` cannot take keywords, so
the fallback emits one JSON object per line. No googlevideo URL is ever seen here.

Nothing in this module talks to a speaker, a group or the TV.
"""

from __future__ import annotations

import collections
import http.client
import json
import random
import socket
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import contracts as C

_LIB = str(Path(__file__).resolve().parent.parent)
if _LIB not in sys.path:  # lib/ holds castv2.py; CI imports this module on a bare
    sys.path.insert(0, _LIB)  # python, so the pychromecast-backed import stays lazy

_CASTV2 = None


#: how long an adopted app instance gets to answer `hello` before it is recycled
HELLO_PROBE_S = 3.0
IDLE_APP_ID = "E8C28D3C"  # Backdrop: the screen a device shows when nobody owns it


def _is_idle_screen(status: dict) -> bool:
    """True when the device is on its own idle screen (Backdrop) or reports nothing at all."""
    payload = status.get("payload") if isinstance(status, dict) else None
    body = payload if isinstance(payload, dict) else (status if isinstance(status, dict) else {})
    apps = body.get("applications") or []
    if not apps:
        return True
    app = apps[0] or {}
    return app.get("appId") == IDLE_APP_ID or bool(app.get("isIdleScreen"))


def _castv2():
    """Import ``lib/castv2.py`` on first use.

    It needs ``pychromecast``'s generated protobufs, which the contracts-only CI job does not
    install; importing it lazily keeps ``import v020.chromecast`` dependency-free.
    """
    global _CASTV2
    if _CASTV2 is None:
        import castv2 as mod  # noqa: PLC0415  (deliberate: see docstring)

        _CASTV2 = mod
    return _CASTV2


# --- tunables (all measured or bounded in notes/labo-link.md) --------------------------------

#: A23: the v0.1 inbox "grew forever". 256 states is 64 s at the 4 Hz state cadence; beyond
#: that the oldest are dropped and counted, never silently.
INBOX_MAX = 256

#: Our own heartbeat cadence on a link nobody reads (A23: the receiver closes a link whose
#: PINGs go unanswered, ~24 s).
HEARTBEAT_S = 5.0

#: A clock estimate further than this from the accepted one is a jump, not jitter (the RTT
#: jitter of a LAN round trip is a few ms; the smallest page reload moves timeOrigin by much
#: more).
CLOCK_JUMP_MS = 50.0

#: ... or this many times the last residual, whichever is larger.
CLOCK_JUMP_SIGMA = 8.0

#: A receiver timestamp that goes backwards by less than this is treated as reordering, not as
#: a clock step: the fake shows up to ~6 ms of it under a 40 ms RTT distribution, and a real
#: system-clock step (the case this guard exists for) is far larger.
CLOCK_BACKWARDS_TOL_MS = 250.0

#: Bounded reconnect backoff: 5 attempts, 1.55 s of waiting in total.
RECONNECT_DELAYS_S = (0.05, 0.10, 0.20, 0.40, 0.80)

#: Timeouts, deliberately short: this is a LAN device and a control loop must not block.
STATUS_TIMEOUT_S = 2.0
#: A Chromecast busy decoding 1080p sometimes misses one GET_STATUS. Measured on the device
#: (2026-10-27): 2 of 6 share cycles died on that single timeout, reported as a takeover. A
#: timeout is not evidence of anything, so reads are retried before a verdict is drawn.
STATUS_ATTEMPTS = 3
STATUS_RETRY_S = 0.5
CLOCK_TIMEOUT_S = 2.0
HELLO_TIMEOUT_S = 10.0

#: The google receiver namespace. Same string as ``castv2.NS_RECEIVER``; repeated here so the
#: presence logic can run (and be tested) without importing the pychromecast-backed module.
NS_CAST_RECEIVER = "urn:x-cast:com.google.cast.receiver"
#: the virtual-connection namespace (CONNECT / CLOSE), same string as ``castv2.NS_CONNECTION``
NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"

#: Fields §3.1 requires, checked before anything reaches the wire.
REQUIRED_FIELDS = {
    C.MSG_LOAD: ("url", "content_type", "media"),
    C.MSG_PLAY_AT: ("rx_ms", "media"),
    C.MSG_SEEK: ("media",),
    C.MSG_RATE: ("value",),
}

RATE_RANGE = (0.5, 2.0)


def now_ms() -> float:
    """The controller's clock: monotonic milliseconds (§3, "its own monotonic base")."""
    return time.monotonic() * 1000.0


class LostDevice(RuntimeError):
    """A foreign sender took the Chromecast, or our app was stopped (§3.3)."""


class LinkDown(RuntimeError):
    """No usable link: unreachable device, closed socket, or a failed reconnect.

    Distinct from :class:`LostDevice` on purpose. ``LostDevice`` means "the device is alive and
    somebody else owns it" — stop the group. ``LinkDown`` means "we cannot talk to it right
    now" — :meth:`SenderLink.reconnect` is the answer.
    """


def estimate_clock(samples: Iterable[Sequence[float]]) -> tuple[C.ClockEstimate, dict]:
    """Min-RTT clock estimate from ``(t0_ms, rx_ms, t3_ms)`` triples.

    Midpoint (NTP/Cristian) estimator, then **selection, not averaging**: the sample with the
    smallest round trip is the one least polluted by queueing on either side, and averaging
    would mix a clean sample with a congested one. Selection also protects against a
    one-sided detection delay: a late ``t3`` inflates that sample's RTT, so the contaminated
    ping is not the one selected.

    ``residual_ms`` is the **robust dispersion** of the per-sample offsets around the selected
    one: ``1.4826 × median(|offset_i − offset_sel|)`` (MAD scaled to a σ-equivalent). A single
    congested ping cannot inflate it, and the worst disagreement is reported next to it in
    ``spread_ms``. It measures local consistency, not the absolute error: a constant path
    asymmetry biases every ping by the same amount and no one-sided estimator can see it
    (notes/labo-link.md L1.4); the absolute error bound under symmetry, ``rtt_ms / 2``, is in
    the stats as ``bound_ms``.
    """
    rows = []
    for t0, rx, t3 in samples:
        t0, rx, t3 = float(t0), float(rx), float(t3)
        rtt = t3 - t0
        if rtt < 0.0:  # impossible without a clock jump: ignore
            continue
        rows.append({"rtt_ms": rtt, "offset_ms": rx - (t0 + t3) / 2.0})
    if not rows:
        raise ValueError("no usable clock sample")
    best = min(rows, key=lambda row: row["rtt_ms"])
    offset = best["offset_ms"]
    deviations = sorted(abs(row["offset_ms"] - offset) for row in rows)
    n = len(deviations)
    median_dev = deviations[n // 2] if n % 2 else (deviations[n // 2 - 1] + deviations[n // 2]) / 2.0
    residual = 1.4826 * median_dev
    est = C.ClockEstimate(
        offset_ms=offset,
        residual_ms=residual,
        rtt_ms=best["rtt_ms"],
        samples=len(rows),
        measured_at=time.monotonic(),
    )
    stats = {
        "offsets_ms": [round(row["offset_ms"], 3) for row in rows],
        "rtts_ms": [round(row["rtt_ms"], 3) for row in rows],
        "bound_ms": round(best["rtt_ms"] / 2.0, 3),
        "spread_ms": round(deviations[-1], 3),
        "residual_ms": round(residual, 3),
    }
    return est, stats


class SenderLink:
    """Launch, connect, speak the custom namespace, notice when the device is taken."""

    INBOX_MAX = INBOX_MAX

    def __init__(self, host: str, app_id: str, port: int = 8009, log=print) -> None:
        self.host, self.app_id, self.port, self.log = host, app_id, port, log

        # link state
        self.link = None
        self.transport: str | None = None
        self.session_id: str | None = None
        self.closed = True
        self._owns = False

        # protocol state
        self.seq = 0  # never reset across reconnects
        self.hello: dict | None = None
        self.hellos = 0
        self.last_state: C.Sample | None = None
        self.inbox: collections.deque[dict] = collections.deque(maxlen=self.INBOX_MAX)
        self.dropped = 0
        self._hello_pending = False
        self._status_misses = 0
        self._media_loaded = False
        self._last_send_at = 0.0
        self.last_send_ms = 0.0  # controller clock at the write of the last message
        self._status: dict = {}
        self._adopt_at = 0.0  # statuses older than this describe a previous owner

        # clock state
        self.clock: C.ClockEstimate | None = None
        self.clock_flagged = False
        self.clock_jump_ms: float | None = None
        self.clock_backwards = 0
        self._last_rx_ms: float | None = None
        self._backwards_at_last_ping = 0
        self.last_ping_stats: dict | None = None

        # presence / recovery
        self.lost_reason: str | None = None
        self.reconnects = 0

    # -- logging -----------------------------------------------------------------------------
    def _emit(self, event: str, **fields: Any) -> None:
        try:
            self.log(event, **fields)
        except TypeError:
            try:
                self.log(json.dumps({"ev": event, **fields}, default=str, ensure_ascii=False))
            except Exception:  # noqa: BLE001  (a logger must never break the link)
                pass

    # -- lifecycle ---------------------------------------------------------------------------
    def launch(self, timeout: float = 20.0) -> dict:
        """LAUNCH ``app_id`` and wait for the namespace to come up. Returns the receiver status.

        A read-only ``GET_STATUS`` always comes first: if our app is already running we adopt it
        (never relaunch on top of it), if another app is running we refuse (that is a takeover,
        §3.3 — the owner decides), and we never launch after a failed status check.
        """
        cv = _castv2()
        if self.link is None or self.closed:
            self.closed = False
            self._open(timeout)
        try:
            status = self._status_with_retry(min(timeout, STATUS_TIMEOUT_S * 3))
        except cv.CastError as exc:
            raise LinkDown(f"status check before LAUNCH failed on {self.host}: {exc}") from exc

        app_id, _name, _transport = cv.receiver_app(status)
        if app_id == self.app_id:
            self._emit("already_running", app_id=app_id, host=self.host)
        elif app_id is None or _is_idle_screen(status):
            # The device sitting on its own idle screen is *free*, not somebody else's cast.
            # Measured on CT 121 (2026-10-26): Backdrop is what an idle Chromecast reports, and
            # treating it as a takeover refused every first launch of a session.
            self._emit("idle_screen", app_id=app_id, host=self.host)
        else:
            self._mark_lost(f"foreign_app:{app_id}")
            raise LostDevice(
                f"{self.host} is running {app_id!r}, not ours ({self.app_id}); refusing to launch"
            )

        if app_id != self.app_id:
            # free device (idle screen or nothing): launch ours

            try:
                payload = self.link.launch(self.app_id, timeout=timeout)
            except cv.CastError as exc:
                raise LinkDown(f"LAUNCH {self.app_id} failed on {self.host}: {exc}") from exc
            status = (payload or {}).get("status") or {}
            app_id, _name, _transport = cv.receiver_app(status)
            if app_id != self.app_id:
                raise LinkDown(f"LAUNCH returned app {app_id!r}, expected {self.app_id}")

        self.lost_reason = None
        self._adopt_app(status, attach=True)
        try:
            self._wait_hello(min(timeout, HELLO_PROBE_S))
        except LinkDown:
            # Measured on CT 121 (2026-10-26): an app instance left over from earlier sessions can
            # be wedged — the app is ours, the page never answers the namespace. Adopting it would
            # fail every share until somebody recycles the device, so recycle the *app* instead:
            # stop it and launch once more, which is what a fresh launch does reliably.
            if app_id != self.app_id:
                raise
            self._emit("stale_app", app_id=app_id, note="no hello: relaunching once")
            self._restart_app(status, timeout)
        self._emit(
            "launched",
            app_id=self.app_id,
            transport=self.transport,
            session_id=self.session_id,
            hellos=self.hellos,
        )
        return status

    def _restart_app(self, status: dict, timeout: float) -> None:
        """Stop our wedged app instance and launch a fresh one (one attempt, then give up)."""
        cv = _castv2()
        if self.link is None or self.closed:
            # the failed hello path closed the link and marked us closed; reopening is cheap and
            # beats failing a share because of a bookkeeping detail
            self.closed = False
            self._open(timeout)
        session_id = None
        body = (status.get("payload") if isinstance(status, dict) else None) or status or {}
        for app in body.get("applications") or []:
            if app.get("appId") == self.app_id:
                session_id = app.get("sessionId")
        if session_id:
            try:
                self.link.stop_app(session_id, timeout=8)
            except cv.CastError as exc:
                raise LinkDown(f"could not stop the stale app on {self.host}: {exc}") from exc
            time.sleep(1.0)
        try:
            payload = self.link.launch(self.app_id, timeout=timeout)
        except cv.CastError as exc:
            raise LinkDown(f"relaunch of {self.app_id} failed on {self.host}: {exc}") from exc
        fresh = (payload or {}).get("status") or {}
        app_id, _name, _transport = cv.receiver_app(fresh)
        if app_id != self.app_id:
            raise LinkDown(f"relaunch returned app {app_id!r}, expected {self.app_id}")
        self.hellos = 0
        self._adopt_app(fresh, attach=True)
        self._wait_hello(timeout)

    def connect(self, timeout: float = 10.0) -> None:
        """Attach to the running app's transport; must survive a receiver reconnect."""
        cv = _castv2()
        self.closed = False
        self._close_link()
        self._open(timeout)
        try:
            status = self.link.receiver_status(timeout=timeout)
        except cv.CastError as exc:
            self._close_link()
            self.closed = True
            raise LinkDown(f"GET_STATUS failed on {self.host}: {exc}") from exc

        app_id, _name, _transport = cv.receiver_app(status)
        if app_id != self.app_id:
            reason = "no_app" if app_id is None else f"foreign_app:{app_id}"
            self._mark_lost(reason)
            self._close_link()
            self.closed = True
            raise LostDevice(f"{self.host} is not running {self.app_id} ({reason})")

        if self.lost_reason is not None:
            self._emit("lost_cleared", reason=self.lost_reason)
        self.lost_reason = None
        self._adopt_app(status, attach=True)
        try:
            self._wait_hello(min(timeout, HELLO_PROBE_S))
        except LinkDown:
            # Measured on CT 121 (2026-10-26): an app instance left over from earlier sessions can
            # be wedged — the app is ours, the page never answers the namespace. Adopting it would
            # fail every share until somebody recycles the device, so recycle the *app* instead:
            # stop it and launch once more, which is what a fresh launch does reliably.
            if app_id != self.app_id:
                raise
            self._emit("stale_app", app_id=app_id, note="no hello: relaunching once")
            self._restart_app(status, timeout)
        self._emit(
            "connected",
            transport=self.transport,
            session_id=self.session_id,
            seq=self.seq,
            hellos=self.hellos,
        )

    def reconnect(self, attempts: int | None = None, timeout: float = 5.0) -> None:
        """Bounded backoff reconnect; keeps ``seq``, ``last_state`` and the last clock.

        Never relaunches and never silently starts a new session: a five-attempt failure raises
        :class:`LinkDown`, a foreign app raises :class:`LostDevice`.
        """
        if attempts is None:
            attempts = len(RECONNECT_DELAYS_S)
        if attempts < 1:
            raise ValueError("reconnect needs at least one attempt")
        self._close_link()
        self.closed = False
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                self.connect(timeout=timeout)
                self.reconnects += 1
                self._emit(
                    "reconnected",
                    attempt=attempt + 1,
                    transport=self.transport,
                    seq=self.seq,
                    host=self.host,
                )
                return
            except LostDevice:
                raise
            except Exception as exc:  # noqa: BLE001  (LinkDown, CastError, OSError)
                last = exc
            delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
            delay *= 1.0 + random.uniform(-0.2, 0.2)
            self._emit(
                "reconnect_failed",
                attempt=attempt + 1,
                delay_ms=round(delay * 1000),
                error=f"{type(last).__name__}: {last}",
            )
            time.sleep(delay)
        raise LinkDown(f"reconnect to {self.host}:{self.port} failed after {attempts} attempts: {last}")

    def close(self) -> None:
        self.closed = True
        self._close_link()

    # -- protocol ----------------------------------------------------------------------------
    def send(self, msg_type: str, **payload) -> int:
        """Send one controller message (§3.1), return its ``seq``."""
        if msg_type not in C.CONTROLLER_MESSAGES:
            raise ValueError(
                f"unknown controller message type {msg_type!r}; "
                f"allowed: {', '.join(C.CONTROLLER_MESSAGES)}"
            )
        for reserved in ("type", "seq"):
            if reserved in payload:
                raise ValueError(f"payload may not carry {reserved!r}: the link owns that field")
        missing = [f for f in REQUIRED_FIELDS.get(msg_type, ()) if f not in payload]
        if missing:
            raise ValueError(f"{msg_type} is missing {', '.join(missing)}")
        if msg_type == C.MSG_RATE:
            value = float(payload["value"])
            if not RATE_RANGE[0] <= value <= RATE_RANGE[1]:
                raise ValueError(f"rate {value} outside {RATE_RANGE[0]}..{RATE_RANGE[1]} (§3.1)")

        if self.link is None or self.closed:
            raise LinkDown(f"no link to {self.host}:{self.port}; call connect() or reconnect()")
        if self.lost_reason:
            raise LostDevice(self.lost_reason)
        if not self.transport:
            raise LinkDown("not attached to the receiver transport; call connect()")

        self._service()  # keepalive + harvest before we write
        if self.link is None or self.closed:
            raise LinkDown("link dropped while sending")
        if self.lost_reason:  # a status read by that pass may have just lost it
            raise LostDevice(self.lost_reason)

        self.seq += 1
        body = {"type": msg_type, "seq": self.seq, **payload}
        try:
            self.last_send_ms = now_ms()
            self.link.send(self.transport, C.NS_RECEIVER, body)
        except Exception as exc:  # noqa: BLE001
            self._link_error(exc)
            raise LinkDown(f"send {msg_type} failed: {type(exc).__name__}: {exc}") from exc

        if msg_type in (C.MSG_LOAD, C.MSG_PLAY_AT):
            self._media_loaded = True
        elif msg_type in (C.MSG_STOP, C.MSG_IDLE):
            self._media_loaded = False
        self._last_send_at = time.time()
        return self.seq

    def drain(self) -> list[dict]:
        """All receiver messages received since the last call, in order.

        The queue is bounded (:attr:`INBOX_MAX`); a burst bigger than that loses the oldest,
        and :attr:`dropped` counts exactly how many, so the loss is visible.
        """
        self._service()
        out = list(self.inbox)
        self.inbox.clear()
        return out

    def latest(self) -> C.Sample | None:
        """Most recent receiver ``state``, or None. A backwards clock is flagged, not followed."""
        self._service()
        return self.last_state

    def ping(self, n: int = 5) -> C.ClockEstimate:
        """N clock pings, min-RTT selection, residual reported (§3.1)."""
        if n < 1:
            raise ValueError("ping needs at least one sample")
        if self.link is None or self.closed:
            raise LinkDown(f"no link to {self.host}:{self.port}; call connect() or reconnect()")
        if self.lost_reason:
            raise LostDevice(self.lost_reason)

        samples: list[tuple[float, float, float]] = []
        for _ in range(n):
            seq = self.send(C.MSG_CLOCK)
            t0 = self.last_send_ms  # stamped just before the write, not before the service pass
            reply = self._wait_reply(C.MSG_CLOCK_REPLY, seq, CLOCK_TIMEOUT_S)
            if reply is None:
                self._emit("clock_no_reply", seq=seq)
                continue
            t3 = now_ms()
            samples.append((t0, float(reply["rx_ms"]), t3))
        if not samples:
            raise LinkDown(f"no clock_reply from {self.host} in {n} pings")
        return self._accept_clock(*estimate_clock(samples))

    def _status_with_retry(self, timeout: float) -> dict:
        """``GET_STATUS`` with retries: one missed reply is a busy device, not a takeover."""
        cv = _castv2()
        last: Exception | None = None
        for attempt in range(STATUS_ATTEMPTS):
            try:
                return self.link.receiver_status(timeout=timeout)
            except cv.CastError as exc:
                last = exc
                if attempt + 1 < STATUS_ATTEMPTS:
                    self._emit("status_retry", attempt=attempt + 1, error=str(exc)[:80])
                    # Retrying on the same socket is useless when the socket is what died: an
                    # app restart (or the device dropping an idle TLS session) leaves our link
                    # dead, and every later read times out on it (measured 2026-10-27: that is
                    # exactly the 15 s hole the failing shares showed).
                    try:
                        self._close_link()
                        self.closed = False
                        self._open(timeout)
                    except Exception as reopen_exc:  # noqa: BLE001
                        self._emit("status_reopen_failed", error=str(reopen_exc)[:80])
                    time.sleep(STATUS_RETRY_S)
        raise cv.CastError(f"{last}")

    def is_ours(self) -> bool:
        """True when the app running on the device is ours (§3.3).

        One ``GET_STATUS`` is one poll: the answer is positive only when that status names our
        ``app_id``. Any failure answers False and records a reason — the safe direction, since
        the caller's reaction to a lost device is to stop the group.
        """
        if self.link is None or self.closed:
            return False
        if self.lost_reason:
            return False
        cv = _castv2()
        try:
            status = self._status_with_retry(STATUS_TIMEOUT_S)
        except cv.CastError as exc:
            # a read that never arrives says nothing about who owns the device: keep the session
            # and let the next poll decide (two misses in a row are a real loss)
            self._status_misses += 1
            if self._status_misses < 2:
                self._emit("status_missed", error=str(exc)[:80])
                return True
            self._mark_lost(f"status_failed:{type(exc).__name__}")
            return False
        except Exception as exc:  # noqa: BLE001
            self._mark_lost(f"status_failed:{type(exc).__name__}")
            return False
        self._status_misses = 0

        apps = status.get("applications") or []
        if not apps:
            self._mark_lost("no_app")
            return False
        if apps[0].get("appId") != self.app_id:
            self._mark_lost(f"foreign_app:{apps[0].get('appId')}")
            return False
        self._note_status(status)
        return True

    # -- clock helpers -----------------------------------------------------------------------
    def receiver_ms(self, now_ms_value: float | None = None) -> float | None:
        """The receiver's clock (ms) at controller time ``now_ms_value``, or None if unpinned."""
        if self.clock is None:
            return None
        base = now_ms() if now_ms_value is None else float(now_ms_value)
        return base + self.clock.offset_ms

    def reset_clock(self) -> None:
        """Re-base after a jump the caller decided to accept (e.g. after a page reload)."""
        had = self.clock is not None
        self.clock = None
        self.clock_flagged = False
        self.clock_jump_ms = None
        self._last_rx_ms = None
        self._backwards_at_last_ping = self.clock_backwards
        if had:
            self._emit("clock_reset", backwards=self.clock_backwards)

    def _accept_clock(self, est: C.ClockEstimate, stats: dict) -> C.ClockEstimate:
        """Adopt a fresh estimate unless it looks like a clock jump."""
        self.last_ping_stats = {
            **stats,
            "offset_ms": round(est.offset_ms, 3),
            "rtt_ms": round(est.rtt_ms, 3),
            "samples": est.samples,
        }
        if self.clock is None:
            self.clock = est
            self.clock_flagged = False
            self.clock_jump_ms = None
            self._backwards_at_last_ping = self.clock_backwards
            return est

        jump = est.offset_ms - self.clock.offset_ms
        tol = max(CLOCK_JUMP_MS, CLOCK_JUMP_SIGMA * max(self.clock.residual_ms, 0.5))
        backwards = self.clock_backwards > self._backwards_at_last_ping
        if backwards or abs(jump) > tol:
            self.clock_flagged = True
            self.clock_jump_ms = round(jump, 3)
            self._emit(
                "clock_jump_flagged",
                jump_ms=round(jump, 3),
                backwards=backwards,
                rtt_ms=round(est.rtt_ms, 3),
                residual_ms=round(est.residual_ms, 3),
                kept_offset_ms=round(self.clock.offset_ms, 3),
            )
            return self.clock  # flagged, not followed
        self.clock = est
        self.clock_flagged = False
        self.clock_jump_ms = None
        self._backwards_at_last_ping = self.clock_backwards
        return est

    # -- link plumbing -----------------------------------------------------------------------
    def _open(self, timeout: float) -> None:
        cv = _castv2()
        try:
            self.link = cv.CastLink(self.host, self.port, timeout=timeout)
        except cv.CastError as exc:
            self.link = None
            self.closed = True
            raise LinkDown(str(exc)) from exc
        try:
            # A play_at carries a target receiver timestamp: a Nagle/delayed-ACK stall would
            # land the video late by up to 40 ms, most of the acceptance budget. The plain
            # socket is detached by wrap_socket, so the option goes on the TLS one.
            self.link.tls.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        try:
            self.link.connect_platform()
        except Exception as exc:  # noqa: BLE001
            self._close_link()
            self.closed = True
            raise LinkDown(f"CONNECT to {self.host}:{self.port} failed: {exc}") from exc

    def _close_link(self) -> None:
        link, self.link = self.link, None
        if link is not None:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass

    def _adopt_app(self, status: dict, attach: bool = True) -> None:
        cv = _castv2()
        app_id, _name, transport = cv.receiver_app(status)
        if not transport:
            raise LinkDown(f"{self.host} reported app {app_id!r} without a transport id")
        apps = status.get("applications") or [{}]
        self.session_id = apps[0].get("sessionId")
        changed = transport != self.transport
        if changed:
            self._emit("transport_changed", old=self.transport, new=transport)
        self.transport = transport
        self._owns = True
        self._adopt_at = time.time()  # older RECEIVER_STATUSes are stale by definition
        if attach or changed:
            try:
                self.link.connect_transport(transport)
            except Exception as exc:  # noqa: BLE001
                self._link_error(exc)
                raise LinkDown(f"CONNECT to transport {transport} failed: {exc}") from exc
            self._hello_pending = True
        if changed:
            self._emit("attached", app_id=app_id, transport=transport, session_id=self.session_id)

    def _wait_hello(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while self._hello_pending:
            if time.monotonic() >= deadline:
                self._close_link()
                self.closed = True
                raise LinkDown(
                    f"no hello on {C.NS_RECEIVER} from {self.host}:{self.port} within {timeout:.1f}s"
                )
            self._service(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            if self.link is None:
                raise LinkDown(f"link to {self.host} died while waiting for hello")
        return self.hello or {}

    def _service(self, timeout: float = 0.0) -> None:
        """One service pass: answer/send heartbeat, read, harvest, bound the inbox.

        ``timeout`` is a wait for *one* arrival; 0 reads everything already buffered and
        returns. Never called from a second thread: an ``SSL*`` must not be shared (castv2).
        """
        if self.link is None or self.closed:
            return
        try:
            self.link.keepalive(HEARTBEAT_S)
        except Exception as exc:  # noqa: BLE001
            self._link_error(exc)
            return
        self._harvest()
        if self.link is None:
            return
        if timeout <= 0.0:
            for _ in range(4096):
                try:
                    got = self.link.pump(0.0)
                except Exception as exc:  # noqa: BLE001
                    self._link_error(exc)
                    return
                self._harvest()
                if self.link is None or got == 0:
                    break
        else:
            # Short slices rather than one long pump: castv2.pump() waits out its whole timeout
            # even when the reply arrived at the start, which would inflate t3 by up to 20 ms
            # and make the estimator look 10 ms worse than it is.
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0.0:
                    return
                try:
                    got = self.link.pump(min(0.002, left))
                except Exception as exc:  # noqa: BLE001
                    self._link_error(exc)
                    return
                self._harvest()
                if self.link is None:
                    return
                if got:
                    return
        if self.link is not None and self.link.closed:
            self._link_error("socket closed by peer")

    def _wait_reply(self, msg_type: str, seq: int, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            for i, msg in enumerate(self.inbox):
                if msg.get("type") == msg_type and msg.get("seq") == seq:
                    del self.inbox[i]
                    return msg
            if time.monotonic() >= deadline:
                return None
            self._service(timeout=min(0.02, max(0.001, deadline - time.monotonic())))
            if self.link is None:
                return None

    def _harvest(self) -> None:
        if self.link is None:
            return
        recs = self.link.inbox[:]
        del self.link.inbox[:]
        for rec in recs:
            self._on_message(rec)

    def _on_message(self, rec: dict) -> None:
        ns = rec.get("namespace")
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        if ns == NS_CAST_RECEIVER:  # the google receiver namespace: presence only
            if payload.get("type") == "RECEIVER_STATUS":
                self._note_status(payload.get("status") or {}, rec.get("t"))
            return
        if ns == NS_CONNECTION and payload.get("type") == "CLOSE":
            # The receiver closing our virtual connection is silent on the wire otherwise: the
            # platform channel keeps answering GET_STATUS while our page no longer hears us
            # (measured 2026-10-27: 6 minutes of silence 244 s into a session). Say so; the
            # session's silence watchdog reconnects.
            self._emit("transport_closed", source=rec.get("source"), transport=self.transport)
            return
        if ns != C.NS_RECEIVER:  # media/heartbeat/connection noise: not ours
            return
        typ = payload.get("type")
        if typ == C.MSG_HELLO:
            self._on_hello(payload)
        elif typ == C.MSG_STATE:
            self._on_state(payload)
        else:
            self._guard_clock(payload)
        self._queue(payload)

    def _on_hello(self, payload: dict) -> None:
        had_clock = self.clock is not None
        self.hellos += 1
        self.hello = dict(payload)
        self._hello_pending = False
        self._status_misses = 0
        # A new page has a new performance.timeOrigin: every earlier timestamp is meaningless.
        self._last_rx_ms = None
        self.clock = None
        self.clock_flagged = False
        self.clock_jump_ms = None
        self._backwards_at_last_ping = self.clock_backwards
        self._emit(
            "hello",
            ua=payload.get("ua"),
            version=payload.get("version"),
            codecs=payload.get("codecs"),
            seq=payload.get("seq"),
            rx_ms=payload.get("rx_ms"),
            hellos=self.hellos,
        )
        if had_clock:
            self._emit("clock_rebased", reason="hello")
        if self._media_loaded:
            # Not proof of loss (a namespace reconnect keeps the page) but the controller must
            # not assume the media session survived a hello it did not ask for.
            self._emit("session_maybe_lost", hellos=self.hellos)

    def _on_state(self, payload: dict) -> None:
        if payload.get("player_state") == C.PLAYER_IDLE and self._media_loaded:
            self._mark_lost("idle_while_ours")
        sample = self._sample_from_state(payload)
        if sample is not None:
            self.last_state = sample

    def _sample_from_state(self, payload: dict) -> C.Sample | None:
        rx = payload.get("rx_ms")
        media = payload.get("media")
        if not isinstance(rx, (int, float)) or not isinstance(media, (int, float)):
            self._emit("state_malformed", payload=payload)
            return None
        if not self._guard_clock(payload):
            return None  # flagged, not followed
        rate = payload.get("rate", 1.0)
        buffered = payload.get("buffered_end")
        return C.Sample(
            rx_ms=float(rx),
            media=float(media),
            rate=float(rate) if isinstance(rate, (int, float)) else 1.0,
            player_state=str(payload.get("player_state", C.PLAYER_IDLE)),
            buffered_end=float(buffered) if isinstance(buffered, (int, float)) else None,
        )

    def _guard_clock(self, payload: dict) -> bool:
        """Monotone guard: a receiver timestamp that goes backwards is flagged, not used."""
        rx = payload.get("rx_ms")
        if not isinstance(rx, (int, float)):
            return True
        rx = float(rx)
        if self._last_rx_ms is not None and rx < self._last_rx_ms - CLOCK_BACKWARDS_TOL_MS:
            self.clock_backwards += 1
            self._emit(
                "receiver_clock_backwards",
                rx_ms=round(rx, 3),
                last_rx_ms=round(self._last_rx_ms, 3),
                delta_ms=round(rx - self._last_rx_ms, 3),
            )
            return False
        self._last_rx_ms = rx if self._last_rx_ms is None else max(self._last_rx_ms, rx)
        return True

    def _queue(self, payload: dict) -> None:
        if len(self.inbox) >= self.INBOX_MAX:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 256 == 0:
                self._emit("inbox_overflow", dropped=self.dropped, bound=self.INBOX_MAX)
        self.inbox.append(dict(payload))

    def _note_status(self, status: dict, arrived_at: float | None = None) -> None:
        if arrived_at is not None and arrived_at < self._adopt_at:
            # Arrived before our LAUNCH/CONNECT was answered: it describes the device as it
            # was before we owned it (typically the empty answer to the first CONNECT).
            return
        self._status = status or {}
        apps = self._status.get("applications") or []
        if not apps:
            if self._owns:
                self._mark_lost("app_not_running")
            return
        app = apps[0]
        app_id = app.get("appId")
        if app_id != self.app_id:
            self._mark_lost(f"foreign_app:{app_id}")
            return
        new_transport = app.get("transportId")
        if (
            new_transport
            and self.link is not None
            and self.transport is not None
            and new_transport != self.transport
        ):
            self._emit("receiver_restarted", old=self.transport, new=new_transport)
            try:
                self._adopt_app(self._status, attach=True)
            except LinkDown as exc:
                self._emit("reattach_failed", error=str(exc))

    def _mark_lost(self, reason: str) -> None:
        self._owns = False
        if self.lost_reason is None:
            self.lost_reason = reason
            self._emit(
                "device_lost", reason=reason, host=self.host, seq=self.seq, transport=self.transport
            )

    def _link_error(self, exc: Exception | str) -> None:
        name = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        self._emit("link_error", error=name, host=self.host, seq=self.seq)
        self._close_link()
        self.closed = True
        self._mark_lost(f"link_error:{name}")

    # -- read-only device info ---------------------------------------------------------------
    def status(self) -> dict:
        """Last ``RECEIVER_STATUS`` seen, read-only (never a request)."""
        return dict(self._status)


def probe(host: str, port: int = 8008, timeout: float = 4.0) -> dict:
    """Read-only device info (eureka_info). Launches nothing, plays nothing.

    One ``GET /setup/eureka_info`` on the Chromecast's own HTTP port, then a whitelist of
    harmless identity fields. Never raises: an unreachable device is an answer.
    """
    res: dict[str, Any] = {"host": host, "port": port, "read_only": True}
    started = time.monotonic()
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(
            "GET",
            "/setup/eureka_info?options=detail",
            headers={"User-Agent": "nestaudiocast-v020-probe", "Connection": "close"},
        )
        reply = conn.getresponse()
        raw = reply.read(262144)
        res["status"] = reply.status
        res["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        if reply.status != 200:
            res["error"] = f"HTTP {reply.status}"
            return res
        data = json.loads(raw.decode("utf-8", "replace"))
        device = data.get("device_info") or {}
        for key in (
            "name",
            "version",
            "build_version",
            "uptime",
            "ip_address",
            "locale",
            "release_track",
            "signal_level",
            "has_update",
            "time_format",
            "timezone",
        ):
            if key in data:
                res[key] = data[key]
        for key in ("manufacturer", "model_name", "capabilities"):
            if key in device:
                res[key] = device[key]
    except (OSError, http.client.HTTPException, ValueError) as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"
        res["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return res
