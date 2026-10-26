# Lab note — task B: media resolver + range proxy (2026-10-25, night)

Every entry is hypothesis → measurement → conclusion, on the numbers actually archived in
`evidence/40-*`. Ran on **CT 121 `castrelay` (192.168.2.21)**, `yt-dlp 2026.10.21`, `ffprobe 7.1.5`,
Python 3.13.5. Nothing audible was touched: no speaker, no group, no Chromecast, no TV.

Measured with `yt-dlp 2026.10.21` (the venv package on CT 121 and on the authoring host are the
same version — checked both, `2026.10.21`).

---

## B1 — one extraction gives both tracks, fast enough

**Hypothesis.** A single `yt-dlp` extraction of one video id yields both an `avc1` video URL and an
`mp4a` audio URL, in well under the 5 s budget.

**Measurement** (`evidence/40-resolve.json`): three real videos, `time.monotonic()` around
`resolve()`:

| label | video | duration | resolve | video | audio |
|---|---|---|---|---|---|
| short | `jNQXAC9IVRw` | 19 s | **1.452 s** | itag 133, 240p | 140 |
| long | `ai_gI0Jbr_E` | 8630 s | **0.960 s** | itag 137, 1080p | 140 |
| capped | `dQw4w9WgXcQ` | 213 s | **1.548 s** | itag 136, **720p** | 140 |

The capped row is the `MAX_HEIGHT=720` check: the same video also offers a 1080p `avc1`
(`tallest_avc1 = 1080`, recorded next to the pick), so the cap is what produced 720p.

**Conclusion.** 1.0–1.6 s per video, ≥3× under budget, and the height cap works. Both tracks come
from one `extract_info` call — the offline test counts the calls.

## B2 — the trap: the best-looking format has no byte range

**Hypothesis.** "Best `avc1` ≤ max_height" can be taken literally on `height`/`tbr`.

**Measurement.** For `ai_gI0Jbr_E` the extractor lists `270` (avc1, 1080p, **tbr 4703**, protocol
`m3u8_native`, a `manifest_url`, no byte range) and `137` (avc1, 1080p, tbr 2324, protocol
`https`, a 2 507 362 728-byte file). Sorting by `(height, fps, tbr)` picks **270** — an HLS variant
that a range proxy cannot address at all.

**Conclusion.** `_is_direct()` is part of the selection rule, not an optimisation: direct
`https`/`http`, no `manifest_url`, no `fragments`, no DRM. `tests/v020/fixtures/ytdlp_recorded.json`
keeps `270` next to `137` on purpose, so the offline test fails if the filter is ever dropped.

## B3 — the byte layout is discoverable without ffmpeg

**Hypothesis.** yt-dlp does not give `init_range`/`index_range` for YouTube, so the proxy must find
them in the file's own boxes, within one 256 KiB probe.

**Measurement** (`evidence/40-mpd.json`, `sizes`): a `Range: bytes=0-262143` request returns
`ftyp(0,28) moov(28,714) sidx(742,19160)` for the video and `ftyp(0,24) moov(24,699)
sidx(723,10412)` for the audio, with `Content-Range: …/2507362728` and `…/139674474`. Those totals
equal the extractor's own `filesize` for both tracks (2 507 362 728 / 139 674 474).

**Conclusion.** `init_range = 0…end(moov)` and `index_range = sidx`, from the file, no ffmpeg, one
extra request per track, 256 KiB of RAM. An extractor that *does* provide both ranges is used
as-is (code path + unit test).

## B4 — the synthesised MPD is a manifest a real demuxer accepts

**Hypothesis.** A hand-built `isoff-on-demand` MPD — `type="static"`, known
`mediaPresentationDuration`, `SegmentBase@indexRange` + `Initialization@range`, both adaptation
sets, `BaseURL` 0/1 — is enough for `ffprobe`.

**Measurement** (`evidence/40-mpd.json`): `ffprobe -show_format -show_streams` over
`http://192.168.2.21:7641/media/<sid>/manifest.mpd` → rc 0, **h264 1920×1080** + **aac 44100 Hz
stereo**, `format.duration = 8630.000000` (the video is 8630 s). `ffprobe -i` prints both streams;
the proxy log shows the requests it made: the manifest, then `…/0` and `…/1`.

**Conclusion.** Done-when item met. `bandwidth` is computed from the measured file size
(`total*8/duration`), not invented.

## B5 — a seek to 7200 s returns the source's own bytes

**Hypothesis.** `sidx` (timescale 12800, 1594 subsegments) maps media second 7200 to one fragment,
and that fragment fetched through the proxy is byte-identical to the same range fetched from the
source directly.

**Measurement** (`evidence/40-seek-7200.json`):

| quantity | value |
|---|---|
| 7200 s → byte range | **2031653270–2033379567** (fragment starts at 7195.84 s) |
| bytes fetched direct from the source | 1 726 298 |
| bytes fetched through the proxy | 1 726 298 |
| sha256 (both) | `d7d0189e…b611e403fb` — equal |
| first PTS of that fragment decoded locally (bytes straight from the source) | **7195.84 s** |
| `ffprobe -read_intervals 7200%+#2` on the **proxied track URL** | **7195.84 s**, 0.28 s |

The last two lines are independent of the proxy's own output: one decodes bytes fetched directly
from googlevideo, the other is ffmpeg seeking the proxied file by itself.

**Conclusion.** Correct bytes, verified against the source, not against our own echo.

**Not measured, and it matters for task C:** `ffprobe`'s **DASH demuxer** cannot seek this
manifest. `ffprobe -read_intervals 7200%+…` on the MPD reads from 0 instead of seeking (no answer
within 25 s), and `ffmpeg -ss 7200 -i <mpd>` reports `could not seek to position 7200.000` — while
the *same* bytes, through the *same* proxy, seek fine as a plain track URL. Four manifest variants
(`SegmentBase@timescale` + `presentationTimeOffset`, `Period@duration`, absolute `BaseURL`,
`SegmentList` with explicit `mediaRange` per fragment) were tried on the same bytes and none made
ffmpeg's DASH demuxer seek. So: our ranges and sidx are right, ffmpeg-as-a-DASH-client is not a
seek reference. The receiver is a browser (MSE or a plain `<video>` on the track URL) — task C has
to confirm which, and `video_url_for(sid)` exists for the fallback.

## B6 — memory with two concurrent readers

**Hypothesis.** Streaming in 64 KiB chunks keeps the process far below 150 MB with two readers.

**Measurement** (`evidence/40-rss.json`, one fresh interpreter, server pid 16xxx, two 32 MiB
ranges read concurrently): peak `stats()["rss_mb"]` = **58.3 MB**, peak `ps -o rss=` on the same
process = **58.3 MB** (they agree), both readers got all 33 554 432 bytes. RSS was flat across the
whole transfer (samples in the file).

Separate measurement (not archived, `rss_growth.py`): six successive `resolve()` calls in one
process plateau at **54–64 MB** — the cost of yt-dlp's own parse, not of the proxy. Because an
always-on sender never exits, `refresh()` now calls `malloc_trim(0)` (best effort, glibc) to give
those freed arenas back; measured effect in that script: 54.6 → 53.9 MB.

**Conclusion.** 58 MB with two readers, 2.6× under the bound. The archiver's `ps` check and
`stats()` will agree.

## B7 — a stale token in the middle of a body

**Hypothesis.** When the source starts refusing mid-body, one re-resolution lets the *same* HTTP
response finish with the right bytes.

**Measurement** (`evidence/40-expiry.json`): one client request for 32 MiB, single response.

1. at 1 048 576 bytes read, the session's source URLs are rewritten with an `expire=` in the past;
2. at 12 MiB the source connection is dropped (`proxy.faults['drop_upstream_after']`, **fault
   injection, named as such**: an already-authorised googlevideo download does not die by itself
   when its token expires, so the drop has to be simulated — the 403 that follows is real);
3. the proxy re-opens at that offset, gets **403**, calls `refresh()` — a real `yt-dlp` extraction
   (the same 1.0–1.6 s as B1; the run log said `refresh … took=1306ms`, not archived) — and
   resumes;
4. result: `bytes = 33 554 432` (the declared length), `refreshes = 1`, and the sha256 equals a
   fresh direct fetch of the same 32 MiB: `e174e3e1…4cfbc8980`.

**Conclusion.** Transparent: one response, no truncation, right bytes. The offline suite covers the
same path twice — a source that drops mid-body without expiring (reopen, no refresh) and one that
expires on the drop (refresh) — plus revocation while a reader is mid-body (the reader completes,
a new request gets 410).

## B8 — no token in anything archived

**Measurement** (`evidence/40-token-scan.txt`): the probe scans its own `evidence/40-*` files for
URLs and for the token parameter names (`sig`, `lsig`, `expire`, `ei`, `sparams`, `clen`, …). Before
trusting a clean verdict it self-tests the scanner on a synthetic googlevideo URL — that must be
flagged, otherwise "clean" would only mean the predicate is broken (A33).

Result: 6 files, **0 source URLs**, our own `http://192.168.2.21:7641/media/…` URLs only (7 in
`40-mpd.json`, 2 in `40-seek-7200.json`, 2 in `40-summary.json`), 0 token parameters. The only
occurrence of the string `googlevideo` anywhere is a bare cache-node **hostname**
(`rr5---sn-….googlevideo.com`): 10 of them, 6 in `40-resolve.json` and 4 inside the echoed step
output of `40-summary.json`, every one kept by `annotate()` — a host is not a token, there is no
path and no query. Checked by `grep -o googlevideo evidence/40-*` as well as by the scanner. `resolve()` never logs a URL: `redact()` is the only
form that reaches a file, and `annotate()` archives the host name only.

**Conclusion.** Clean, and the scanner is proven to be able to fail.

---

## Design decisions worth arguing

**One handler, mounted by the sender.** `MediaProxy` is not a WSGI app. It exposes
`handle(handler) -> bool`: the sender owns the single `ThreadingHTTPServer` on
`192.168.2.21:7640` (contracts §5) and writes `if proxy.handle(self): return`. A range body has to
be written and flushed while the source is still being read, and the read loop has to be able to
answer `410` for a revoked sid *before* the first byte, or resume at an offset after a refresh —
neither fits WSGI's "return an iterable of bytes". `start()` binds its own server for the probe
(`:7641`) and for a fallback; the sender never needs it.

**Revocation while reading.** `close_session` removes the sid from the directory immediately (a new
request gets `410`, tested), but a reader that already started keeps its bytes and finishes: its
session is only dropped when its last reader leaves. The alternative — killing the transfer —
produces exactly the truncated `200` the contract forbids.

**Nothing is buffered.** 64 KiB per write, one head probe per track, no cache of media bytes. When
the client hangs up, the proxy stops pulling from the source (tested: < half the body left the
source).

**Re-resolution never logs a URL.** `refresh()` logs `sid`, reason, itags and the new expiry — never
the token it has just received.

## What I could not verify

- **The Chromecast side.** No device was touched tonight. Whether the receiver's player accepts this
  MPD (profile `isoff-on-demand`, `SegmentBase`), and whether `playbackRate` interacts with it, is
  task C's measurement. `video_url_for(sid)` (plain progressive fMP4 with Range) exists as the
  fallback if the receiver's player will not do DASH.
- **`ffmpeg`/`ffprobe` as a DASH *seek* client** — fails on this manifest (B5). Not fixed, reported.
- **Long-run stability** (hours, many shares). Measured: a plateau at ~60 MB over six resolutions;
  not measured: a full evening of shares.
- **The HLS exclusion** is a rule about byte addressability, measured on one video's format list;
  every YouTube video I resolved (3) had a direct DASH `avc1` alternative.
- **Age-gated and private videos**: exercised offline against the recorded fixture and a synthetic
  extractor error; no real private video was attempted (there is none at hand).

## Reproduce

```sh
# deploy (authoring host -> CT 121); never binds 7640
cd ~/opencode/nestaudiocast-wt/media
tar -czf - lib/v020 probe/v020_media_probe.py \
  | ssh root@192.168.2.21 'rm -rf /opt/nac-dev/media/lib/v020 && tar -xzf - -C /opt/nac-dev/media'

# the whole done-when list, one evidence file per step, each in its own process
ssh root@192.168.2.21 'cd /opt/nac-dev/media && /opt/castrelay/.venv/bin/python \
  probe/v020_media_probe.py all --out /opt/nac-dev/media/evidence'

# one step at a time (same thing, easier to watch)
ssh root@192.168.2.21 'cd /opt/nac-dev/media && /opt/castrelay/.venv/bin/python \
  probe/v020_media_probe.py {resolve|mpd|seek|rss|expiry|scan} --out /opt/nac-dev/media/evidence'

# bring the evidence back
scp root@192.168.2.21:/opt/nac-dev/media/evidence/40-* evidence/

# offline suite (no network, no device)
./tests/v020/run.sh            # or: .venv/bin/python -m unittest discover -s tests/v020 -v
./tools/prove.sh --fast
```

`./tests/v020/run.sh <module>` is broken under Python 3.13 (`unittest: error: unrecognized
arguments: -s … -p …`) because it puts `-v` before the `discover` subcommand; the full-suite form
works. I did not touch that script (it is not mine).
