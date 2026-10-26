# Lab — Task D: the sender's link to our own receiver (branch `feat/sender-link`)

Scope: `lib/v020/chromecast.py` (the `SenderLink` behind `lib/castv2.py`),
`tests/v020/fake_receiver_net.py` (a localhost CASTv2 receiver on the real wire protocol) and
the tests around them. Everything measured here is **127.0.0.1 only**: no device, no speaker,
no group, no TV, no audio. The one LAN contact is a read-only HTTP `eureka_info` on `.80`
(L6), which launches nothing.

Contract anchors: `docs/v020-contracts.md` §3.1 (messages), §3.2 (receiver → controller),
§3.3 (presence and takeover), §2 (`Sample`); A23 in `notes/00-labo.md` (the unbounded inbox).

---

## L1 — the clock estimator

**Hypothesis.** The receiver's clock is `rx_ms = performance.timeOrigin + performance.now()`
(§3), sampled at the same statement as the media position. The controller's base is monotonic
milliseconds. Four timestamped pings are enough to pin the offset to about a millisecond on a
LAN, and the estimator can say how much it trusts itself.

**Derivation.** One round trip, controller timestamps `t0` (just before the write) and `t3`
(just after the read), receiver timestamp `rx` (when it handled the message):

```
rx = t0 + d_in  + phi
t3 = t0 + d_in  + d_out
r  = t3 - t0 = d_in + d_out                    (round trip)

phi_hat = rx - (t0 + t3) / 2 = phi + a,      a = (d_in - d_out) / 2
|phi_hat - phi| = |a| <= r / 2                 (always, no assumption)
```

`a` is the **path asymmetry**. It is not observable from one side: a constant 6 ms asymmetry
shifts every estimate by 6 ms and nothing in the round-trip data shows it.

**Why min-RTT selection, not averaging.** Queueing adds delay independently on each side, so
`a` is smallest when `d_in` and `d_out` are both near their floor — that is exactly the sample
with the smallest `r`. Averaging mixes clean and congested samples and can only make the
answer worse. Selection has a second, less obvious benefit: a late `t3` (scheduler hiccup on
the controller) inflates *that ping's* `r`, so the contaminated sample is the one that gets
discarded. This was measured, not assumed (L2, and the bug in L2.3).

**What `residual_ms` means here.** The robust dispersion of the per-sample offsets around the
selected one: `1.4826 x median(|offset_i - offset_selected|)` (a MAD scaled to a σ-equivalent).
One congested ping cannot inflate it. The worst disagreement is reported next to it
(`last_ping_stats["spread_ms"]`), and the absolute bound `r_min / 2` is `bound_ms`. The
residual is a measure of **repeatability**; it is blind to `a`, by construction (L1.4).

**Monotone guard and jump guard.** Two different failures, two different responses:

| Symptom | Detected by | Response |
|---|---|---|
| a receiver timestamp goes **backwards** (> 250 ms) | every message with `rx_ms` | flag, keep the previous `Sample` (never feed a rewound clock to the loop) |
| the estimated offset **steps** by more than `max(50 ms, 8 x residual)` | `ping()` against the accepted estimate | flag `clock_flagged`, keep the accepted estimate, log `clock_jump_flagged` |
| a new `hello` (new page, new `timeOrigin`) | `_on_hello` | drop the base (`clock = None`), re-ping; the caller never sees a stale clock |

The 250 ms tolerance for "backwards" is deliberate: reordering of a few ms is possible, a
system-clock step (the case the guard exists for) is far larger. A flagged clock is **not
followed** until the caller re-bases it with `reset_clock()` — i.e. a decision, not a silent
recovery.

**Measurement** (`evidence/42-sender-link-fake.json`, seed 7): 12 trials x 8 pings against a
fake whose RTT is uniform 0–40 ms with ±0.5 ms of path asymmetry, and whose true offset is
known (`86 400 000.0 ms`).

| quantity | median | max |
|---|---|---|
| absolute error vs the fake's true offset | **0.494 ms** | **1.141 ms** |
| reported `residual_ms` | 0.579 ms | 1.206 ms |
| selected RTT (`r_min`) | 2.12 ms | 8.30 ms |
| absolute bound `r_min / 2` | 1.06 ms | 4.15 ms |

200 more trials of the pure estimator on synthetic samples (no socket, no thread):
error median 0.26 ms / max 0.498 ms, residual median 0.355 ms / max 1.106 ms.

**Conclusion.** With a 0–40 ms RTT distribution the estimate stays **under 1.2 ms** of the
truth over 12 batches and the reported residual stays **under 1.3 ms** — the ≤ 2 ms criterion
is met with margin, and the residual is measured, not assumed. Run the criterion as
`ping(8)`; `ping(5)` (the default) gives the same median with a slightly wider tail because
the minimum RTT is drawn from fewer samples.

### L1.4 — the blind spot, measured
Same fake, `path_asym_ms = 6`, no jitter, RTT floor raised to 12 ms so the model stays
physical: the estimate is **+5.68 / +5.55 / +5.24 / +5.99 ms** wrong while the residual stays
**0.13–0.64 ms**. That is the whole story: a one-sided estimator cannot see a constant path
asymmetry. On the real device the asymmetry is unknown, so the honest statement is "the
estimate is accurate to the *path's* asymmetry, which this cannot measure" — and it is why the
residual must never be read as an absolute error bar. `bound_ms` (= `r_min / 2`) is the bound
*if* the path is symmetric.

---

## L2 — speaking the protocol

**Hypothesis.** `LAUNCH` + `CONNECT` on the app's transport + a `hello` on our namespace is
enough to know the receiver is ours and ready; every controller message needs a `seq` the
receiver echoes.

**Measurement** (`test_chromecast_net.py`, `fake_receiver_net.py`). The fake is a real TLS
server with length-prefixed protobuf `CastMessage`s: `receiver` (GET_STATUS / LAUNCH / STOP /
SET_VOLUME), `tp.connection`, `tp.heartbeat`, and our namespace with `hello`, `clock_reply`,
`state` and the 8 controller messages. 20 wire tests, all green.

- `launch()` does a read-only `GET_STATUS` first: our app already running → adopt (sequel
  `launch()` twice = 2 hellos, no relaunch); a **foreign** app → `LostDevice`, it never
  launches over somebody else; idle → `LAUNCH`, then `CONNECT` to the returned transport, then
  wait for `hello` (a timeout is `LinkDown`, not a silent success).
- `send()` validates before the wire: unknown type → `ValueError`; `type`/`seq` in the payload
  → `ValueError`; missing required field (`play_at.rx_ms`, `seek.media`, `load.*`) →
  `ValueError`; `rate` outside 0.5–2.0 → `ValueError`. `seq` is a strictly increasing counter
  owned by the link, never reset by a reconnect (`1 → 2` across the forced close in L4).
- `drain()` returns the receiver messages in arrival order and consumes `clock_reply` only when
  `ping()` asked for it (a pong is not useful to the control law).
- `latest()` is a typed `contracts.Sample`: `rx_ms`, `media`, `rate`, `player_state`,
  `buffered_end`. A malformed `state` is logged and skipped, never half-parsed.

### L2.3 — a real bug the fake found: Nagle
The first measurements had sporadic RTT outliers of ~37–48 ms on a 0–40 ms model, and an
unsolicited `RECEIVER_STATUS` sometimes took ~40 ms to become readable. Cause: small TLS
writes with Nagle enabled and the peer's delayed ACK. Fixed by setting `TCP_NODELAY` on the
TLS socket, both in `SenderLink._open` and in the fake. This is not cosmetic for v0.2: a
`play_at` carries a target receiver timestamp, and 40 ms of buffering on the command path is
most of the acceptance budget. After the fix the same fake gives the L1 table (error ≤ 1.2 ms).
The concrete symptom before the fix: residuals up to 20 ms and takeover reads that needed an
extra read.

### L2.4 — castv2's request window
`CastLink.request()` polls in `pump(min(0.02, ...))` slices, so one `GET_STATUS` costs about
20 ms even on loopback (measured: 20.1 ms). It is castv2's behaviour, not ours, and it is why
"one poll" below is ~20 ms rather than ~1 ms.

---

## L3 — presence and takeover (§3.3)

**Hypothesis.** Loss of the device is visible in two independent places, and the loop must
notice within one poll.

Paths, in the order they are read:

1. **an unsolicited `RECEIVER_STATUS`** on the `receiver` namespace — a foreign `appId`, or no
   application at all (a stop from another sender). This is what a real device broadcasts when
   somebody else casts or stops the app, and it costs no round trip;
2. **`is_ours()`** — one `GET_STATUS`, compared against `app_id`;
3. **an `IDLE` `state` while we believe we loaded media** — the receiver page is up but the
   session is gone.

Any of them sets `lost_reason` (`foreign_app:<id>`, `app_not_running`, `idle_while_ours`,
`status_failed:*`, `link_error:*`), logs `device_lost` once, and makes every device command
raise `LostDevice`. A status that arrived *before* our `LAUNCH`/`CONNECT` was answered is
discarded (`_adopt_at`) — that stale-doc bug made a fresh `launch()` report `app_not_running`
on the fake's very first empty `CONNECT` reply.

**Measurement** (`evidence/42-sender-link-fake.json`, takeover → `appId 9A1B2C3D`):

| path | reads/polls | latency | verdict |
|---|---|---|---|
| one `is_ours()` poll | **1** | **20.1 ms** (castv2's request window, L2.4) | False, `foreign_app:9A1B2C3D` |
| one `drain()` after the broadcast | **1** | **0.056 ms** | detected, `foreign_app:9A1B2C3D` |
| stop from another sender | 1 read | ~0.05 ms | detected, `app_not_running` |
| `IDLE` while media loaded | 1 read | ~0.05 ms | detected, `idle_while_ours` |

Then `send("pause")` raises `LostDevice` (recorded in the same file). Against §3.3's
"detected within one poll (≤ 1 s)", the measured worst case is one poll at 20 ms.

---

## L4 — reconnect, bounded backoff, and what is never silently lost

**Hypothesis.** A socket that dies (Wi-Fi blip, receiver restart) must not look like a session
that ended, and a reconnect must not silently start a new session.

- `reconnect()` = 5 attempts at most, delays 0.05 / 0.10 / 0.20 / 0.40 / 0.80 s (±20 % jitter),
  total ≤ 1.6 s of waiting; then `LinkDown`. A **foreign** app during the retry raises
  `LostDevice` at once (retrying over somebody else's app is not a transient failure).
- The session survives the outage: `seq` continues, `last_state` is kept, `hellos` counts the
  handshakes, `transport` is preserved. A `hello` while media was loaded logs
  `session_maybe_lost` — the link says it cannot promise the page kept its media, instead of
  pretending.
- `send()` never retries by itself: a transport command retried blindly could load twice. It
  raises `LinkDown` and the caller decides.
- A **transport change** seen in a status (receiver page reconnected/relaunched) re-attaches
  (`CONNECT` to the new transport) and waits for the new `hello`; the clock is re-based there.

**Measurement** (`evidence/42-sender-link-fake.json`, forced server-side close):

| quantity | value |
|---|---|
| `send()` while the socket is dead | `LinkDown: link dropped while sending` |
| reconnect time (1 attempt) | **24.1 ms** |
| `seq` before → after (next command) | 1 → **2** |
| `hellos` before → after | 1 → **2** |
| transport before → after | unchanged (`transport-1d6a7db4`) |
| `is_ours()` after | True |
| transport restart (new page transport) | re-attached, new `hello`, `hellos` 2 → 3 |

A failed reconnect is bounded by the schedule above; the single "dead device" test asserts the
attempt budget and that it ends in `LinkDown`.

---

## L5 — the bounded inbox (A23)

**Hypothesis.** A23's root cause was an inbox that grew for hours. The link must keep order,
cap the queue, and make any loss visible.

**Measurement.** 1000 `state` messages injected in one burst by the fake, client not draining
(`evidence/42-sender-link-fake.json`):

```
injected 1000, received 256, dropped 744, largest drain 256, bound 256, ordered true
```

`INBOX_MAX = 256` = 64 s at the 4 Hz state cadence. `dropped` counts every message evicted,
and `inbox_overflow` is logged (first time, then every 256). Nothing is silently lost: the
caller can see how many were skipped.

---

## L6 — what is NOT proven (and what was deliberately not touched)

- **No real CASTv2 connection to the Chromecast tonight.** `.80` is owned by the receiver task;
  I did not open a transport, did not `LAUNCH`, did not `CONNECT`. Everything about launch /
  hello / takeover on real hardware is therefore **unproven** — the protocol is exercised
  against `tests/v020/fake_receiver_net.py`, which implements §3 but is not the device.
- **Real RTT and real asymmetry are unknown.** The 0–40 ms distribution and ±0.5 ms asymmetry
  are synthetic. On the LAN the RTT should be ~1–5 ms (L1's `r_min` on the fake is 2.1 ms);
  the device's real asymmetry, and therefore the absolute clock error, is **not measured**.
  `bound_ms` is the honest bound *if* the path is symmetric.
- **The receiver page's own behaviour** (whether it sends `hello` on a namespace reconnect, how
  fast it emits 4 Hz states under load, whether its `timeOrigin` survives a reload) belongs to
  the receiver task. `SenderLink` re-bases on every `hello` and tolerates both cases, but only
  the fake proves that.
- **`launch()` costs on hardware.** One `GET_STATUS` + `LAUNCH` + `CONNECT` + `hello` on a real
  Chromecast is not timed here. Contract §3.3 only requires the *takeover* detection within one
  poll, which is measured on the fake.
- **Only `eureka_info` was read on `.80`** (`evidence/42-eureka-info-80.json`): HTTP 200 in
  27.9 ms, `Chromecast Salon`, firmware 12, `stable-channel`. It is a read-only `GET`; no app
  was launched and no CASTv2 socket was opened.
- **Not touched, by rule:** `lib/castv2.py` (unchanged, the v0.1 relay depends on it),
  `lib/groupguard.py`, `lib/silence.py`, `lib/emergency.py`, every other `lib/v020/*` module,
  the group, the TV, `.80`'s app state. `tools/prove.sh --fast` stays green.

---

## Commands (reproduce everything above)

```sh
cd ~/opencode/nestaudiocast-wt/link

# whole offline suite (51 tests, ~11 s)
./tests/v020/run.sh

# just this task
.venv/bin/python -m unittest discover -s tests/v020 -p 'test_chromecast_offline.py' -v
.venv/bin/python -m unittest discover -s tests/v020 -p 'test_chromecast_net.py' -v

# the numbers archived in evidence/42-sender-link-fake.json (+ its event log)
.venv/bin/python tests/v020/fake_receiver_net.py --bench --seed 7 --trials 12 --pings 8 \
    --log evidence/42-sender-link-fake.log --out evidence/42-sender-link-fake.json

# read-only device identity, launches nothing
.venv/bin/python -c "import sys,json; sys.path.insert(0,'lib'); \
from v020.chromecast import probe; print(json.dumps(probe('192.168.2.80'), indent=2))"

# safety suite
./tools/prove.sh --fast
```

Caveat for the integrator: `./tests/v020/run.sh <module>` (the single-module mode) is broken on
Python 3.13 — it expands to `python -m unittest discover -s tests/v020 -p <module>.py` with the
arguments in an order 3.13's argparse-based CLI rejects (`unrecognized arguments: -s …`). Run
`unittest discover` directly as above, or fix `run.sh` (not my file).
