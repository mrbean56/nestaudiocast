"""Frozen constants and value types of v0.2.0 (docs/v020-contracts.md §2-§6).

Both sides of every interface import from here: the Python controller, the receiver page (which
mirrors these strings by hand, and is checked by tests/v020/test_contracts.py), the fakes and the
tests. Adding a message type or a state needs the integrator's approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- receiver protocol (contracts §3) -------------------------------------------------------

NS_RECEIVER = "urn:x-cast:com.beansden.nestaudiocast.v1"

#: controller -> receiver
MSG_CLOCK = "clock"
MSG_LOAD = "load"
MSG_PLAY_AT = "play_at"
MSG_PAUSE = "pause"
MSG_SEEK = "seek"
MSG_RATE = "rate"
MSG_STOP = "stop"
MSG_IDLE = "idle"

#: receiver -> controller
MSG_HELLO = "hello"
MSG_CLOCK_REPLY = "clock_reply"
MSG_STATE = "state"
MSG_INTENT = "intent"
MSG_ENDED = "ended"
MSG_ERROR = "error"

CONTROLLER_MESSAGES = (
    MSG_CLOCK,
    MSG_LOAD,
    MSG_PLAY_AT,
    MSG_PAUSE,
    MSG_SEEK,
    MSG_RATE,
    MSG_STOP,
    MSG_IDLE,
)
RECEIVER_MESSAGES = (
    MSG_HELLO,
    MSG_CLOCK_REPLY,
    MSG_STATE,
    MSG_INTENT,
    MSG_ENDED,
    MSG_ERROR,
)

# --- player / session states ----------------------------------------------------------------

#: receiver side, field ``player_state``
PLAYER_IDLE = "idle"
PLAYER_LOADING = "loading"
PLAYER_BUFFERING = "buffering"
PLAYER_PLAYING = "playing"
PLAYER_PAUSED = "paused"
PLAYER_ENDED = "ended"
PLAYER_ERROR = "error"
PLAYER_STATES = (
    PLAYER_IDLE,
    PLAYER_LOADING,
    PLAYER_BUFFERING,
    PLAYER_PLAYING,
    PLAYER_PAUSED,
    PLAYER_ENDED,
    PLAYER_ERROR,
)

#: owner app side, ``GET /api/status`` -> ``state``
STATE_IDLE = "idle"
STATE_RESOLVING = "resolving"
STATE_STARTING = "starting"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_ENDED = "ended"
STATE_QUIET_REFUSED = "quiet_refused"
SESSION_STATES = (
    STATE_IDLE,
    STATE_RESOLVING,
    STATE_STARTING,
    STATE_PLAYING,
    STATE_PAUSED,
    STATE_ENDED,
    STATE_QUIET_REFUSED,
)

# --- transport intents (contracts §3.2) ------------------------------------------------------

INTENT_PLAY = "play"
INTENT_PAUSE = "pause"
INTENT_SEEK = "seek"
INTENTS = (INTENT_PLAY, INTENT_PAUSE, INTENT_SEEK)

# --- HTTP surface (contracts §5) ------------------------------------------------------------

HTTP_PORT_DEFAULT = 7640
PATH_RECEIVER = "/receiver.html"  # frozen: registered in the Cast console
PATH_SHARE = "/api/share"
PATH_CONTROL = "/api/control"
PATH_STATUS = "/api/status"
PATH_HEALTH = "/healthz"
PATH_MEDIA_PREFIX = "/media/"
PATH_UI = "/"
LAN_PREFIX = "192.168.2."

#: POST /api/control actions
ACTION_PLAY = "play"
ACTION_PAUSE = "pause"
ACTION_SEEK = "seek"
ACTION_VOLUME = "volume"
ACTION_STOP = "stop"
ACTIONS = (ACTION_PLAY, ACTION_PAUSE, ACTION_SEEK, ACTION_VOLUME, ACTION_STOP)

# --- configuration defaults (contracts §6) --------------------------------------------------

CONFIG_DEFAULTS: dict[str, Any] = {
    "CHROMECAST": "192.168.2.80",
    "APP_ID": "28028CD2",
    "GROUP": "Cuisine",
    "HTTP_BIND": "192.168.2.21",
    "HTTP_PORT": str(HTTP_PORT_DEFAULT),
    "DELTA_V2": "0.0",
    "RATE_MAX": "0.002",
    "MAX_HEIGHT": "1080",
    "QUIET_START": "",
    "QUIET_END": "",
    "QUIET_POLICY": "refuse",
}

QUIET_POLICIES = ("refuse", "video_only")

# --- timeline rules (contracts §2) -----------------------------------------------------------

STEADY_AFTER_S = 30.0  # no transport event for this long = steady
STEADY_WINDOW_S = 5.0  # digital-error window
DIGITAL_TOL_MS = 20.0  # acceptance: |median e| per window
RATE_DEAD_BAND_S = 0.005  # do not trim for less than this
RATE_MAX_DEFAULT = 0.002  # |rate - 1| ceiling
STATE_HZ = 4.0  # receiver -> controller state cadence

# --- group ownership (contracts §7) ---------------------------------------------------------

OWNER_FILE = "state/group.owner"
OWNER_SENDER = "cast-sender"
OWNER_RELAY = "cast-relay"
OWNER_STALE_S = 15.0
OWNER_HEARTBEAT_S = 5.0


def digital_error(group_pos: float, video_pos: float, delta_v2: float) -> float:
    """``e = p_group - p_video - DELTA_V2`` — contracts §2. Positive = the kitchen is ahead."""
    return float(group_pos) - float(video_pos) - float(delta_v2)


@dataclass(frozen=True)
class Sample:
    """One timestamped reading from one side (§1-§2).

    ``rx_ms`` is that device's clock (§3, ``performance.timeOrigin + performance.now()`` for the
    receiver, a monotonic base for the controller). ``media`` is media seconds, float64.
    """

    rx_ms: float
    media: float
    rate: float = 1.0
    player_state: str = PLAYER_IDLE
    buffered_end: float | None = None


@dataclass(frozen=True)
class GroupSample:
    """One reading of the kitchen group, timestamps on the controller's clock."""

    wall: float
    media: float
    player_state: str
    session_id: int | None = None


@dataclass(frozen=True)
class ClockEstimate:
    """Result of the ping/pong estimator (§3.1). Offsets in ms, receiver minus controller."""

    offset_ms: float
    residual_ms: float
    rtt_ms: float
    samples: int
    measured_at: float


@dataclass(frozen=True)
class Command:
    """One instruction emitted by the control law (§1). ``kind`` is a MSG_* constant."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Track:
    """One elementary stream of a resolved video (§4)."""

    itag: int
    codecs: str
    content_type: str
    url: str = ""  # raw token: never logged, never archived
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    init_range: tuple[int, int] | None = None
    index_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class ResolvedMedia:
    """What the media resolver hands to the proxy (§4)."""

    video_id: str
    title: str
    duration_s: float
    video: Track
    audio: Track
    fetched_at: float
    expires_at: float
