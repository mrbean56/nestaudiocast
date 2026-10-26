"""The kitchen group, as v0.2 uses it: parked, then released, then left alone (contracts §1).

Behaviour that must survive from v0.1 — reused here, not rewritten (`relay/cast_relay.py` is the
reference, and is neither imported nor edited):

* ``lib/groupguard.require_safe_group`` on **every** connect and reconnect (A6, evidence/09): a
  group that contains a video device takes the TV over the moment a session starts on it.
* keepalive at every poll (A23, evidence/22): a link nobody reads stops answering the receiver's
  PINGs and is closed by it after ~24 s.
* park in PAUSE, then release with one PLAY (evidence/27: PAUSE→SEEK→PLAY σ 30 ms against 202 ms
  for a LOAD); the group is never reloaded once it plays.
* a single stale read must not start a reload: two consecutive non-PLAYING reads are needed
  before any restart decision (A36, evidence/23-acoustic-v015b), and no read counts during the
  ~0.6 s a LOAD needs to report PLAYING (A21).
* the playhead estimate rejects a sample contradicted by its neighbours and reports its residual
  (A15, evidence/17: σ 10.2 ms with one-sample spikes of +162/−156 ms).
* mute, never zero a level (A37, evidence/28): a group's master volume *multiplies* its members'
  levels, so a zeroed level is a silent kitchen that looks like it plays.

Nothing here opens a socket until :meth:`CastGroup.connect` is called; a dry run that only builds
the object and prints a plan therefore constructs no transport at all (that is what
``tools/group_probe_v020.py --dry-run`` asserts).

Test seams (all of them exist so that the offline suite needs no device): :meth:`CastGroup.now`,
:meth:`CastGroup.sleep`, :meth:`CastGroup._discover`, :meth:`CastGroup._guard` and
:meth:`CastGroup._open_link`.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from collections import deque
from typing import Any, Callable, Iterable, Sequence

from .contracts import (
    PLAYER_BUFFERING,
    PLAYER_IDLE,
    PLAYER_PAUSED,
    PLAYER_PLAYING,
    GroupSample,
)

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:  # lib/ holds groupguard.py and castv2.py
    sys.path.insert(0, _LIB)

#: the Default Media Receiver on the group, exactly as v0.1 uses it (relay/cast_relay.py:50)
DMR = "CC1AD845"

DISCOVER_TIMEOUT_S = 6.0
KEEPALIVE_EVERY_S = 5.0  # A23: the receiver closes a link that stops reading after ~24 s
POLL_TIMEOUT_S = 3.0
LAUNCH_TIMEOUT_S = 25.0
PARK_WAIT_S = 5.0  # v0.1 waits this long for the park to be confirmed
PARK_TOL_S = 0.05
SETTLE_S = 2.0  # A21: right after a LOAD the group reports the requested position
RESTART_READS = 2  # A36: one stale read must not cost a 1.6 s gap
VOLUME_EPS = 1e-3  # a level this low is 0 for all practical purposes — refuse to send it
VOLUME_FRONT = 0.005  # mirror on change only: resending fights a change made on the group

#: how many reads the robust median looks at (A15: 5-sample median)
WINDOW = 5
#: a read this far from the consensus is suspect (A15: one-sample spikes of ±160 ms)
OUTLIER_MS = 60.0
#: a level fit needs this much wall time before its slope is trusted
MIN_FIT_SPAN_S = 0.5
#: a fitted rate outside this of 1.0 is noise, not a rate: the group free-runs within 1e-4
#: of real time (evidence/17), so a 1 s window that says 1.01 is measuring its own noise.
MAX_FIT_SLOPE_DEV = 0.02
#: a sample older than this is dropped from the median (it would mix drift into a level)
WINDOW_AGE_S = 5.0
#: contracts §2 reports drift as ms per 10 minutes: the factor that turns a rate ratio into it
DRIFT_WINDOW_S = 600.0

# --- estimator verdicts ---------------------------------------------------------------------
VERDICT_OK = "ok"  # accepted, agrees with the window
VERDICT_RESUMED = "resumed"  # accepted, first read after a non-playing one
VERDICT_JUMP = "jump"  # accepted as a real move: two reads in a row disagree
VERDICT_OUTLIER = "outlier"  # rejected: contradicted by the window and by its neighbours
VERDICT_NOT_PLAYING = "not_playing"  # a frozen position, not a sample of a moving playhead


# ------------------------------------------------------------------------------ seams
def discover_groups(timeout: float = DISCOVER_TIMEOUT_S) -> dict[str, tuple[str, int]]:
    """All Cast groups on the LAN: name -> (host, port), resolved over mDNS (read-only)."""
    import groupguard

    return groupguard.discover_groups(timeout)


def require_safe_group(host: str, port: int, label: str = "") -> list[dict]:
    """The only gate that may allow a session on a group (contracts §0, A6)."""
    import groupguard

    return groupguard.require_safe_group(host, port, label)


def make_link(host: str, port: int):
    """One CASTv2 transport. Imported late so this module imports without pychromecast."""
    from castv2 import CastLink

    return CastLink(host, port)


# ------------------------------------------------------------------------------ errors
class UnsafeGroup(RuntimeError):
    """The group contains something that is not an audio speaker (contracts §0)."""


class GroupNotFound(RuntimeError):
    """The group named in the configuration is not on the LAN."""


class ForeignSession(RuntimeError):
    """The group is running a session this object did not create: hands off."""


# ------------------------------------------------------------------ playhead estimator
class PlayheadEstimator:
    """Filtered playhead of the group, on the controller's clock (contracts §2).

    v0.1's lesson (A15): a group's ``currentTime`` carries isolated stale reads — σ 10.2 ms about
    the trend, with one-sample spikes of +162/−156 ms — and acting on one costs an audible reload.
    So a read is a *sample* only when it agrees with the median of the last five; a read that
    contradicts both the window and its neighbours is dropped, while two contradicting reads in a
    row are accepted as a real move (a seek we did not ask for) and start a new window.

    The position at a given wall time is a least-squares level fit over the window (a level, not
    a rate law: the group free-runs, contracts §1), falling back to the robust median offset while
    the window is too short to fit. ``residual_ms`` is what that fit cannot explain — the number
    the hardware gate compares with the group's real noise.
    """

    def __init__(
        self,
        window: int = WINDOW,
        outlier_ms: float = OUTLIER_MS,
        min_span_s: float = MIN_FIT_SPAN_S,
        max_age_s: float = WINDOW_AGE_S,
    ) -> None:
        self.window = int(window)
        self.outlier_ms = float(outlier_ms)
        self.min_span_s = float(min_span_s)
        self.max_age_s = float(max_age_s)
        self.points: deque[tuple[float, float]] = deque(maxlen=self.window)
        self.last_media: float | None = None  # the last position the group reported
        self.accepted = 0
        self.rejections = 0
        self.jumps = 0
        self.last_verdict: str | None = None
        self._playing = False
        self._outlier_run = 0

    # -- state -------------------------------------------------------------------------------
    def reset(self) -> None:
        """Forget the window (transport event, reconnect, or a seek that moved the group)."""
        self.points.clear()
        self.last_media = None
        self._outlier_run = 0
        self._playing = False

    def mark_parked(self, media: float | None = None) -> None:
        """The playhead is frozen from now on: never extrapolate past the last known position."""
        self._playing = False
        self._outlier_run = 0
        if media is not None:
            self.last_media = float(media)

    def median_offset(self) -> float | None:
        """Median of ``media - wall`` over the window: the robust level of the playhead."""
        if not self.points:
            return None
        offs = sorted(media - wall for wall, media in self.points)
        return offs[len(offs) // 2]

    def add(self, sample: GroupSample) -> str:
        """Feed one reading; returns a ``VERDICT_*`` string (see ``sample()`` for the policy)."""
        if sample.player_state != PLAYER_PLAYING:
            self._playing = False
            self._outlier_run = 0
            self.last_media = sample.media  # frozen, but still the group's position
            self.last_verdict = VERDICT_NOT_PLAYING
            return VERDICT_NOT_PLAYING
        if not self._playing:
            # A paused group does not move, so the window before the pause is about a different
            # timeline; mixing the two is exactly how a stale read becomes a fake jump.
            self.points.clear()
            self._playing = True
            self._outlier_run = 0
            return self._accept(sample, VERDICT_RESUMED)

        offset = sample.media - sample.wall
        level = self.median_offset()
        if level is not None and abs(offset - level) * 1000.0 > self.outlier_ms:
            self._outlier_run += 1
            if self._outlier_run < RESTART_READS:
                # Contradicted by the window *and* by the neighbours that built it: stale read.
                self.rejections += 1
                self.last_verdict = VERDICT_OUTLIER
                return VERDICT_OUTLIER
            # Two in a row agree with each other and not with the window: the group really
            # moved, the window is stale. Start a new one instead of rejecting forever.
            self.jumps += 1
            self.points.clear()
            self._outlier_run = 0
            return self._accept(sample, VERDICT_JUMP)

        self._outlier_run = 0
        return self._accept(sample, VERDICT_OK)

    def _accept(self, sample: GroupSample, verdict: str) -> str:
        self._prune(sample.wall)
        self.points.append((sample.wall, sample.media))
        self.last_media = sample.media
        self.accepted += 1
        self.last_verdict = verdict
        return verdict

    def _prune(self, wall: float) -> None:
        while self.points and self.points[0][0] < wall - self.max_age_s:
            self.points.popleft()

    # -- estimate ----------------------------------------------------------------------------
    def fit(self) -> tuple[float, float | None]:
        """(slope, intercept) of ``media = slope*wall + intercept`` for the current window."""
        pts = list(self.points)
        if not pts:
            return 1.0, None
        xs = [wall for wall, _ in pts]
        ys = [media for _, media in pts]
        span = xs[-1] - xs[0]
        if len(pts) < 3 or span < self.min_span_s:
            level = self.median_offset()
            return 1.0, level
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 0.0:
            return 1.0, self.median_offset()
        slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
        if abs(slope - 1.0) > MAX_FIT_SLOPE_DEV:
            slope = 1.0  # a 5-sample window cannot measure a rate; do not invent one
        return slope, my - slope * mx

    def slope(self) -> float:
        return self.fit()[0]

    def playhead(self, wall: float) -> float | None:
        """Media position at controller time ``wall``; ``None`` with no usable sample.

        While the group is not PLAYING the last reported position is returned unchanged: a paused
        playhead does not advance, and extrapolating it is how a fake error gets invented.
        """
        if not self._playing:
            return self.last_media
        if not self.points:
            return self.last_media
        slope, intercept = self.fit()
        if intercept is None:
            return None
        return intercept + slope * wall

    def residual_ms(self) -> float | None:
        """σ of the residuals of the window about the fit, in ms (A15: ~10 ms on the group)."""
        pts = list(self.points)
        if len(pts) < 3:
            return None
        slope, intercept = self.fit()
        if intercept is None:
            return None
        res = [media - (slope * wall + intercept) for wall, media in pts]
        dof = 2 if len(pts) >= 3 and (pts[-1][0] - pts[0][0]) >= self.min_span_s else 1
        if len(res) - dof < 1:
            return None
        return statistics.stdev(res) * 1000.0 if len(res) > 1 else 0.0

    def max_residual_ms(self) -> float | None:
        pts = list(self.points)
        if len(pts) < 3:
            return None
        slope, intercept = self.fit()
        if intercept is None:
            return None
        return max(abs(media - (slope * wall + intercept)) for wall, media in pts) * 1000.0

    def stats(self) -> dict[str, Any]:
        pts = list(self.points)
        return {
            "points": len(pts),
            "accepted": self.accepted,
            "rejected": self.rejections,
            "jumps": self.jumps,
            "playing": self._playing,
            "span_s": round(pts[-1][0] - pts[0][0], 3) if len(pts) > 1 else 0.0,
            "slope": self.slope() if pts else None,
            "residual_ms": self.residual_ms(),
            "max_residual_ms": self.max_residual_ms(),
            "last_verdict": self.last_verdict,
        }


# ---------------------------------------------------------------------- restart gate
class RestartGate:
    """When a restart decision is allowed (A36, A21).

    Counts consecutive reads that are not ``playing`` and reports the second one; a single
    non-playing read is a suspicion, not a fact (evidence/23-acoustic-v015b: one stale read
    started a reload 1.6 s into a session, split an acoustic window, and is exactly what the
    listener hears as "it starts again"). It only counts while the group is *expected* to play,
    and never during the settle window after a transport event.
    """

    def __init__(self, needed: int = RESTART_READS, settle_s: float = SETTLE_S) -> None:
        self.needed = int(needed)
        self.settle_s = float(settle_s)
        self.streak = 0
        self._armed = False
        self._settled_at: float | None = None

    def expect_playing(self, now: float) -> None:
        """The group should be playing from ``now`` on (we just sent PLAY, or a seek)."""
        self._armed = True
        self._settled_at = now
        self.streak = 0

    def expect_parked(self) -> None:
        """The group is parked on purpose (prepared, paused by the user, stopped)."""
        self._armed = False
        self.streak = 0

    def reset(self) -> None:
        self._armed = False
        self.streak = 0
        self._settled_at = None

    def observe(self, now: float, player_state: str) -> bool:
        """One read. ``True`` once ``needed`` consecutive non-playing reads were seen."""
        if not self._armed:
            self.streak = 0
            return False
        if self._settled_at is not None and now - self._settled_at < self.settle_s:
            return False  # a LOAD takes ~0.6 s to report PLAYING (A21)
        if player_state == PLAYER_PLAYING:
            self.streak = 0
            return False
        self.streak += 1
        return self.streak >= self.needed


# ------------------------------------------------------------------------ level / drift
def window_medians(
    samples: Iterable[GroupSample], window_s: float = 5.0, min_points: int = 3
) -> list[tuple[float, float]]:
    """(centre wall, median media) per ``window_s`` bucket of playing samples (contracts §2)."""
    pts = sorted((s.wall, s.media) for s in samples if s.player_state == PLAYER_PLAYING)
    if not pts:
        return []
    t0 = pts[0][0]
    buckets: dict[int, list[tuple[float, float]]] = {}
    for wall, media in pts:
        buckets.setdefault(int((wall - t0) // window_s), []).append((wall, media))
    out = []
    for _, bucket in sorted(buckets.items()):
        if len(bucket) < min_points:
            continue
        out.append((statistics.fmean(w for w, _ in bucket), statistics.median(m for _, m in bucket)))
    return out


def least_squares(points: Sequence[tuple[float, float]]) -> dict[str, float | int | None]:
    """``media = slope*wall + intercept`` with the standard error of the slope."""
    pts = list(points)
    n = len(pts)
    if n < 2:
        return {
            "n": n,
            "slope": None,
            "intercept": None,
            "sigma_ms": None,
            "slope_se": None,
            "span_s": 0.0,
        }
    xs = [w for w, _ in pts]
    ys = [m for _, m in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        return {
            "n": n,
            "slope": None,
            "intercept": None,
            "sigma_ms": None,
            "slope_se": None,
            "span_s": 0.0,
        }
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    intercept = my - slope * mx
    res = [y - (slope * x + intercept) for x, y in pts]
    sigma = statistics.stdev(res) if n > 2 else 0.0
    slope_se = sigma / (sxx**0.5) if n > 2 and sxx > 0 else None
    return {
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "sigma_ms": sigma * 1000.0,
        "slope_se": slope_se,
        "span_s": xs[-1] - xs[0],
    }


def drift(points: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Drift of 5 s medians in ms / 10 min, with its standard error (contracts §2)."""
    fit = least_squares(points)
    slope = fit["slope"]
    factor = DRIFT_WINDOW_S * 1000.0  # a rate ratio into ms per 10 minutes
    if slope is None:
        return {
            "n": fit["n"],
            "drift_ms_per_10min": None,
            "se_ms_per_10min": None,
            "significant": None,
            "sigma_ms": None,
        }
    value = (float(slope) - 1.0) * factor
    se = None if fit["slope_se"] is None else float(fit["slope_se"]) * factor
    return {
        "n": fit["n"],
        "drift_ms_per_10min": value,
        "se_ms_per_10min": se,
        "significant": None if se is None else abs(value) > 2.0 * se,
        "sigma_ms": fit["sigma_ms"],
        "span_s": fit["span_s"],
    }


# ------------------------------------------------------------------------------ the sink
class CastGroup:
    """Audio sink: the Default Media Receiver on a speakers-only group."""

    def __init__(
        self, name: str = "Cuisine", host: str | None = None, port: int | None = None, log=print
    ) -> None:
        self.name, self.log = name, log
        self.host, self.port = host, port
        self.link = None
        self.transport: str | None = None
        self.session: int | None = None  # the mediaSessionId *we* created
        self.content: str | None = None
        self.state: str = PLAYER_IDLE
        self.volume: float | None = None
        self.muted: bool | None = None
        self.estimator = PlayheadEstimator()
        self.gate = RestartGate()
        self.now: Callable[[], float] = time.time
        self.sleep: Callable[[float], None] = time.sleep
        self._link_errors = 0
        self._restart = False
        self._last_sample: GroupSample | None = None

    # -- test seams (overridden by tests/v020/test_group.py) ---------------------------------
    def _discover(self, timeout: float = DISCOVER_TIMEOUT_S) -> dict[str, tuple[str, int]]:
        return discover_groups(timeout)

    def _guard(self, host: str, port: int) -> list[dict]:
        return require_safe_group(host, port, f"group {self.name!r}")

    def _open_link(self, host: str, port: int):
        return make_link(host, port)

    # -- lifecycle ---------------------------------------------------------------------------
    def connect(self) -> None:
        """Resolve by name over mDNS, refuse an unsafe group, connect, keepalive-ready."""
        if self.link is not None:
            self._drop_link()
        host, port = self._resolve()
        members = self._guard(host, port)  # A6: before any link of ours is opened
        self.host, self.port = host, port
        self.link = self._open_link(host, port)
        try:
            self.link.connect_platform()
            st = self.link.receiver_status()
        except Exception:
            self._drop_link()
            raise
        app_id, app_name, transport = _receiver_app(st)
        self.transport = transport
        self.session = None  # we created nothing yet
        self.estimator.reset()
        self.gate.reset()
        self._restart = False
        self._link_errors = 0
        self._read_volume(st)
        self._warn_if_silent(members)
        self.log(
            f"group_connected: {self.name!r} {host}:{port} "
            f"members={[m.get('name') for m in members]} app={app_name!r}"
        )
        if transport:
            try:
                self.link.connect_transport(transport)
            except Exception as exc:  # noqa: BLE001 - a camera/backdrop may refuse
                self.log(f"group_transport_connect_failed: {type(exc).__name__}: {exc}")
            self._note_session_at_connect()

    def reconnect(self) -> None:
        """Re-resolve + re-guard after a link error (A22); keeps a session we still own."""
        given = self.session
        self.log(f"group_reconnect: {self.name!r}")
        self.connect()  # resolution and guard run again (A6)
        if given is not None:
            ok, device_session = self._device_session()
            if ok and device_session == given:
                self.session = given  # the group is still running what we started
                self.log(f"group_session_adopted: {given}")
            else:
                self.log(f"group_session_lost: ours={given} device={device_session}")

    def close(self) -> None:
        self._drop_link()
        self.estimator.reset()
        self.gate.reset()

    # -- transport ---------------------------------------------------------------------------
    def prepare(self, url: str, content_type: str, at_time: float) -> bool:
        """LOAD parked in PAUSE at ``at_time`` (evidence/27: PAUSE→SEEK→PLAY σ 30 ms)."""
        self._require_link()
        at = max(0.0, float(at_time))
        transport = self._ensure_app()
        try:
            if (
                self.content == url
                and self.session is not None
                and self.state in (PLAYER_PLAYING, PLAYER_PAUSED, PLAYER_BUFFERING)
            ):
                if self.state != PLAYER_PAUSED:
                    self.link.media_command(transport, "PAUSE", self.session, timeout=8)
                # SEEK while paused: the parked position is set without ever starting the audio
                self.link.media_command(
                    transport,
                    "SEEK",
                    self.session,
                    {"currentTime": at, "resumeState": "PLAYBACK_PAUSE"},
                    timeout=8,
                )
            else:
                payload = {
                    "appId": DMR,
                    "sessionId": transport,
                    "media": {
                        "contentId": url,
                        "contentType": content_type,
                        "streamType": "BUFFERED",
                        "metadata": {"metadataType": 0, "title": ""},
                    },
                    "autoplay": False,
                    "currentTime": at,
                }
                reply = self.link.load(transport, payload, timeout=LAUNCH_TIMEOUT_S)
                status = (reply.get("status") or [{}])[0]
                self.session = status.get("mediaSessionId")  # ours: we created it
                self.content = url
                self.state = status.get("playerState") or PLAYER_BUFFERING
        except Exception:
            self._link_errors += 1
            raise
        self.estimator.reset()
        self._restart = False
        self.gate.expect_parked()
        deadline = self.now() + PARK_WAIT_S
        while True:
            status = self._media_status()
            state = status.get("playerState")
            position = status.get("currentTime")
            if state == PLAYER_PAUSED and position is not None and abs(position - at) < PARK_TOL_S:
                self.state = PLAYER_PAUSED
                self.estimator.mark_parked(at)
                self.log(f"group_parked: {at:.3f}s session={self.session}")
                return True
            if self.now() >= deadline:
                self.log(f"group_park_timeout: state={state} position={position} want={at:.3f}")
                return False
            self.sleep(0.1)

    def play(self) -> None:
        """Release the parked position. The only instant that decides the start (§1)."""
        self._require_ours()
        transport = self.transport
        try:
            self.link.media_command(transport, "PLAY", self.session, timeout=8)
        except Exception:
            self._link_errors += 1
            raise
        self.state = PLAYER_PLAYING
        self.estimator.reset()  # the window starts at the release
        self._restart = False
        self.gate.expect_playing(self.now())

    def pause(self) -> None:
        self._require_ours()
        transport = self.transport
        try:
            self.link.media_command(transport, "PAUSE", self.session, timeout=8)
        except Exception:
            self._link_errors += 1
            raise
        self.state = PLAYER_PAUSED
        self._restart = False
        self.gate.expect_parked()
        frozen = self._last_sample.media if self._last_sample is not None else None
        self.estimator.mark_parked(frozen)

    def seek(self, at_time: float) -> None:
        self._require_ours()
        transport = self.transport
        try:
            self.link.media_command(
                transport, "SEEK", self.session, {"currentTime": max(0.0, float(at_time))}, timeout=8
            )
        except Exception:
            self._link_errors += 1
            raise
        # A seek invalidates the window and is a transport event: re-arm with its settle time.
        self.estimator.reset()
        self.estimator.mark_parked(max(0.0, float(at_time)))
        self._restart = False
        if self.state == PLAYER_PLAYING:
            self.gate.expect_playing(self.now())
        else:
            self.gate.expect_parked()

    def set_volume(self, level: float, muted: bool | None = None) -> None:
        """Mirror a level. Never sets a level to 0 to silence (A37)."""
        self._require_link()
        want = float(level)
        want = max(0.0, min(1.0, want))
        want_muted = None if muted is None else bool(muted)
        if want < VOLUME_EPS:
            # A37: a level of 0 destroyed the value needed to restore it, and a group's master
            # volume multiplies its members' levels, so a zeroed group is silent while it plays.
            # Mute instead, and leave the device's own level untouched.
            if self.muted is True and want_muted is not False:
                return
            self._set_receiver_volume(level=None, muted=True)
            self.muted = True
            self.log("group_volume_zero_refused: muted instead, level left untouched")
            return
        level_out = round(want, 3)
        if (
            self.volume is not None
            and abs(level_out - self.volume) < VOLUME_FRONT
            and (want_muted is None or want_muted == self.muted)
        ):
            return  # mirror on change only: front, like v0.1
        self._set_receiver_volume(level=level_out, muted=want_muted)
        self.volume = level_out
        if want_muted is not None:
            self.muted = want_muted

    def stop(self) -> None:
        """Stop the session this class created; leave every other session alone."""
        if self.link is None:
            self.log("group_stop_skipped: no link")
            self.session = None
            return
        if self.session is None:
            self.log("group_stop_skipped: this object created no session")
            return
        ok, device_session = self._device_session()
        if ok and device_session != self.session:
            # §8: never touch a session we did not create. It is not ours any more.
            self.log(f"group_stop_refused: device session {device_session} is not ours ({self.session})")
            self.session = None
            self.estimator.reset()
            self.gate.expect_parked()
            return
        try:
            self.link.stop_app(self.transport, timeout=10)
            self.log(f"group_stopped: session={self.session}")
        except Exception as exc:  # noqa: BLE001 - best effort, link may be gone
            self.log(f"group_stop_failed: {type(exc).__name__}: {exc}")
        finally:
            self.session = None
            self.content = None
            self.state = PLAYER_IDLE
            self._restart = False
            self.estimator.reset()
            self.gate.expect_parked()

    # -- observation -------------------------------------------------------------------------
    def sample(self) -> GroupSample | None:
        """One playhead reading, with outliers rejected (§2, A15).

        ``None`` means: the link is dead, the group runs nothing, or this read was contradicted
        by the window and by its neighbours — in all three cases the caller must not act on it.
        """
        if self.link is None or self.transport is None:
            return None
        try:
            self.link.keepalive(KEEPALIVE_EVERY_S)  # A23: answer PINGs, or the link is closed
            t0 = self.now()
            status = self._media_status()
            t1 = self.now()
            self._link_errors = 0
        except Exception as exc:  # noqa: BLE001 - caller reconnects (A22)
            self._link_errors += 1
            self.log(f"group_read_error: {type(exc).__name__}: {exc}")
            return None
        wall = (t0 + t1) / 2.0  # the playhead belongs to the reply, not the poll
        device_session = status.get("mediaSessionId")
        state = status.get("playerState") or PLAYER_IDLE
        media = status.get("currentTime")
        self.state = state
        if self.session is not None and device_session is not None and device_session != self.session:
            self.log(f"group_foreign_session: {device_session} replaced ours {self.session}")
            self.session = None  # we own nothing on this group any more
        if media is None:
            return None
        sample = GroupSample(
            wall=wall, media=float(media), player_state=state, session_id=device_session
        )
        self._last_sample = sample
        self._restart = self.gate.observe(wall, state)
        verdict = self.estimator.add(sample)
        if verdict == VERDICT_OUTLIER:
            self.log(
                f"group_sample_rejected: media={sample.media:.3f} level={self.estimator.median_offset()}"
            )
            return None
        return sample

    def playhead(self, wall: float) -> float | None:
        """Media position at controller time ``wall``, from the filtered estimator."""
        return self.estimator.playhead(wall)

    # -- state for the controller ------------------------------------------------------------
    def restart_needed(self) -> bool:
        """Two consecutive non-PLAYING reads while playing was expected (A36)."""
        return self._restart

    def needs_reconnect(self) -> bool:
        """The link is gone, or two reads in a row failed (A22's reconnect trigger)."""
        if self.link is None:
            return True
        return self._link_errors >= 2 or bool(getattr(self.link, "closed", False))

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "endpoint": f"{self.host}:{self.port}" if self.host else None,
            "transport": self.transport,
            "session": self.session,
            "content": self.content,
            "state": self.state,
            "volume": self.volume,
            "muted": self.muted,
            "link_errors": self._link_errors,
            "restart_needed": self._restart,
            "estimator": self.estimator.stats(),
        }

    # -- internals ---------------------------------------------------------------------------
    def _resolve(self) -> tuple[str, int]:
        found: dict[str, tuple[str, int]] = {}
        try:
            found = self._discover()
        except Exception as exc:  # noqa: BLE001 - mDNS may be unavailable
            self.log(f"group_discovery_failed: {type(exc).__name__}: {exc}")
        if self.name in found:
            return found[self.name]
        if self.host and self.port:
            self.log(f"group_not_in_mdns: {self.name!r}; keeping {self.host}:{self.port}")
            return self.host, int(self.port)
        raise GroupNotFound(
            f"group {self.name!r} is not on the LAN (mDNS saw {sorted(found) or 'nothing'}); "
            f"the port of a Cast group is dynamic, so it must be resolved by name (A19)"
        )

    def _ensure_app(self) -> str:
        """The Default Media Receiver must be running on the group; launch it if it is not."""
        status = self.link.receiver_status()
        _app_id, _app_name, transport = _receiver_app(status)
        if not transport:
            self.link.launch(DMR, timeout=LAUNCH_TIMEOUT_S)
            status = self.link.receiver_status()
            _app_id, _app_name, transport = _receiver_app(status)
            if not transport:
                raise RuntimeError("the group did not launch the Default Media Receiver")
            self.sleep(0.2)
        if transport != self.transport:
            self.transport = transport
            self.link.connect_transport(transport)
            self.sleep(0.2)
        return transport

    def _media_status(self) -> dict:
        reply = self.link.media_status(self.transport, timeout=POLL_TIMEOUT_S)
        return (reply.get("status") or [{}])[0]

    def _device_session(self) -> tuple[bool, int | None]:
        """(read succeeded, mediaSessionId or None) — read-only, never commands anything."""
        if self.link is None or self.transport is None:
            return False, None
        try:
            return True, self._media_status().get("mediaSessionId")
        except Exception:  # noqa: BLE001 - a dead link answers nothing
            return False, None

    def _note_session_at_connect(self) -> None:
        """A session already running when we connect is not ours until reconnect adopts it (§8)."""
        ok, device_session = self._device_session()
        if ok and device_session:
            self.state = PLAYER_PLAYING
            self.log(f"group_session_present: {device_session} (ours only if its id is recorded)")

    def _require_link(self) -> None:
        if self.link is None:
            raise RuntimeError("CastGroup.connect() was not called")
        if getattr(self.link, "closed", False):
            raise RuntimeError("the group link is closed: reconnect() first")

    def _require_ours(self) -> None:
        self._require_link()
        if self.session is None:
            raise ForeignSession("no session created by this object: refusing to command the group")

    def _read_volume(self, status: dict) -> None:
        volume = status.get("volume") or {}
        if volume.get("level") is not None:
            self.volume = round(float(volume["level"]), 3)
        if volume.get("muted") is not None:
            self.muted = bool(volume["muted"])

    def _warn_if_silent(self, members: list[dict]) -> None:
        """A37: a group master volume multiplies its members' levels — silence hides in there."""
        levels = [m.get("level") for m in members if m.get("level") is not None]
        all_muted = bool(members) and all(bool(m.get("muted")) for m in members)
        if all_muted or (levels and max(levels) < VOLUME_EPS):
            self.log(
                f"WARNING group members are silent: levels={levels} "
                f"muted={all_muted} — the group will play but nothing will be heard "
                f"(A37: members' levels multiply the master)"
            )

    def _set_receiver_volume(self, level: float | None, muted: bool | None) -> None:
        try:
            self.link.set_receiver_volume(level=level, muted=muted)
        except Exception as exc:  # noqa: BLE001
            self._link_errors += 1
            raise RuntimeError(f"group volume not set: {type(exc).__name__}: {exc}") from exc

    def _drop_link(self) -> None:
        link, self.link = self.link, None
        self.transport = None
        if link is not None:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass


def _receiver_app(status: dict) -> tuple[str | None, str | None, str | None]:
    """(app_id, display_name, transport_id) of the first running app.

    Same contract as ``castv2.receiver_app``, re-declared here so that importing this module
    does not import pychromecast (the offline suite must run without it).
    """
    apps = status.get("applications") or []
    if not apps:
        return None, None, None
    app = apps[0]
    return app.get("appId"), app.get("displayName"), app.get("transportId")
