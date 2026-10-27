# v0.2.0 — frozen contracts

Status: **frozen** 2026-10-25, branch `v0.2.0`. Every agent codes against this file. A change
needs the integrator's approval and a note in `notes/consensus.md`; nobody edits this file on a
`feat/*` branch (propose an amendment in your report instead).

Everything here that is marked *measured* comes from `evidence/`; everything marked *to measure*
is a bet that a task must settle before the hardware gate.

---

## 0. Topology (frozen, from the owner)

| Rôle | Appareil | Adresse | Notes |
|---|---|---|---|
| Video sink | **Chromecast Salon** | `192.168.2.80` | the TV's own Chromecast, unplugged from the TV tonight. Registered as a dev device. |
| Receiver app | **VideoNetCaster**, custom receiver | `APP_ID = 28028CD2` | unpublished, "Ready for Testing" |
| Receiver URL | registered in the Cast console | `http://192.168.2.21:7640/receiver.html` | **cannot change without a 15 min propagation**; the path is therefore frozen |
| Owner app + media | CT 121 `castrelay` | `192.168.2.21:7640` | one HTTP server: `/receiver.html`, `/api/*`, `/media/*`, `/` |
| Audio sink | Cast group **Cuisine** | resolved by mDNS at every start | 5 Nest Audio, `caps == 196612`; `lib/groupguard.py` is the only gate |
| v0.1 relay | `cast-relay.service` | `:8099`, same container | keeps running; must yield the group when v0.2 owns it |

Rules that follow from the record and are not negotiable:

- `lib/castv2.py`, `lib/silence.py`, `lib/emergency.py`, `lib/groupguard.py` keep their behaviour.
  The v0.1 relay depends on the first one; `tools/prove.sh` guards all of them.
- **One owner at a time for the Cuisine group**: `state/group.owner` (see §7).
- Never play on a group that contains a video device (A6, `evidence/09`).
- Mute, never zero a level; unmute at startup whatever we muted (A37, `evidence/28`).

## 1. Who leads (the architecture decision of v0.2.0)

`SET_PLAYBACK_RATE` is a no-op on the group: asked 1.005, measured 0.999957 (`evidence/17`).
After the start event the group may **never** be reloaded (acceptance: no gaps after start;
corrections are rate trims, never reloads). Therefore:

- the **group is the leader** (free running, drift −12.4 ± 1.0 ms / 10 min, `evidence/17`);
- the **video follows the group** through `playbackRate` on our own receiver — the one actuator
  v0.2 adds. *To measure* (task C): whether the Chromecast honours it, with what gain error, and
  whether a rate change stalls the player. If it does not, v0.2's acceptance cannot be met and the
  integrator must be told before anyone writes a workaround.

Two reaction speeds, never mixed up:

| Phase | Allowed actions |
|---|---|
| start, resume after pause, after a seek | park the group paused and send `PLAY`, then `play_at` on the receiver with a target clock time and media position; one re-landing is allowed (`evidence/27`: PAUSE→SEEK→PLAY σ 30 ms, LOAD σ 202 ms) |
| steady (≥ 30 s after the last transport event) | **rate trims only**, on the video side. No LOAD, no SEEK, on either side |

## 2. Timeline semantics

- A **position** is media seconds, `float64`, as reported by the device (the group's `currentTime`,
  the receiver's `video.currentTime`). No wall-clock, no integer milliseconds.
- Invariant, maintained by the control law:

  ```
  p_group = p_video + DELTA_V2
  ```

  `DELTA_V2` is the calibration knob (media seconds), the v0.2 counterpart of v0.1's `DELTA`:
  `DELTA_V2 = OBS_0` measured exactly as in `notes/measurement-protocol.md` §1, with the receiver
  playing instead of the TV. Sign rule unchanged: **`delta = +OBS_0`**, and the linearity check
  (§4.4 of the protocol) is mandatory.
- **Digital error**: `e = p_group − p_video − DELTA_V2` (positive = the kitchen is ahead).
- **Perceived offset**: what the acoustic instrument measures. It equals the digital error plus
  the residual of the render latencies, which no loop can see (consensus §5.4/§5.6). Digital
  error is the loop's business, perceived offset is the gate's business. Never report one as the
  other.
- **Steady**: a sample is steady when at least 30 s have passed since the last transport event of
  either side, the last rate command is older than 2 s, and neither device reported a non-playing
  state in the last 2 s.
- **Digital pass criterion**: over each 5 s window of steady samples, the **median** of `e` must
  be within ±20 ms (acceptance). Raw p95 and max are reported next to it, never instead of it,
  because the group's raw reads carry isolated outliers (A15: σ 10.2 ms, outliers ±156 ms).
- Drift is `least squares slope of the 5 s medians`, reported in ms / 10 min with its standard
  error. A trend only counts if the projected 10 min value exceeds 2σ.
- **Both playheads are filtered, and the video side's filter is measured** (§10.11): the receiver's
  reported playhead is a sawtooth (σ 60 ms, `evidence/49`), so `e` is computed from a 30 s running
  mean of `media − rx` on the video side (reset at every landing) and from the group's own
  estimator. `DELTA_V2` does **not** absorb an estimator bias — it cancels in the true error — so
  the filter's choice is a matter of accuracy, not of taste.

## 3. Receiver protocol (CASTv2 custom namespace)

Namespace: `urn:x-cast:com.beansden.nestaudiocast.v1`. Payload: **one JSON object**, UTF-8.
Every message has `type` (string) and `seq` (int, the controller's counter; the receiver echoes it
in its replies to that message). The receiver never sends a message without `seq`.

Clock convention: the receiver timestamps with `rx_ms = performance.timeOrigin + performance.now()`
sampled **at the same statement** as the media position it reports. The controller timestamps with
`time.time() + monotonic offset`, in ms, on its own monotonic base. Windows and offsets are
estimated from ping/pong, never assumed.

### 3.1 Controller → receiver

| type | fields | meaning |
|---|---|---|
| `clock` | — | ping; reply is `clock_reply` |
| `load` | `url`, `content_type`, `media` (float, start position), `autoplay` (bool, default false) | load media without playing |
| `play_at` | `rx_ms` (target, receiver clock), `media` (float) | seek to `media` and start so that media position `media` is on screen at receiver time `rx_ms` |
| `pause` | — | pause |
| `seek` | `media` (float) | seek while preserving the current playing state |
| `rate` | `value` (0.5 … 2.0) | `playbackRate` |
| `stop` | — | unload media, keep the app running, show the idle page |
| `idle` | — | same as `stop`, plus tell the controller we are ready for a new share |

### 3.2 Receiver → controller

| type | fields | meaning |
|---|---|---|
| `hello` | `rx_ms`, `ua`, `codecs` (object), `version` | sent once after the namespace connects and after every `receiver` reconnect |
| `clock_reply` | `rx_ms` | pong for a `clock` |
| `state` | `rx_ms`, `media`, `rate`, `player_state` (`idle|loading|buffering|playing|paused|ended|error`), `buffered_end`, `dropped` | 4 Hz while any media is loaded, and on every transition |
| `intent` | `action` (`play|pause|seek`), `media` (for seek) | the user pressed something on another sender (Google Home app, TV remote, another phone). The controller mirrors it on the group |
| `ended` | `rx_ms`, `media` | media ended naturally |
| `error` | `code`, `message` | anything the receiver could not do |

The receiver **must** accept transport intents from other senders (that is what makes pause/seek/
volume/stop work from the Google Home app and from the TV remote), and forward them as `intent`.
It must not act on them silently: the controller owns the group.

### 3.3 Presence and takeover

- `launch` = `receiver` namespace `LAUNCH` with `appId = 28028CD2` (see `lib/castv2.py`).
- **Foreign takeover**: the receiver's `receiver_status.applications[0].appId` is not ours, or the
  device reports `IDLE`/a different session. Detected within one poll (≤ 1 s): the controller
  stops the group, logs `device_lost`, goes to `idle`, and **never** relaunches by itself.
  Relaunching needs a new share (acceptance: "clean end if a foreign cast takes the Chromecast").

## 4. Media URL scheme (frozen surface, implementation free)

Served by CT 121 on `:7640`:

| Path | Content |
|---|---|
| `/media/<sid>/manifest.mpd` | DASH manifest for the receiver (video + audio), `SegmentBase` + `indexRange`, `type="static"`, `duration` known |
| `/media/<sid>/audio.m4a` | the audio track alone, for the group's Default Media Receiver, `Range` supported |
| `/media/<sid>/<n>` | byte-range endpoints referenced by the manifest |

- `<sid>` is opaque, per session, revoked on session end.
- The **video and audio come from the same extraction** (same video id, same moment): the AAC
  track the group plays is the AAC track the manifest carries, so encoder priming is identical on
  both sides (A13: WAV vs M4A differ by 70 ms; never mix containers).
- Media is **never accumulated on disk** (CT 121 has 2.9 GB free and nothing may be deleted):
  streamed through a range proxy, bounded memory. A googlevideo URL is a short-lived token:
  **redacted in every log and evidence file** (`scheme://host/<6 chars>…`).
- Codecs: best `avc1` whose height ≤ `MAX_HEIGHT` (default 1080) plus `mp4a` (format 140 or the
  nearest AAC). Anything the Chromecast refuses is task C's problem to report, not to work around.

## 5. HTTP API (owner app, LAN only)

Bind `192.168.2.21:7640`, accept only `192.168.2.0/24`. All bodies JSON, all replies JSON except
`/media/*`, `/receiver.html` and `/`.

| Method + path | Body | Reply |
|---|---|---|
| `POST /api/share` | `{"url": str, "start": float?, "measure": bool?}` | `202 {"session_id": str, "state": "resolving"}` |
| `POST /api/control` | `{"action": "play"\|"pause"\|"seek"\|"volume"\|"stop", "position": float?, "level": float?, "muted": bool?}` | `200 {"ok": true, "state": str}` |
| `GET /api/status` | — | `200 {"state", "session_id", "video_id", "position_video", "position_group", "digital_error_ms", "p95_ms", "rate", "steady_since", "quiet", "group_owner", "delta_v2", "last_event", "last_error"}` |
| `GET /healthz` | — | `200 {"ok": true, "owner": "cast-sender"}` |
| `GET /receiver.html` | — | the receiver page (**path frozen**) |
| `GET /` | — | minimal remote UI: state, one share field, play/pause/seek/stop, volume |

`measure: true` replaces the group's audio with `assets/click_train_2310.m4a` while the video keeps
playing the normal track — that is how the acoustic gate measures a *real* session
(`notes/measurement-protocol.md` §2). The video track is untouched, so the picture stays the
reference.

States: `idle → resolving → starting → playing ⇄ paused → ended → idle`, plus `quiet_refused`.
A new share while `playing` stops the current session cleanly first (acceptance: "new video").

## 6. Configuration (`/etc/castsender.conf`, systemd `EnvironmentFile`)

```
CHROMECAST=192.168.2.80
APP_ID=28028CD2
GROUP=Cuisine
HTTP_BIND=192.168.2.21
HTTP_PORT=7640
DELTA_V2=0.0          # = OBS_0, measured; 0.0 until the acoustic calibration exists
RATE_MAX=0.002        # |rate-1| ceiling for a trim
MAX_HEIGHT=1080
QUIET_START=          # empty = quiet hours disabled
QUIET_END=
QUIET_POLICY=refuse    # refuse | video_only
```

`APP_ID` and the device address are not secrets. Nothing else may be added without approval.

## 7. Group ownership and quiet hours

- `state/group.owner`: a small JSON file `{"owner": "cast-sender"|"cast-relay", "pid": int,
  "since": float}` created atomically (`O_EXCL`), refreshed as a heartbeat every 5 s, considered
  stale after 15 s (then takeover is allowed and logged). The v0.1 relay reads it and behaves as
  "the TV is not playing YouTube" while it is not the owner (no LOAD, no SEEK, no mute, no volume
  mirroring — heartbeat only). **The v0.2 owner always wins**: it is created by an explicit user
  action (a share), the relay's never is.
- Quiet hours (**not used**: the owner does not want them, 2026-10-26; both keys stay empty and the
  gate does not test them): outside `QUIET_START…QUIET_END` no audible playback starts. `refuse` = the share
  answers `quiet_refused` and nothing plays; `video_only` = the video plays on the Chromecast and
  the group is never loaded. Inside the window nothing changes (a session running when the window
  opens is **not** stopped — the rule is about starting).

## 8. Rollback hooks (the gate will execute them)

- `POST /api/control {"action":"stop"}` stops the group and the receiver, restores the group volume
  and leaves the Chromecast on its idle page.
- The service's `ExecStopPost` runs the same cleanup as `lib/emergency.py` for the group **and**
  stops our receiver app on the Chromecast.
- Boot cleanup: on startup, stop any DMR session **we** created (a session id we recorded) and undo
  any mute we set. Never touch a session we did not create.

## 9. Definition of "evidence" for a task in this project

A task is done when a command in `evidence/README.md`'s style reproduces a number, on this LAN,
archived in `evidence/`, with the command that produced it in the lab note. "Unproven, because X"
is an acceptable answer. A fabricated or model-generated number is a failure of the task.

---

## 10. Amendments after the first eight tasks (integrator, 2026-10-25)

Each one was forced by a merge or by a measurement. Nothing above is retracted; these only pin
what was ambiguous.

1. **§1 — the units of `Controller.start()`.** `start(now, video_pos, group_pos, lead_s)`:
   `video_pos` and `group_pos` are **media positions**, in seconds, where each side sits parked and
   loaded when the call is made. They are *not* instants. (The first integration wired wall-clock
   seconds in and the plan landed years away; `tests/v020/test_integration_seams.py` now fails if it
   ever happens again.)
2. **§1 — who loads.** The executor (the session) performs `load`/`prepare`; the control law never
   loads and never seeks. `_stopgap_*` paths exist only while a seam is a stub and say so in the log.
3. **§1 — the parked landing delay.** `lead_s` is the delay between telling the **parked** group to
   play and it actually playing: **44 ms** (`evidence/27`, case C: −44 ms mean). It is *not* the
   0.55 s of a LOAD from idle (`evidence/16`, A4) — that path does not exist in v0.2.
   *Review 2026-10-26:* 44 ms is the **muted bench** mean (σ 30 ms); real use scattered ~±80 ms
   (A25). Simulated at 80 ms it is absorbed (`evidence/44b-sim-video-side.json`,
   `group_landing_80`); on hardware it is unmeasured. See `notes/consensus.md` §8.5.2.
4. **§4/§5 — `MediaProxy` needs an HTTP face.** Add
   `MediaProxy.serve_http(method, path, headers) -> (status, headers, body)`; `/media/<sid>/<n>` is
   delegated to it by the API. Until task B implements it, the API answers `501
   media_stream_unavailable` rather than pretending.
5. **§5 — the measurement track lives at `/assets/measure/<file>`** (read-only, `.m4a`/`.wav`, no
   path traversal), because `measure: true` must hand `click_train_2310.m4a` to the group while the
   video keeps its own track.
6. **§6 — `CastGroup.volume_state() -> {"level", "muted"}`** is added: `set_volume(level, muted)`
   alone cannot express "mute" without re-commanding a level. Until it exists, `POST
   /api/control {"action":"volume","muted":true}` answers `409 no_known_level`.
7. **§5 — `quiet` is an object**: `{enabled, start, end, policy, allowed_now, decision}`;
   `steady_since` is an epoch in seconds or `null`.
8. **§5 — quiet refusal is a 202**, with `state: "quiet_refused"` in the same envelope as a nominal
   share: the request was accepted, the *playback* is refused.
9. **§3/§1 — command routing convention** (`Command.kind`): any `kind` in `CONTROLLER_MESSAGES` goes
   to the receiver; any other `kind` goes to the group, through the aliases `play|pause|stop|seek|
   prepare|load|volume` (and their `group_`-prefixed forms); `payload["target"] == "group"` forces
   the group (needed because `pause` exists on both sides).
10. **§2 — clock bias: OPEN, not settled** (review 2026-10-26). A clock bias that is the same in
    every session is absorbed by the acoustic calibration of `DELTA_V2`. The part of the
    estimator's error that changes from one session to the next is not: the simulation's
    `clock_residual` case (12 ms → steady p95 12.7 ms, convergence 271 s) is therefore a real
    bound, not a re-calibration scenario. The estimator gives 0.5 ms on the fake (task D); the
    number that decides is task C (d) on the device. Recorded as a disagreement in
    `notes/consensus.md` §8.5.1. (The earlier wording of this amendment called the case
    a re-calibration scenario; it is kept in git history, `9f950fd`.)

11. **§2 — the video playhead is filtered, and which filter is a measurement** (2026-10-26). On the
    device's own 238 reads, a 30 s mean gives σ 5.1 ms / p95 10.2 ms with +1.0 ms bias; a 10 s
    median gives +21 ms of bias and v0.1's upper envelope +82 ms (the TV's sawtooth ran only slow,
    this device's scatters both ways). `sync.VideoClock` implements the mean; the scoring table and
    the reasoning are in `notes/labo-sync.md` §5. The consequence is recorded honestly: with the
    measured noise the steady p95 is ~13 ms — inside the acceptance's 20 ms, above the 10 ms the
    simulation met with a perfect video side.

12. **§4/§3 — the receiver plays the stream with its own MSE reader, not CAF's player**
    (2026-10-26/27, `notes/labo-receiver.md` §C6–C11). The manifest of §4 is unchanged and still
    the input, but only for its two byte ranges per track (`Initialization@range`,
    `SegmentBase@indexRange`): `receiver/mse.js` parses the `sidx` itself and appends fragments by
    byte range. Measured on the device: the synthesised MPD played on neither CAF's player nor
    the platform DMR, and the MediaSource accepts **one** SourceBuffer, so each track has its own
    element and MediaSource (audio element kept on the video's clock by rate, re-seated only past
    250 ms). The reader refills from a 2 Hz timer, keeps 12 s ahead and 20 s behind, and calls
    `endOfStream()` after the last fragment so the element fires `ended`.
13. **§5 — the receiver page is a stable loader.** The device caches by **path** and ignores
    query strings and `Cache-Control` until it reboots (measured). `/receiver.html` (frozen) reads
    `/b/<now>/build.txt` (`no-store`) and loads `/r/<build>/receiver.js` and `/r/<build>/mse.js`;
    `<build>` is the first 12 hex of the script's sha1. The first loader generation's paths
    (`/build.txt`, `/receiver.js`) are answered with a 302 to the current build.
14. **§7 — one lock file, one path.** cast-sender writes `<STATE_DIR>/group.owner`
    (`/opt/castsender/state/group.owner` on CT 121); the relay reads the path named by
    `OWNER_FILE` in `/etc/castrelay.conf`, for both `run-relay.sh` and its `ExecStopPost` wrapper.
    Two checkouts with two relative defaults had them reading different files (review
    2026-10-27); `tools/safety_guards.sh` now checks the example configuration.
