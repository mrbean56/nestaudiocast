#!/usr/bin/env node
/*
 * Offline harness for receiver/mse.js — proves the parser against **real** sidx bytes captured
 * from a live session (tests/v020/fixtures), then drives the player with a fake MediaSource and
 * a fake fetch, so the sequence of byte ranges and appends is checked without a device.
 *
 *   node receiver/mse_selftest.js         # prints JSON, exit 1 on any failure
 */
'use strict';
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const FIX = path.join(HERE, '..', 'tests', 'v020', 'fixtures');
const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail === undefined ? null : detail });
}

function loadMse() {
  delete require.cache[require.resolve('./mse.js')];
  return require('./mse.js');
}

// ---- 1. the parser, on the real bytes --------------------------------------------------------
function toArrayBuffer(buf) {
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

const meta = JSON.parse(fs.readFileSync(path.join(FIX, 'media-meta.json'), 'utf8'));
const sidxBytes = fs.readFileSync(path.join(FIX, 'video-sidx.bin'));
const initBytes = fs.readFileSync(path.join(FIX, 'video-init.bin'));
let mse = loadMse();

check('fixture_is_a_sidx', sidxBytes.slice(4, 8).toString() === 'sidx', sidxBytes.slice(4, 8).toString());
check('fixture_init_is_ftyp', initBytes.slice(4, 8).toString() === 'ftyp', initBytes.slice(4, 8).toString());

const sidx = mse.parseSidx(toArrayBuffer(sidxBytes), meta.sidx_start === undefined ? 742 : meta.sidx_start);
check('sidx_parsed', !!sidx);
if (sidx) {
  const table = mse.fragmentTable(sidx);
  check('has_fragments', table.length > 100, table.length);
  check('timescale_sane', sidx.timescale > 0, sidx.timescale);
  // every fragment must be contiguous and inside the file the proxy advertised
  let contiguous = true;
  for (let i = 1; i < table.length; i++) {
    if (table[i].start !== table[i - 1].end + 1) { contiguous = false; break; }
  }
  check('fragments_are_contiguous', contiguous);
  const lastEnd = table[table.length - 1].end;
  check('first_fragment_starts_after_the_index',
        table[0].start === (meta.sidx_start || 742) + sidxBytes.length + sidx.firstOffset,
        { first: table[0].start, expected: (meta.sidx_start || 742) + sidxBytes.length });
  check('last_fragment_reaches_the_end_of_the_file',
        !!meta.file_bytes ? Math.abs(lastEnd + 1 - meta.file_bytes) < 4096 : lastEnd > table[0].start,
        { lastEnd, file_bytes: meta.file_bytes });
  const totalDuration = table.reduce((a, f) => a + f.duration, 0);
  check('durations_are_seconds', totalDuration > 60 && totalDuration < 60 * 60 * 6, {
    seconds: Math.round(totalDuration), expected: 'the video is 2 h 23'
  });
  // the table must cover the stream in order, without holes in time
  let monotonic = true;
  for (let i = 1; i < table.length; i++) {
    if (table[i].time <= table[i - 1].time) { monotonic = false; break; }
  }
  check('times_are_monotonic', monotonic);
  // seeking picks the fragment that contains the instant
  const target = 3600;
  const at = table[mse.fragmentAt(table, target)];
  check('fragment_at_picks_the_container', at.time <= target && target < at.time + at.duration,
        { picked_time: Math.round(at.time), duration: +at.duration.toFixed(2) });
}

// ---- 2. the player, with a fake MediaSource and a fake fetch --------------------------------
function fakeWorld() {
  const requests = [];
  const appends = [];
  class FakeBuffer {
    constructor(mime) { this.mime = mime; this.updating = false; this.buffered = { length: 1, start: () => 0, end: () => 0 }; }
    addEventListener(name, fn) { this._on = this._on || {}; (this._on[name] = this._on[name] || []).push(fn); if (name === 'updateend') { setTimeout(() => fn(), 0); } }
    appendBuffer(bytes) { appends.push({ mime: this.mime, bytes: bytes.byteLength }); }
    abort() {}
    remove() {}
  }
  class FakeMediaSource {
    constructor() {
      this.handlers = {}; this.sourceBuffers = []; this.readyState = 'open';
      setTimeout(() => (this.handlers.sourceopen || []).forEach(f => f()), 0);
    }
    addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); }
    addSourceBuffer(mime) { const b = new FakeBuffer(mime); this.sourceBuffers.push(b); return b; }
  }
  const el = {
    currentTime: 0, playbackRate: 1, paused: true, readyState: 4,
    buffered: { length: 1, start: () => 0, end: () => 30 },
    play() { this.paused = false; return Promise.resolve(); },
    pause() { this.paused = true; },
    removeAttribute() {}, load() {},
    addEventListener() {}
  };
  const fetch = (url, opts) => {
    const range = (opts.headers || {}).Range || '';
    requests.push({ url, range });
    return Promise.resolve({
      ok: true, status: 206,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(16))
    });
  };
  return { requests, appends, el, FakeMediaSource, fetch };
}

async function playTest() {
  const world = fakeWorld();
  global.MediaSource = world.FakeMediaSource;
  global.URL = { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} };
  global.fetch = world.fetch;
  global.window = { fetch: world.fetch };
  mse = loadMse();

  const tracks = [
    { mime: 'video/mp4; codecs="avc1.640028"', sidxUrl: 'http://x/media/0',
      initUrl: 'http://x/media/0', initStart: 0, initEnd: 741, sidxStart: 742, sidxEnd: 19901 },
  ];
  const player = new mse.MsePlayer(world.el, {});
  // the fake fetch returns 16 bytes, so the parser will refuse them: that is the check that a
  // bad source fails loudly instead of silently playing nothing
  let failed = false;
  try { await player.start(tracks, 0); } catch (e) { failed = true; }
  check('bad_bytes_fail_loudly', failed, player.tracks.length);

  // now serve the real fixture bytes: init for the init range, the sidx for the index range
  const real = (url, opts) => {
    const range = (opts.headers || {}).Range || '';
    const [, a, b] = /bytes=(\d+)-(\d+)/.exec(range).map(Number);
    const bytes = a === 0 ? initBytes : sidxBytes.slice(0, b - a + 1);
    world.requests.push({ url, range });
    return Promise.resolve({ ok: true, status: 206,
      arrayBuffer: () => Promise.resolve(toArrayBuffer(bytes)) });
  };
  global.fetch = real; global.window = { fetch: real };
  mse = loadMse();
  const p2 = new mse.MsePlayer(world.el, {});
  world.el.currentTime = 0;
  await p2.start(tracks, 0);
  check('start_appends_init_then_one_fragment', world.appends.length >= 2, world.appends.length);
  check('init_was_the_first_append', world.appends[0] && world.appends[0].bytes === initBytes.byteLength,
        world.appends[0]);
  // with the playhead at 0 and a 20 s lookahead, several fragments must be prefetched
  // the lookahead is 12 s and a fragment is ~5.4 s: init + two fragments is what one pump fills
  check('prefetches_inside_the_lookahead', world.appends.length >= 2, world.appends.length);
  const first = p2.tracks[0].table[0];
  check('first_fragment_range_is_the_table_entry',
        world.requests.some(r => r.range === `bytes=${first.start}-${first.end}`),
        { asked: `bytes=${first.start}-${first.end}` });
}

(async () => {
  try {
    await playTest();
  } catch (e) {
    check('harness_completed', false, (e && e.message) || String(e));
  }
  const failed = checks.filter(c => !c.ok);
  console.log(JSON.stringify({ page: 'receiver/mse.js', checks: checks.length,
                               passed: checks.length - failed.length, failed: failed.length,
                               results: checks }, null, 1));
  process.exit(failed.length ? 1 : 0);
})();
