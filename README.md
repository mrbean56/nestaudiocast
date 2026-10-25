# nestaudiocast — v0.1

Play the audio of a YouTube video on a Google Cast **speaker group**, in sync with the video
playing on a TV in the same room, with a **measured** A/V offset under 150 ms.

The TV keeps its native YouTube UI and its own sound. The speakers follow.

```
        phone ──cast──►  TV (Chromecast)  ── HDMI ──► screen + TV speakers
                              │
                              │ CASTv2 MEDIA_STATUS (read-only, 4 Hz)
                              ▼
                     relay (this repo)
                              │  yt-dlp → audio file → HTTP
                              ▼
                   Google Cast speaker group  ──► kitchen speakers
```

## What this is not

It is **not** a cast target. You **cannot** cast to it from the YouTube app: Google requires a
device certificate signed by Google for the `deviceauth` exchange, and a homemade receiver is
refused before it ever receives a `CONNECT`. This was tested, not assumed — a full CASTv2
receiver with mDNS advertisement works with a non-validating sender, while a real Google
sender stops at authentication and closes
(`evidence/11-virtual-receiver.log`, `notes/track1-protocol.md`).

So the phone casts to the **TV**, exactly as before, and the relay shadows that session.

Also refuted along the way, with evidence: putting the video+audio in one group (a Cast group
plays the sound and shows a "now playing" card, no picture — `evidence/18`), and any hardware
fix under 100 CAD (Nest Audio has no audio input and a Cast group only accepts network audio —
`notes/track3-hardware.md`).

## Measured results

| What | Value | Where |
|---|---|---|
| Perceived offset, calibrated | **−20 ms ± 4 ms**, three points inside one video, drift **0.0 ms/10 min** | notes A30 |
| Perceived offset across a video change | +8 → +34 ms (a step, not a drift) | notes A30 |
| Perceived offset, uncalibrated | +295 ms ± 6 ms | same |
| Digital error, 10-minute session | +7.7 ms mean, σ 10.3 ms | `evidence/29-session-report.json` |
| Drift over 10 min | −5.5 ± 1.2 ms | same |
| Group clock drift, 600 s | −12.4 ± 1.0 ms/10 min | `evidence/17` |
| Seek → re-lock | 6–20 s | `evidence/21`, `26` |
| Session length verified | 911 s continuous, 5 acoustic points | notes A30 |
| Real video (2 h 23, 134 MB) | audio ready in 10 s, deep seek to 2 h followed | `evidence/33`, `34` |

## How the sync works

1. The relay reads the TV's playhead over CASTv2 (`urn:x-cast:com.google.cast.media`).
   The YouTube receiver reports a **sawtooth** playhead (it advances at ~0.85× and jumps every
   1–1.5 s), so a single read is off by up to ±140 ms; `TvClock` takes the upper envelope over a
   10 s window instead (σ 4.6 ms).
2. It downloads the video's audio with `yt-dlp` and serves it over HTTP with Range support.
3. It drives the kitchen group with the Default Media Receiver. **`SET_PLAYBACK_RATE` is a
   no-op on this hardware** (asked 1.005, measured 0.999957), so the only real actuator is a
   reload: the group is parked **paused** slightly ahead of the TV, then `PLAY` is sent at the
   calculated instant (allowance: LOAD σ 202 ms, SEEK σ 57 ms, PAUSE→SEEK→PLAY **σ 30 ms**).
4. `delta` absorbs the difference between the two rendering latencies
   (TV panel vs speaker group). It is calibrated **acoustically**, once, and is the only
   constant in the system. It is not measured by the loop, and nothing in the running system
   detects that it has drifted.

## Requirements

- A Google Cast **speaker group** with no video device in it (the relay refuses any other kind).
- A TV whose cast app exposes the media namespace (YouTube does).
- Linux host with Python 3.11+, `ffmpeg`, `yt-dlp`, and LAN access to the devices.
- Optional: a phone with wireless ADB (for the acoustic calibration and measurements).

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install zeroconf pychromecast numpy cryptography yt-dlp
mkdir -p assets/yt evidence logs
cp tools/../castrelay.conf.example /etc/castrelay.conf   # edit GROUP / TV / DELTA
```

Run:

```sh
.venv/bin/python relay/cast_relay.py --group "Cuisine" --delta 0.265 \
    --mirror-volume --log evidence/relay.jsonl
.venv/bin/python relay/cast_relay.py --dry-run          # watch the TV, touch nothing
```

Systemd unit and wrapper: `tools/cast-relay.service`, `tools/run-relay.sh`.

## Safety

This runs in a house with other people in it, so three rules are enforced in code:

- **Never play on a group that contains a video device.** A cast group is announced over mDNS
  and its membership can be read without playing anything (`MULTIZONE_STATUS`).
  `lib/groupguard.py` refuses anything else — on the LAN this was written for, *all* the
  pre-existing groups contained the living-room TV, and starting a session on one of them takes
  the TV over.
- **The TV is read-only** for the relay: no `LAUNCH`/`STOP`/`SET_VOLUME`, only `GET_STATUS`.
- **Silence first, then restore.** `lib/silence.py` mutes, and stops any session *it* created;
  `lib/emergency.py` is the one-command panic stop and never changes a group's volume (volume on
  a group propagates to its members — i.e. to the TV).

## Reproducing the measurements

```sh
./tools/prove.sh --fast     # offline proofs, no LAN, no sound
```

Measurement instruments:

- `measure/analyze_offset.py` — perceived offset from one room recording, using a 1 kHz beep on
  the TV and a non-harmonically-related 2.31 kHz click on the speakers; validated against known
  offsets, refuses to conclude on aliasing or dispersion (`evidence/14`).
- `probe/actuator_test.py` — repeatability of each way of (re)starting the group.
- `measure/analyze_session.py` — digital error/drift report for a relay session log.
- `tools/acoustic_session.py` + `tools/phone_record.sh` — the full calibration session
  (phone microphone over wireless ADB, no app needed).

A perceived-offset measurement on **arbitrary** content is *not* implemented: without a marker,
two copies of the same music cannot be separated reliably. Two attempts and their failure are
documented in `notes/00-labo.md` (A29).

## Repository layout

| Path | What |
|---|---|
| `lib/castv2.py` | minimal single-threaded CASTv2 client (TLS + protobuf, select/recv/send) |
| `relay/cast_relay.py` | the relay: TV clock, controller, group sink, audio cache |
| `relay/sim.py` | closed-loop simulation of the controller against a simulated device |
| `lib/groupguard.py`, `lib/silence.py`, `lib/emergency.py` | safety machinery |
| `measure/`, `probe/`, `tools/` | instruments, experiments, scenario drivers |
| `notes/00-labo.md` | lab notebook: hypothesis → measurement → conclusion, per attempt |
| `notes/consensus.md` | what was decided, what was refuted, open disagreements |
| `notes/measurement-protocol.md` | the reproducible A/V measurement protocol |
| `notes/rollback.md` | rollback procedure (tested) |
| `evidence/` | every measurement, with `evidence/README.md` saying what each file proves |

Design notes are in **French**; code and this README are in English.
The requirement-by-requirement audit is `notes/ACCEPTANCE.md`.

## Status

v0.1 — working proof of concept, running daily on one household. Known gaps:

- the perceived offset drifts by a step of ~+30 ms at each video change (within a video it is
  flat to the millisecond) — each new video re-locks with a slightly different pipeline latency,
- `delta` stability across content (24p/30p, HDR) and TV picture modes is unmeasured,
- behaviour during **ads** is untested,
- a 10-minute session was verified digitally, not acoustically,
- the acoustic calibration currently requires a separate recording and a human.

## License

MIT — see `LICENSE`.
