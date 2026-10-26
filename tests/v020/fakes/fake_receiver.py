"""An in-memory receiver (contracts §3). No TLS, no device, no sound."""

from __future__ import annotations

import time

from v020.contracts import (
    MSG_CLOCK,
    MSG_CLOCK_REPLY,
    MSG_ENDED,
    MSG_HELLO,
    MSG_IDLE,
    MSG_LOAD,
    MSG_PAUSE,
    MSG_PLAY_AT,
    MSG_RATE,
    MSG_SEEK,
    MSG_STATE,
    MSG_STOP,
    PLAYER_BUFFERING,
    PLAYER_IDLE,
    PLAYER_PAUSED,
    PLAYER_PLAYING,
    ClockEstimate,
    Sample,
)


class LostDevice(RuntimeError):
    pass


class FakeReceiver:
    """Honours the protocol of §3, including the rate gain that task C must measure for real.

    ``rate_gain`` models the suspicion recorded in evidence/17 (a device that echoes a rate and
    ignores it): 0.0 = useless actuator, 1.0 = perfect. ``start_jitter`` is the landing dispersion
    of ``play_at`` in seconds (the video-side counterpart of evidence/27).
    """

    def __init__(
        self,
        app_id: str = "28028CD2",
        clock=time.monotonic,
        rate_gain: float = 1.0,
        start_jitter: float = 0.0,
        clock_offset_ms: float = 0.0,
        rtt_ms: float = 4.0,
    ):
        self.app_id = app_id
        self.clock = clock
        self.rate_gain = rate_gain
        self.start_jitter = start_jitter
        self.clock_offset_ms = clock_offset_ms
        self.rtt_ms = rtt_ms
        self.launched = False
        self.foreign = False
        self.media = 0.0
        self.rate = 1.0
        self.applied_rate = 1.0
        self.state = PLAYER_IDLE
        self.seq = 0
        self.inbox: list[dict] = []
        self.outbox: list[dict] = []
        self.calls: list[str] = []
        self.loaded_url: str | None = None
        self._anchor_ms: float | None = None  # rx_ms at which self.media was valid
        self._playing = False

    # -- lifecycle ---------------------------------------------------------------------------
    def launch(self, timeout: float = 20.0) -> dict:
        self.launched = True
        self.outbox.append(
            {
                "type": MSG_HELLO,
                "seq": 0,
                "rx_ms": self.rx_ms(),
                "ua": "fake",
                "codecs": {"h264": True, "hevc": False, "vp9": False, "av1": False},
                "version": "0.2.0-fake",
            }
        )
        return {"applications": [{"appId": self.app_id, "sessionId": "fake-session"}]}

    def connect(self, timeout: float = 10.0) -> None:
        if self.foreign:
            raise LostDevice("another sender holds the device")

    def close(self) -> None:
        pass

    def takeover(self) -> None:
        """Simulate a foreign cast taking the Chromecast (§3.3)."""
        self.foreign = True
        self.state = PLAYER_IDLE
        self._playing = False

    # -- protocol ----------------------------------------------------------------------------
    def rx_ms(self) -> float:
        return self.clock() * 1000.0 + self.clock_offset_ms

    def _media_at(self, rx_ms: float) -> float:
        if not self._playing or self._anchor_ms is None:
            return self.media
        return self.media + (rx_ms - self._anchor_ms) / 1000.0 * self.applied_rate

    def send(self, msg_type: str, **payload) -> int:
        if self.foreign:
            raise LostDevice("another sender holds the device")
        self.seq += 1
        self.inbox.append({"type": msg_type, "seq": self.seq, **payload})
        handler = {
            MSG_CLOCK: self._on_clock,
            MSG_LOAD: self._on_load,
            MSG_PLAY_AT: self._on_play_at,
            MSG_PAUSE: self._on_pause,
            MSG_SEEK: self._on_seek,
            MSG_RATE: self._on_rate,
            MSG_STOP: self._on_stop,
            MSG_IDLE: self._on_stop,
        }.get(msg_type)
        if handler is not None:
            handler(self.seq, payload)
        return self.seq

    def _emit_state(self, seq: int) -> None:
        self.outbox.append(
            {
                "type": MSG_STATE,
                "seq": seq,
                "rx_ms": self.rx_ms(),
                "media": self._media_at(self.rx_ms()),
                "rate": self.rate,
                "player_state": self.state,
                "buffered_end": self.media + 30.0,
                "dropped": 0,
            }
        )

    def _on_clock(self, seq: int, payload: dict) -> None:
        self.outbox.append({"type": MSG_CLOCK_REPLY, "seq": seq, "rx_ms": self.rx_ms()})

    def _on_load(self, seq: int, payload: dict) -> None:
        self.calls.append("load")
        self.loaded_url = payload.get("url")
        self.media = float(payload.get("media", 0.0))
        self._anchor_ms = self.rx_ms()
        self._playing = bool(payload.get("autoplay", False))
        self.state = PLAYER_BUFFERING if self._playing else PLAYER_PAUSED
        if self._playing:
            self.state = PLAYER_PLAYING
        self._emit_state(seq)

    def _on_play_at(self, seq: int, payload: dict) -> None:
        target_ms = float(payload["rx_ms"]) + self.start_jitter * 1000.0
        self.calls.append("play_at")
        self.media = float(payload["media"])
        self._anchor_ms = target_ms
        self._playing = True
        self.state = PLAYER_PLAYING
        self._emit_state(seq)

    def _on_pause(self, seq: int, payload: dict) -> None:
        self.calls.append("pause")
        self.media = self._media_at(self.rx_ms())
        self._playing = False
        self.state = PLAYER_PAUSED
        self._emit_state(seq)

    def _on_seek(self, seq: int, payload: dict) -> None:
        self.calls.append("seek")
        self.media = float(payload["media"])
        self._anchor_ms = self.rx_ms()
        self._emit_state(seq)

    def _on_rate(self, seq: int, payload: dict) -> None:
        self.calls.append("rate")
        self.media = self._media_at(self.rx_ms())
        self._anchor_ms = self.rx_ms()
        self.rate = float(payload["value"])
        self.applied_rate = 1.0 + (self.rate - 1.0) * self.rate_gain
        self._emit_state(seq)

    def _on_stop(self, seq: int, payload: dict) -> None:
        self.calls.append("stop")
        self._playing = False
        self.media = 0.0
        self.state = PLAYER_IDLE
        self._emit_state(seq)

    # -- observation -------------------------------------------------------------------------
    def drain(self) -> list[dict]:
        out, self.outbox = self.outbox, []
        return out

    def ping(self, n: int = 5) -> ClockEstimate:
        self._on_clock(self.seq, {})
        return ClockEstimate(
            offset_ms=self.clock_offset_ms,
            residual_ms=self.rtt_ms / 2.0,
            rtt_ms=self.rtt_ms,
            samples=n,
            measured_at=self.clock(),
        )

    def latest(self) -> Sample | None:
        rx = self.rx_ms()
        return Sample(
            rx_ms=rx,
            media=self._media_at(rx),
            rate=self.applied_rate,
            player_state=self.state,
            buffered_end=self.media + 30.0,
        )

    def is_ours(self) -> bool:
        return not self.foreign

    # -- convenience for tests ----------------------------------------------------------------
    def end_naturally(self) -> None:
        self._playing = False
        self.state = PLAYER_IDLE
        self.outbox.append(
            {"type": MSG_ENDED, "seq": self.seq, "rx_ms": self.rx_ms(), "media": self.media}
        )
