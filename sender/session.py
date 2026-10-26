"""The session state machine of the owner app — contracts §1, §3.3, §5, §7, §8.

``idle → resolving → starting → playing ⇄ paused → ended → idle``, plus ``quiet_refused``.

The seams are injected in the constructor and are used through their *contract* types only:
``SenderLink`` (chromecast), ``CastGroup`` (group), ``MediaProxy`` (media), ``Controller``
(control law) — plus the resolver, which task B owns. Nothing here opens a socket, so the whole
machine is exercised offline against ``tests/v020/fakes``.

Two rules this module *enforces* instead of trusting:

1. **No gaps after start.** After the start event, no ``load``, no ``seek`` and no ``play_at``
   leaves the process — and neither does a group ``prepare``/``seek`` — unless a user transport
   action (pause→resume, seek, an intent mirrored from the receiver) opened a one-shot
   re-landing window with a budget. A controller that asks anyway is refused and the refusal is
   logged. That is the acceptance criterion "no gaps after start", in code, whatever the
   control law does.
2. **A lost device ends the session.** ``LostDevice`` or two consecutive non-playing reads of
   our own receiver (A36's lesson: one stale read is not evidence) stop the group, log
   ``device_lost`` and go ``idle``. They never relaunch: a new share is required (§3.3).

A new share while a session runs stops the old one cleanly first (acceptance: "new video").
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from v020 import contracts as C
from v020.contracts import OWNER_SENDER

from .group_lock import (
    GroupLock, clear_session_record, session_record_path, stop_receiver_idle,
    write_session_record,
)
from .quiet import DECISION_OK, QuietWindow

#: States in which a session is still doing something.
LIVE_STATES = (C.STATE_RESOLVING, C.STATE_STARTING, C.STATE_PLAYING, C.STATE_PAUSED)
#: States a control action can act on.
CONTROLLABLE = (C.STATE_PLAYING, C.STATE_PAUSED)

DEFAULT_TICK_S = 0.25           # ≤ 1 s poll, so a foreign takeover is seen within one poll (§3.3)
TRANSITION_S = 5.0              # a re-landing window after a user transport action
TRANSITION_BUDGET = 2           # one group re-landing + one play_at, per window (§1)
ENDED_LINGER_S = 2.0            # how long `ended` is visible before `idle`
REFUSE_LOG_EVERY_S = 10.0       # throttle for the guard's own log lines (a hostile controller)
START_LEAD_S = 0.2              # only used by the stopgap plan, never by the real controller

#: Commands that move a playhead: allowed before the start event, or inside a re-landing window.
GUARDED = (C.MSG_LOAD, C.MSG_SEEK, C.MSG_PLAY_AT)
#: Group actions that move a playhead (the group's own names, plus the `target: group` form).
GUARDED_GROUP = ("prepare", "load", "seek", "group_prepare", "group_seek")

_GROUP_ALIASES = {
    "play": "play", "group_play": "play", "resume": "play",
    "pause": "pause", "group_pause": "pause",
    "stop": "stop", "group_stop": "stop",
    "seek": "seek", "group_seek": "seek", "land": "seek",
    "prepare": "prepare", "group_prepare": "prepare", "load": "prepare",
    "volume": "volume", "set_volume": "volume",
}

_HTTP_URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


class ShareError(ValueError):
    """The share cannot even be attempted (bad URL, bad start) — HTTP 400."""


class ControlError(RuntimeError):
    """A control action is not valid now (or at all) — HTTP ``code`` (409 by default)."""

    def __init__(self, message: str, *, code: int = 409, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


@dataclass
class Session:
    """Everything we know about one share. One instance at a time."""

    id: str
    url: str
    mode: str = "normal"                  # normal | measure | video_only
    start_at: float = 0.0
    created: float = 0.0
    state: str = C.STATE_RESOLVING
    video_id: str | None = None
    sid: str | None = None
    title: str | None = None
    duration_s: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    last_transport_at: float | None = None
    last_transport_wall: float | None = None
    last_error: str | None = None
    rate: float | None = None
    group_prepared: bool = False
    receiver_launched: bool = False

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def audible(self) -> bool:
        """True when this session is allowed to load the group (§7 ``video_only``)."""
        return self.mode != "video_only"


class SessionManager:
    """The one session of the owner app, driven by :meth:`tick` and by HTTP calls."""

    def __init__(self, *, link, group, controller, proxy, config, log,
                 resolver: Callable[..., Any] | None = None,
                 quiet: QuietWindow | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 spawn: Callable[[Callable[[], None]], Any] | None = None,
                 lock: GroupLock | None = None,
                 state_dir: str | None = None,
                 tick_s: float = DEFAULT_TICK_S,
                 transition_s: float = TRANSITION_S) -> None:
        self._link = link
        self._group = group
        self._controller = controller
        self._proxy = proxy
        self._config = config
        self._log = log
        self._resolver = resolver
        self._quiet = quiet or config.quiet
        self._clock = clock
        self._wall = wall
        self._spawn = spawn or _spawn_thread
        self._tick_s = float(tick_s)
        self._transition_s = float(transition_s)
        self._state_dir = state_dir if state_dir is not None else config.state_dir
        self._record_path = session_record_path(self._state_dir)
        self._lock = threading.RLock()
        self._group_lock = lock or GroupLock(
            path=_group_lock_path(self._state_dir), log=log)
        self._session: Session | None = None
        self._ended_at: float | None = None
        self._start_done = False
        self._transition_until = 0.0
        self._reload_budget = 0
        self._errors: list[tuple[float, float]] = []      # (monotonic, ms)
        self._refusals = 0
        self._last_refusal_log = -1e9
        self._idle_video_reads = 0
        self._last_error: str | None = None
        self._level: float | None = None      # last level we commanded (no getter in the seam)
        self._controller_missing = False

    # =====================================================================================
    # share
    # =====================================================================================
    def share(self, url: str, *, start: float | None = None, measure: bool = False) -> dict:
        """Begin a session; returns ``{"session_id", "state"}`` (HTTP 202)."""
        if not isinstance(url, str) or not url.strip():
            raise ShareError("url is required")
        url = url.strip()
        if not _HTTP_URL.match(url):
            raise ShareError("url must be an absolute http(s) URL")
        start_at = 0.0 if start is None else float(start)
        if start_at < 0.0:
            raise ShareError("start must be >= 0 media seconds")
        if not isinstance(measure, bool):
            raise ShareError("measure must be a boolean")

        with self._lock:
            decision = self._quiet.decision(self._wall())
            if decision == "refuse":
                return self._refuse_for_quiet(url, start_at, measure)
            # `video_only` wins over `measure`: outside the allowed window the group is never
            # loaded, whatever the share asked for (§7).
            mode = "video_only" if decision != DECISION_OK else ("measure" if measure else "normal")

            # A new share while playing stops the old session cleanly first (§5, "new video").
            if self._session is not None and self._session.live:
                self._stop(self._session, "new_share", state=C.STATE_ENDED)
                self._session = None

            if mode != "video_only":
                if not self._group_lock.acquire():
                    raise ControlError(
                        "the group is owned by another cast-sender (or a live lock)", code=409,
                        kind="group_busy")
                self._group_lock.start_heartbeat()

            session = Session(id=f"s{uuid.uuid4().hex[:10]}", url=url, mode=mode,
                              start_at=start_at, created=self._clock())
            self._session = session
            self._ended_at = None
            self._start_done = False
            self._transition_until = 0.0
            self._reload_budget = 0
            self._errors.clear()
            self._idle_video_reads = 0
            self._last_error = None
            self._level = None
            self._log("share_accepted", session_id=session.id, mode=session.mode,
                      start=round(start_at, 3), measure=bool(measure),
                      quiet=self._quiet.decision(self._wall()))
            accepted_state = session.state          # contracts §5: 202 {"state": "resolving"}
            self._spawn(lambda: self._begin(session))
            return {"session_id": session.id, "state": accepted_state}

    def _refuse_for_quiet(self, url: str, start_at: float, measure: bool) -> dict:
        """``refuse`` policy: the share is answered ``quiet_refused`` and nothing plays (§7)."""
        session = Session(id=f"s{uuid.uuid4().hex[:10]}", url=url, start_at=start_at,
                          created=self._clock(), state=C.STATE_QUIET_REFUSED,
                          mode="video_only")
        running = self._session is not None and self._session.live
        if not running:
            # Show the refusal in /api/status; a session that is already running is NOT
            # stopped and NOT replaced (the rule is about starting, §7).
            self._session = session
        self._log("share_refused", session_id=session.id, reason="quiet_hours",
                  policy=self._quiet.policy, running_session=running,
                  quiet=self._quiet.describe(self._wall()))
        return {"session_id": session.id, "state": session.state}

    # =====================================================================================
    # the blocking start sequence (runs on a worker thread; never under the long lock)
    # =====================================================================================
    def _begin(self, session: Session) -> None:
        try:
            resolved = self._resolve(session)
            if resolved is None or self._aborted(session):
                return
            session.video_id = getattr(resolved, "video_id", None)
            session.title = getattr(resolved, "title", None)
            session.duration_s = getattr(resolved, "duration_s", None)
            session.sid = self._open_media(session, resolved)
            if session.sid is None:
                return
            self._log("media_resolved", session_id=session.id, video_id=session.video_id,
                      sid=session.sid, duration_s=session.duration_s, mode=session.mode)
            with self._lock:
                if session.live:
                    session.state = C.STATE_STARTING

            # The receiver first: this is the task allowed to launch an app on .80, and it
            # does its own read-only status check before launching (brief's tonight rule).
            try:
                self._link.launch()
                self._link.connect()
                session.receiver_launched = True
            except NotImplementedError:
                return self._fail(session, "sender_link_unavailable",
                                  "the receiver link is a stub (task D)")
            except Exception as exc:  # noqa: BLE001 - LostDevice included, on purpose
                return self._fail(session, "receiver_failed", f"{type(exc).__name__}: {exc}")
            self._log("receiver_ready", session_id=session.id,
                      host=self._config.chromecast, app_id=self._config.app_id)
            if self._aborted(session):
                return

            group_ready = False
            if session.audible:
                try:
                    self._group.connect()
                    group_ready = bool(self._group.prepare(
                        self._audio_url(session), "audio/mp4", session.start_at))
                    session.group_prepared = group_ready
                except NotImplementedError:
                    return self._fail(session, "group_unavailable", "the group seam is a stub "
                                                                   "(task E)")
                except Exception as exc:  # noqa: BLE001 - UnsafeGroup and friends
                    return self._fail(session, "group_failed", f"{type(exc).__name__}: {exc}")
                self._log("group_prepared", session_id=session.id, ok=group_ready,
                          at=round(session.start_at, 3))

            if self._aborted(session):
                return
            with self._lock:
                self._write_record(session)
            now = self._clock()
            commands = self._start_plan(session, now, group_ready)
            self._apply(session, commands, now, phase="start")
            with self._lock:
                if self._aborted(session):
                    return
                self._start_done = True
                self._reload_budget = 0
                self._transition_until = 0.0
                session.started_at = now
                self._note_transport(now)
                session.state = C.STATE_PLAYING
            self._log("session_started", session_id=session.id, video_id=session.video_id,
                      mode=session.mode, group_ready=group_ready,
                      start=round(session.start_at, 3), state=session.state)
        except Exception as exc:  # noqa: BLE001 - a session must never take the server down
            self._fail(session, "start_failed", f"{type(exc).__name__}: {exc}")

    def _resolve(self, session: Session):
        resolver = self._resolver
        if resolver is None:
            from v020.resolve import resolve as resolver  # the real one, task B
        try:
            return resolver(session.url, max_height=self._config.max_height)
        except NotImplementedError:
            self._fail(session, "resolver_unavailable", "the media resolver is a stub (task B)")
        except Exception as exc:  # noqa: BLE001 - ResolveError, network, ...
            self._fail(session, "resolve_failed", f"{type(exc).__name__}: {exc}")
        return None

    def _open_media(self, session: Session, resolved) -> str | None:
        try:
            return str(self._proxy.open_session(resolved))
        except NotImplementedError:
            self._fail(session, "media_proxy_unavailable", "the media proxy is a stub (task B)")
        except Exception as exc:  # noqa: BLE001
            self._fail(session, "media_open_failed", f"{type(exc).__name__}: {exc}")
        return None

    def _start_plan(self, session: Session, now: float, group_ready: bool) -> list:
        """The controller's single start event, or a documented stopgap when it is a stub."""
        try:
            video_ready = now
            group_ready_at = now + START_LEAD_S if group_ready else now
            plan = self._controller.start(now, video_ready, group_ready_at,
                                          lead_s=START_LEAD_S)
            return list(plan or [])
        except NotImplementedError:
            self._controller_missing = True
            self._log("controller_unavailable", session_id=session.id,
                      fallback="stopgap_start", task="F")
            return self._stopgap_start(session, group_ready)

    def _stopgap_start(self, session: Session, group_ready: bool) -> list:
        """Load parked, release the group, then tell the receiver where to be.

        Not a sync plan: it exists so the skeleton starts end to end while task F is a stub,
        and it says so in the log. The real controller replaces it by returning a plan.
        """
        plan = [C.Command(kind=C.MSG_LOAD, payload={
            "url": self._mpd_url(session), "content_type": "application/dash+xml",
            "media": session.start_at, "autoplay": False})]
        if group_ready and session.audible:
            plan.append(C.Command(kind="play", payload={"target": "group"}))
        plan.append(C.Command(kind=C.MSG_PLAY_AT, payload={
            "rx_ms": self._receiver_ms(), "media": session.start_at}))
        return plan

    # =====================================================================================
    # one observation tick
    # =====================================================================================
    def tick(self) -> None:
        """Drain the link, observe both sides, let the control law speak, guard its output."""
        with self._lock:
            session = self._session
            if session is None:
                return
            now = self._clock()
            if session.state == C.STATE_ENDED:
                if self._ended_at is not None and now - self._ended_at >= ENDED_LINGER_S:
                    self._session = None
                    self._ended_at = None
                    self._log("session_idle", last_session_id=session.id)
                return
            if not session.live:
                return

            try:
                messages = self._link.drain()
            except NotImplementedError:
                messages = []
            except Exception as exc:  # noqa: BLE001 - LostDevice, link errors
                return self._on_lost(session, f"drain: {type(exc).__name__}: {exc}")
            for message in messages or []:
                self._handle_message(session, message, now)

            if not session.live:
                return
            try:
                ours = self._link.is_ours()
            except NotImplementedError:
                ours = True
            except Exception as exc:  # noqa: BLE001
                return self._on_lost(session, f"is_ours: {type(exc).__name__}: {exc}")
            if not ours:
                return self._on_lost(session, "foreign_app")

            video = self._safe_latest()
            group_sample = None
            if session.audible and session.group_prepared:
                try:
                    group_sample = self._group.sample()
                except Exception:  # noqa: BLE001 - a group read must never end the session
                    group_sample = None
            self._observe(session, video, group_sample, now)
            if not session.live:
                return

            commands = []
            if session.state in CONTROLLABLE:
                try:
                    commands = list(self._controller.step(now, video, group_sample) or [])
                except NotImplementedError:
                    if not self._controller_missing:
                        self._controller_missing = True
                        self._log("controller_unavailable", session_id=session.id,
                                  fallback="no_corrections", task="F")
                    commands = []
                except Exception as exc:  # noqa: BLE001
                    self._log("controller_error", session_id=session.id,
                              error=f"{type(exc).__name__}: {exc}")
                    commands = []
            self._apply(session, commands, now, phase="steady")

    def _handle_message(self, session: Session, message, now: float) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == C.MSG_ENDED:
            self._end(session, "video_ended")
        elif kind == C.MSG_INTENT:
            self._mirror_intent(session, message, now)
        elif kind == C.MSG_ERROR:
            session.last_error = f"receiver: {message.get('code')}: {message.get('message')}"
            self._last_error = session.last_error
            self._log("receiver_error", session_id=session.id, code=message.get("code"),
                      message=message.get("message"))

    def _mirror_intent(self, session: Session, message: dict, now: float) -> None:
        """§3.2: the receiver forwards what another sender pressed; we mirror it on the group."""
        action = str(message.get("action") or "")
        self._log("intent", session_id=session.id, action=action,
                  media=message.get("media"))
        if action == C.INTENT_PAUSE:
            self._group_safe(session, "pause")
            with self._lock:
                if session.state == C.STATE_PLAYING:
                    session.state = C.STATE_PAUSED
                    self._note_transport(now)
        elif action == C.INTENT_PLAY:
            with self._lock:
                self._note_transport(now)
                self._open_transition(now, "intent_resume")
            self._group_safe(session, "play")
            with self._lock:
                session.state = C.STATE_PLAYING
        elif action == C.INTENT_SEEK:
            position = _as_float(message.get("media"))
            if position is None:
                return
            with self._lock:
                self._note_transport(now)
                self._open_transition(now, "intent_seek")
            self._group_safe(session, "seek", media=position)

    def _observe(self, session: Session, video, group_sample, now: float) -> None:
        if video is not None:
            if video.rate:
                session.rate = float(video.rate)
            if video.player_state == C.PLAYER_ENDED:
                self._end(session, "video_ended")
                return
            if video.player_state == C.PLAYER_ERROR:
                session.last_error = "receiver reported player_state=error"
                self._last_error = session.last_error
                self._log("receiver_error", session_id=session.id, reason="player_state")
            # Two consecutive IDLE reads of our own app = the session is gone (§3.3, A36).
            if session.state == C.STATE_PLAYING and video.player_state == C.PLAYER_IDLE:
                self._idle_video_reads += 1
                if self._idle_video_reads >= 2:
                    return self._on_lost(session, "receiver_idle")
            else:
                self._idle_video_reads = 0

        if video is not None and group_sample is not None:
            error_ms = C.digital_error(group_sample.media, video.media,
                                       self._config.delta_v2) * 1000.0
            self._errors.append((now, error_ms))
            horizon = now - C.STEADY_WINDOW_S
            self._errors = [item for item in self._errors if item[0] >= horizon]

    # =====================================================================================
    # commands, and the guard that enforces "no gaps after start"
    # =====================================================================================
    def _apply(self, session: Session, commands, now: float, *, phase: str) -> None:
        for command in commands or []:
            if not session.live:
                return
            kind = getattr(command, "kind", None)
            payload: dict = dict(getattr(command, "payload", {}) or {})
            if not kind:
                continue
            target = payload.pop("target", None)
            to_group = target == "group" or kind not in C.CONTROLLER_MESSAGES
            if not to_group:
                if kind in GUARDED and not self._reload_allowed(session, kind, now):
                    continue
                if kind == C.MSG_PLAY_AT and not payload.get("rx_ms"):
                    # A plan may leave the clock to the session: rx_ms belongs to the receiver's
                    # base and only this side has the estimate.
                    payload["rx_ms"] = self._receiver_ms()
                if kind == C.MSG_LOAD and not payload.get("url") and session.sid:
                    payload["url"] = self._mpd_url(session)
                    payload.setdefault("content_type", "application/dash+xml")
                try:
                    self._link.send(kind, **payload)
                except NotImplementedError:
                    self._log("sender_link_unavailable", session_id=session.id, kind=kind)
                except Exception as exc:  # noqa: BLE001
                    self._on_lost(session, f"send({kind}): {type(exc).__name__}: {exc}")
                    return
            else:
                self._group_command(session, kind, payload, now, phase)

    def _group_command(self, session: Session, kind: str, payload: dict, now: float,
                       phase: str) -> None:
        name = _GROUP_ALIASES.get(kind)
        if name is None:
            self._log("command_unknown", session_id=session.id, kind=kind)
            return
        if not session.audible:
            self._log("group_action_refused", session_id=session.id, kind=name,
                      reason="video_only")
            return
        guarded = name in ("prepare", "seek")
        if guarded and not self._reload_allowed(session, name, now):
            return
        try:
            if name == "prepare":
                at = _as_float(payload.get("media", payload.get("at")))
                self._group.prepare(payload.get("url") or self._audio_url(session),
                                    payload.get("content_type") or "audio/mp4",
                                    session.start_at if at is None else at)
            elif name == "play":
                self._group.play()
            elif name == "pause":
                self._group.pause()
            elif name == "stop":
                self._group.stop()
            elif name == "seek":
                at = _as_float(payload.get("media", payload.get("position")))
                if at is None:
                    raise ControlError("seek needs a position", code=400, kind="invalid")
                self._group.seek(at)
            elif name == "volume":
                level = _as_float(payload.get("level"))
                if level is not None and level <= 0.0:
                    raise ControlError("refusing a level of 0: mute instead (A37)", code=400,
                                       kind="invalid")
                self._group.set_volume(level, payload.get("muted"))
        except ControlError as exc:
            self._log("group_action_refused", session_id=session.id, kind=name,
                      reason=str(exc))
        except NotImplementedError:
            self._log("group_unavailable", session_id=session.id, kind=name)
        except Exception as exc:  # noqa: BLE001 - a group error must not kill the server
            self._log("group_error", session_id=session.id, kind=name,
                      error=f"{type(exc).__name__}: {exc}")

    def _reload_allowed(self, session: Session, kind: str, now: float) -> bool:
        """The guard of §1: playhead moves only before the start, or in a one-shot window."""
        if not self._start_done:
            return True
        if now < self._transition_until and self._reload_budget > 0:
            self._reload_budget -= 1
            self._log("reload_allowed", session_id=session.id, kind=str(kind),
                      remaining=self._reload_budget)
            return True
        self._refuse(session, kind, now)
        return False

    def _refuse(self, session: Session, kind: str, now: float) -> None:
        self._refusals += 1
        if now - self._last_refusal_log >= REFUSE_LOG_EVERY_S:
            self._last_refusal_log = now
            self._log("reload_refused", session_id=session.id, kind=str(kind),
                      total=self._refusals,
                      reason="after the start event only rate trims are allowed (§1)")

    # =====================================================================================
    # control (HTTP)
    # =====================================================================================
    def control(self, action: str, *, position: float | None = None,
                level: float | None = None, muted: bool | None = None) -> dict:
        if action not in C.ACTIONS:
            raise ControlError(f"unknown action {action!r}", code=400, kind="invalid")
        # A malformed body is a 400 whether or not a session exists; only a well-formed action
        # can be told "not now" (409).
        if action == C.ACTION_SEEK:
            target = _as_float(position)
            if target is None:
                raise ControlError("seek needs a numeric position", code=400, kind="invalid")
            if target < 0.0:
                raise ControlError("position must be >= 0", code=400, kind="invalid")
        # A field that the action does not use is a client bug, not something to ignore.
        if action != C.ACTION_SEEK and position is not None:
            raise ControlError(f"{action} does not take a position", code=400, kind="invalid")
        if action != C.ACTION_VOLUME and (level is not None or muted is not None):
            raise ControlError(f"{action} does not take level/muted", code=400, kind="invalid")
        level_v = None
        if action == C.ACTION_VOLUME:
            level_v = self._validate_volume(level, muted)
        with self._lock:
            session = self._session
            now = self._clock()
            if action == C.ACTION_STOP:
                if session is None or not session.live:
                    self._log("control_stop_idle")
                    return {"ok": True, "state": C.STATE_IDLE}
                self._stop(session, "control_stop", state=C.STATE_ENDED)
                return {"ok": True, "state": session.state}
            if session is None or session.state not in CONTROLLABLE:
                state = C.STATE_IDLE if session is None else session.state
                raise ControlError(f"{action}: no active session (state={state})")
            if action == C.ACTION_VOLUME:
                return self._volume(session, level_v, muted)
            if action == C.ACTION_PAUSE:
                if session.state == C.STATE_PAUSED:
                    return {"ok": True, "state": session.state}
                self._apply(session, self._ensure(self._call("on_pause", now), C.MSG_PAUSE, {}),
                            now, phase="user")
                self._group_safe(session, "pause")
                session.state = C.STATE_PAUSED
                self._note_transport(now)
                self._log("control_pause", session_id=session.id)
                return {"ok": True, "state": session.state}
            if action == C.ACTION_PLAY:
                if session.state == C.STATE_PLAYING:
                    return {"ok": True, "state": session.state}
                self._note_transport(now)
                self._open_transition(now, "resume", budget=TRANSITION_BUDGET)
                current = self._group_position() or 0.0
                self._apply(session,
                            self._ensure(self._resume_plan(session, now, current),
                                         C.MSG_PLAY_AT, {"media": current}), now, phase="user")
                session.state = C.STATE_PLAYING
                self._log("control_play", session_id=session.id,
                          position=round(current, 3))
                return {"ok": True, "state": session.state}
            if action == C.ACTION_SEEK:
                self._note_transport(now)
                self._open_transition(now, "seek", budget=TRANSITION_BUDGET)
                self._apply(session,
                            self._ensure(self._seek_plan(session, now, target), C.MSG_SEEK,
                                         {"media": target}), now, phase="user")
                if session.state == C.STATE_PAUSED:
                    pass  # a seek while paused stays paused; the position still moves
                self._log("control_seek", session_id=session.id, position=round(target, 3))
                return {"ok": True, "state": session.state}
        raise ControlError(f"action {action!r} is not implemented", code=400, kind="invalid")

    @staticmethod
    def _validate_volume(level, muted) -> float | None:
        level_v = _as_float(level)
        if level is not None and level_v is None:
            raise ControlError("level must be a number", code=400, kind="invalid")
        if level_v is not None and not (0.0 <= level_v <= 1.0):
            raise ControlError("level must be within 0…1", code=400, kind="invalid")
        if level_v is not None and level_v == 0.0:
            # A37: muting is the way to silence; a level of 0 stays silent after an unmute.
            raise ControlError("refusing a level of 0: send muted=true instead", code=400,
                               kind="invalid")
        if level_v is None and muted is None:
            raise ControlError("volume needs level and/or muted", code=400, kind="invalid")
        if muted is not None and not isinstance(muted, bool):
            raise ControlError("muted must be a boolean", code=400, kind="invalid")
        return level_v

    def _volume(self, session: Session, level: float | None, muted: bool | None) -> dict:
        if not session.audible:
            raise ControlError("volume: this session never loads the group (video_only)")
        level_v = level if level is not None else self._level
        if level_v is None:
            raise ControlError(
                "muted needs a known level: CastGroup has no volume getter, so send level once "
                "(then mute-only calls work)", code=409, kind="no_known_level")
        try:
            self._group.set_volume(float(level_v), muted)
        except NotImplementedError:
            raise ControlError("the group seam is a stub (task E)", code=503,
                               kind="unavailable") from None
        except Exception as exc:  # noqa: BLE001
            raise ControlError(f"group refused the volume: {exc}", code=502,
                               kind="group_error") from exc
        if level is not None:
            self._level = float(level_v)
        self._log("control_volume", session_id=session.id, level=level_v, muted=muted)
        return {"ok": True, "state": session.state}

    def _call(self, name: str, *args) -> list:
        """Call an optional controller hook; a stub returns nothing (the group still acts)."""
        method = getattr(self._controller, name, None)
        if method is None:
            return []
        try:
            return list(method(*args) or [])
        except NotImplementedError:
            self._log("controller_unavailable", fallback=name, task="F")
            return []
        except Exception as exc:  # noqa: BLE001
            self._log("controller_error", hook=name, error=f"{type(exc).__name__}: {exc}")
            return []

    def _resume_plan(self, session: Session, now: float, group_position: float) -> list:
        plan = self._call("on_resume", now, group_position)
        if plan:
            return plan
        return [C.Command(kind="play", payload={"target": "group"}),
                C.Command(kind=C.MSG_PLAY_AT,
                          payload={"rx_ms": self._receiver_ms(), "media": group_position})]

    def _seek_plan(self, session: Session, now: float, position: float) -> list:
        plan = self._call("on_seek", now, position)
        if plan:
            return plan
        return [C.Command(kind="group_seek", payload={"media": position}),
                C.Command(kind=C.MSG_SEEK, payload={"media": position})]

    @staticmethod
    def _ensure(plan: list, kind: str, payload: dict) -> list:
        """A user transport action must reach the video even if the plan forgot it.

        Pause, resume and seek are the three moments §1 allows a re-landing; leaving the video
        behind would put the two sides out of step for the rest of the session, so the session
        adds the command rather than trusting the control law to have included it.
        """
        plan = list(plan or [])
        if any(getattr(command, "kind", None) == kind for command in plan):
            return plan
        plan.append(C.Command(kind=kind, payload=dict(payload)))
        return plan

    def _group_safe(self, session: Session, name: str, **payload) -> None:
        self._group_command(session, name, dict(payload), self._clock(), phase="user")

    # =====================================================================================
    # ending, failing, losing
    # =====================================================================================
    def _stop(self, session: Session, reason: str, *, state: str) -> None:
        """Clean stop: group, receiver, media session, lock, record (§8)."""
        with self._lock:
            self._stop_locked(session, reason, state=state)

    def _stop_locked(self, session: Session, reason: str, *, state: str) -> None:
        self._log("session_stopping", session_id=session.id, reason=reason)
        if session.audible and session.group_prepared:
            try:
                self._group.stop()
            except NotImplementedError:
                self._log("group_unavailable", session_id=session.id, step="stop")
            except Exception as exc:  # noqa: BLE001
                self._log("group_error", session_id=session.id, step="stop",
                          error=f"{type(exc).__name__}: {exc}")
        if session.receiver_launched:
            stop_receiver_idle(self._link, self._log)
        if session.sid:
            try:
                self._proxy.close_session(session.sid)
            except Exception as exc:  # noqa: BLE001
                self._log("media_close_failed", session_id=session.id,
                          error=f"{type(exc).__name__}: {exc}")
        self._close_bookkeeping(session, state)
        session.ended_at = self._clock()
        self._ended_at = session.ended_at
        self._log("session_ended", session_id=session.id, reason=reason, state=state)

    def _end(self, session: Session, reason: str) -> None:
        self._stop(session, reason, state=C.STATE_ENDED)

    def _fail(self, session: Session, code: str, message: str) -> None:
        with self._lock:
            self._last_error = f"{code}: {message}"
            session.last_error = self._last_error
            self._log("session_failed", session_id=session.id, code=code, error=message)
            self._stop_locked(session, code, state=C.STATE_ENDED)

    def _on_lost(self, session: Session, why: str) -> None:
        """§3.3: stop the group, log, go idle — and never relaunch by ourselves."""
        with self._lock:
            self._last_error = f"device_lost: {why}"
            session.last_error = self._last_error
            self._log("device_lost", session_id=session.id, why=why)
            self._stop_locked(session, "device_lost", state=C.STATE_ENDED)
            session.state = C.STATE_IDLE
            self._session = None      # a lost device goes straight to idle, no linger

    def _close_bookkeeping(self, session: Session, state: str) -> None:
        session.state = state
        self._end_session_lock(session)
        clear_session_record(self._record_path)
        self._start_done = False
        self._reload_budget = 0
        self._transition_until = 0.0

    def _end_session_lock(self, session: Session) -> None:
        try:
            self._group_lock.stop_heartbeat()
            self._group_lock.release()
        except Exception as exc:  # noqa: BLE001 - a lock error must not block the stop
            self._log("lock_error", error=f"{type(exc).__name__}: {exc}")

    # =====================================================================================
    # status (§5)
    # =====================================================================================
    def status(self) -> dict:
        with self._lock:
            session = self._session
            now = self._clock()
            video = self._safe_latest() if session is not None and session.live else None
            group_sample = None
            if session is not None and session.live and session.audible and session.group_prepared:
                try:
                    group_sample = self._group.sample()
                except Exception:  # noqa: BLE001
                    group_sample = None
            position_video = None if video is None else round(float(video.media), 3)
            position_group = None if group_sample is None else round(float(group_sample.media), 3)
            error_ms = None
            if position_video is not None and position_group is not None:
                error_ms = _no_negative_zero(round(C.digital_error(
                    position_group, position_video, self._config.delta_v2) * 1000.0, 1))
            steady_since = self._steady_since(now)
            try:
                group_owner = self._group_lock.owner_name()
            except Exception:  # noqa: BLE001
                group_owner = None
            state = C.STATE_IDLE if session is None else session.state
            return {
                "state": state,
                "session_id": None if session is None else session.id,
                "video_id": None if session is None else session.video_id,
                "position_video": position_video,
                "position_group": position_group,
                "digital_error_ms": error_ms,
                "p95_ms": self._p95(),
                "rate": None if video is None or not video.rate else round(float(video.rate), 6),
                "steady_since": steady_since,
                "quiet": self._quiet.describe(self._wall()),
                "group_owner": group_owner,
                "delta_v2": self._config.delta_v2,
                "last_event": getattr(self._log, "last_event", None),
                "last_error": self._last_error,
            }

    def _p95(self) -> float | None:
        if not self._errors:
            return None
        values = sorted(value for _, value in self._errors)
        index = max(0, min(len(values) - 1, int(round(0.95 * len(values))) - 1))
        return _no_negative_zero(round(values[index], 1))

    def _steady_since(self, now: float) -> float | None:
        session = self._session
        if session is None or session.state != C.STATE_PLAYING:
            return None
        if session.last_transport_at is None:
            return None
        if now - session.last_transport_at < C.STEADY_AFTER_S:
            return None
        wall = session.last_transport_wall or self._wall()
        return round(wall + C.STEADY_AFTER_S, 3)

    # =====================================================================================
    # helpers
    # =====================================================================================
    def _note_transport(self, now: float) -> None:
        session = self._session
        if session is not None:
            session.last_transport_at = now
            session.last_transport_wall = self._wall()
        self._transition_until = 0.0
        self._reload_budget = 0

    def _open_transition(self, now: float, why: str, budget: int = TRANSITION_BUDGET) -> None:
        self._transition_until = now + self._transition_s
        self._reload_budget = int(budget)
        self._log("transition_open", why=why, budget=int(budget), window_s=self._transition_s,
                  session_id=None if self._session is None else self._session.id)

    def _aborted(self, session: Session) -> bool:
        if self._session is not session or not session.live:
            self._log("session_aborted", session_id=session.id, phase="start")
            self._release_after_abort(session)
            return True
        return False

    def _release_after_abort(self, session: Session) -> None:
        """A stop arrived mid-start: leave nothing running.

        The lock is not touched here: whoever stopped this session already released it (and a
        replacement share may hold it by now).
        """
        if session.receiver_launched:
            stop_receiver_idle(self._link, self._log)
        if session.sid:
            try:
                self._proxy.close_session(session.sid)
            except Exception:  # noqa: BLE001
                pass
        clear_session_record(self._record_path)

    def _safe_latest(self):
        try:
            return self._link.latest()
        except NotImplementedError:
            return None
        except Exception:  # noqa: BLE001
            return None

    def _group_position(self) -> float | None:
        try:
            sample = self._group.sample()
        except Exception:  # noqa: BLE001
            return None
        return None if sample is None else float(sample.media)

    def _mpd_url(self, session: Session) -> str:
        return (f"http://{self._config.http_bind}:{self._config.http_port}"
                f"{C.PATH_MEDIA_PREFIX}{session.sid}/manifest.mpd")

    def _audio_url(self, session: Session) -> str:
        if session.mode == "measure":
            # contracts §5 `measure: true`: the group plays the click train, the video
            # keeps its own track. The asset is served read-only next to /media/*.
            return (f"http://{self._config.http_bind}:{self._config.http_port}"
                    f"/assets/measure/click_train_2310.m4a")
        try:
            return str(self._proxy.audio_url_for(session.sid))
        except Exception:  # noqa: BLE001
            return (f"http://{self._config.http_bind}:{self._config.http_port}"
                    f"{C.PATH_MEDIA_PREFIX}{session.sid}/audio.m4a")

    def _receiver_ms(self) -> float:
        """A controller-clock instant expressed on the receiver's clock (§3 clock convention).

        ``rx_ms = performance.timeOrigin + performance.now()``; the controller's base is its own
        monotonic clock, and ``ClockEstimate.offset_ms`` is what maps one onto the other. Using
        the wall clock here instead would land the playhead 56 years away — the fakes make that
        mistake visible at once, which is why they use a monotonic base.
        """
        offset = 0.0
        try:
            estimate = self._link.ping(n=3)
            offset = float(getattr(estimate, "offset_ms", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 - no clock estimate is a valid state
            offset = 0.0
        return self._clock() * 1000.0 + offset

    def _write_record(self, session: Session) -> None:
        """What a crash restart (or ExecStopPost) needs to undo, and nothing else (§8)."""
        record = {
            "owner": OWNER_SENDER,
            "pid": self._process_id(),
            "session_id": session.id,
            "sid": session.sid,
            "video_id": session.video_id,
            "mode": session.mode,
            "group_prepared": bool(session.group_prepared),
            "receiver_launched": bool(session.receiver_launched),
            "started_at": session.started_at or self._clock(),
            "mutes": [],          # CastGroup has no volume getter: we mute nothing (see notes)
        }
        try:
            write_session_record(self._record_path, record)
        except OSError as exc:
            self._log("session_record_failed", error=f"{type(exc).__name__}: {exc}")

    def _process_id(self) -> int:
        return os.getpid()

    # =====================================================================================
    # shutdown
    # =====================================================================================
    def close(self) -> None:
        """Service shutdown / ExecStopPost: stop what we started, then let go of the lock."""
        with self._lock:
            session = self._session
            if session is not None and session.live:
                self._stop(session, "service_stop", state=C.STATE_ENDED)
            else:
                self._end_session_lock(session or Session(id="none", url=""))
            self._group_lock.stop_heartbeat()
            self._log("sender_closed")


def _no_negative_zero(value: float) -> float:
    """``-0.0`` in JSON looks like a bug; it is the same number as 0.0."""
    return 0.0 if value == 0 else value


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _group_lock_path(state_dir: str) -> str:
    """``state/group.owner`` of §7 — the state dir is where the ``state/`` prefix points."""
    return os.path.join(state_dir, os.path.basename(C.OWNER_FILE))


def _spawn_thread(target: Callable[[], None]):
    thread = threading.Thread(target=target, name="cast-sender-session", daemon=True)
    thread.start()
    return thread
