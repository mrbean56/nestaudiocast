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

// ---- 2. the player, with a faithful fake MediaSource and a fake fetch ------------------------
// The fake SourceBuffer behaves like Chrome's on the points the player depends on: any operation
// while `updating` throws InvalidStateError, `updateend` fires asynchronously, `buffered` is the
// union of the appended fragments' time ranges (looked up from the real sidx table by byte
// offset), `remove()` trims it, and `endOfStream()` moves the MediaSource to `ended`.
function makeEventTarget(obj) {
  obj._on = {};
  obj.addEventListener = function (name, fn, opts) {
    const once = !!(opts && opts.once);
    (this._on[name] = this._on[name] || []).push({ fn, once });
  };
  obj.removeEventListener = function (name, fn) {
    this._on[name] = (this._on[name] || []).filter(h => h.fn !== fn);
  };
  obj.fire = function (name) {
    const hs = (this._on[name] || []).slice();
    this._on[name] = (this._on[name] || []).filter(h => !h.once);
    hs.forEach(h => h.fn());
  };
  return obj;
}

function ranges(list) {
  return { length: list.length, start: i => list[i][0], end: i => list[i][1] };
}

function addRange(list, a, b) {
  const all = list.concat([[a, b]]).sort((x, y) => x[0] - y[0]);
  const out = [];
  for (const r of all) {
    const last = out[out.length - 1];
    if (last && r[0] <= last[1] + 1e-6) { last[1] = Math.max(last[1], r[1]); } else { out.push(r.slice()); }
  }
  return out;
}

function cutRange(list, a, b) {
  const out = [];
  for (const [s, e] of list) {
    if (e <= a || s >= b) { out.push([s, e]); continue; }
    if (s < a) { out.push([s, a]); }
    if (e > b) { out.push([b, e]); }
  }
  return out;
}

function fakeWorld(table) {
  const byStart = new Map(table.map(f => [f.start, f]));
  const world = { requests: [], appends: [], invalid: 0, quota: 0, eos: 0, fetchGate: null };
  class FakeBuffer {
    constructor(mime, ms) {
      makeEventTarget(this);
      this.mime = mime; this.ms = ms; this.updating = false; this.list = [];
      this.quotaS = Infinity;            // seconds this buffer may hold, for the quota test
    }
    get buffered() { return ranges(this.list); }
    _busy() { if (this.updating) { world.invalid += 1; const e = new Error('InvalidStateError: updating'); e.name = 'InvalidStateError'; throw e; } }
    _later(fn) { this.updating = true; setImmediate(() => { fn(); this.updating = false; this.fire('updateend'); }); }
    appendBuffer(bytes) {
      this._busy();
      const frag = byStart.get(bytes.__start);
      if (frag) {
        const held = this.list.reduce((a, r) => a + r[1] - r[0], 0);
        if (held + frag.duration > this.quotaS) {
          world.quota += 1; const e = new Error('QuotaExceededError'); e.name = 'QuotaExceededError'; throw e;
        }
      }
      if (this.ms.readyState === 'ended') { this.ms.readyState = 'open'; }
      world.appends.push({ mime: this.mime, start: bytes.__start, bytes: bytes.byteLength });
      this._later(() => { if (frag) { this.list = addRange(this.list, frag.time, frag.time + frag.duration); } });
    }
    remove(a, b) { this._busy(); this._later(() => { this.list = cutRange(this.list, a, b); }); }
    abort() { this.updating = false; }
  }
  class FakeMediaSource {
    constructor() {
      makeEventTarget(this); this.sourceBuffers = []; this.readyState = 'open';
      setImmediate(() => this.fire('sourceopen'));
    }
    addSourceBuffer(mime) {
      if (this.sourceBuffers.length) { throw new Error('limit of SourceBuffer objects'); }
      const b = new FakeBuffer(mime, this); this.sourceBuffers.push(b); world.buffer = b; return b;
    }
    endOfStream() {
      if (this.sourceBuffers.some(b => b.updating)) { throw new Error('InvalidStateError'); }
      this.readyState = 'ended'; world.eos += 1;
    }
  }
  const el = {
    currentTime: 0, playbackRate: 1, paused: true, readyState: 4, tagName: 'VIDEO',
    get buffered() { return world.buffer ? world.buffer.buffered : ranges([]); },
    play() { this.paused = false; return Promise.resolve(); },
    pause() { this.paused = true; },
    removeAttribute() {}, load() {},
    addEventListener() {}
  };
  const fetch = (url, opts) => {
    const range = (opts.headers || {}).Range || '';
    const [, a, b] = /bytes=(\d+)-(\d+)/.exec(range).map(Number);
    world.requests.push({ url, range, a });
    let bytes;
    if (a === 0) { bytes = toArrayBuffer(initBytes); }
    else if (a === 742) { bytes = toArrayBuffer(sidxBytes.slice(0, b - a + 1)); }
    else { bytes = new ArrayBuffer(Math.min(64, b - a + 1)); bytes.__start = a; }
    const reply = { ok: true, status: 206, arrayBuffer: () => Promise.resolve(bytes) };
    if (world.fetchGate && a > 742) { return world.fetchGate.then(() => reply); }
    return Promise.resolve(reply);
  };
  world.el = el; world.FakeMediaSource = FakeMediaSource; world.fetch = fetch;
  return world;
}

const TRACKS = [
  { mime: 'video/mp4; codecs="avc1.640028"', sidxUrl: 'http://x/media/0',
    initUrl: 'http://x/media/0', initStart: 0, initEnd: 741, sidxStart: 742, sidxEnd: 19901 },
];

function install(world) {
  global.MediaSource = world.FakeMediaSource;
  global.URL = { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} };
  global.fetch = world.fetch;
  global.window = { fetch: world.fetch };
  return loadMse();
}

const settle = async () => { for (let i = 0; i < 20; i++) { await new Promise(r => setImmediate(r)); } };

function fragmentsInOrder(world) {
  const starts = world.appends.filter(x => x.start !== undefined).map(x => x.start);
  for (let i = 1; i < starts.length; i++) { if (starts[i] <= starts[i - 1]) { return { at: i, starts: starts.slice(i - 2, i + 2) }; } }
  return null;
}

async function playTest(table) {
  // a source whose bytes are not a sidx must fail loudly, not play nothing
  {
    const world = fakeWorld(table);
    const m = install(world);
    global.fetch = global.window.fetch = () => Promise.resolve({ ok: true, status: 206,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(16)) });
    const player = new m.MsePlayer(world.el, { setInterval: null });
    let failed = false;
    try { await player.start(TRACKS, 0); } catch (e) { failed = true; }
    check('bad_bytes_fail_loudly', failed, player.tracks.length);
  }

  // ten minutes of playback, the page's interval replaced by a hand-driven tick
  {
    const world = fakeWorld(table);
    const m = install(world);
    const player = new m.MsePlayer(world.el, { setInterval: null });
    await player.start(TRACKS, 0);
    check('init_was_the_first_append', world.appends[0] && world.appends[0].bytes === initBytes.byteLength,
          world.appends[0]);
    const first = table[0];
    check('first_fragment_range_is_the_table_entry',
          world.requests.some(r => r.range === `bytes=${first.start}-${first.end}`),
          { asked: `bytes=${first.start}-${first.end}` });
    player.play();
    let starved = null, hoarded = null, maxHeld = 0;
    for (let t = 0; t <= 600; t += 0.5) {
      world.el.currentTime = t;
      await player.tick(); await settle();
      const b = world.el.buffered;
      const end = b.length ? b.end(b.length - 1) : 0;
      const start = b.length ? b.start(0) : 0;
      if (end < t + 5 && starved === null) { starved = { t, end }; }
      if (t > 60 && start < t - 30 && hoarded === null) { hoarded = { t, start }; }
      maxHeld = Math.max(maxHeld, end - start);
    }
    check('ten_minutes_never_starve', starved === null, starved || { last_end: world.el.buffered.end(0) });
    check('played_data_is_evicted', hoarded === null && maxHeld < 45,
          { hoarded, max_held_s: +maxHeld.toFixed(1) });
    check('fragments_appended_in_order_once', fragmentsInOrder(world) === null, fragmentsInOrder(world));
    check('no_operation_while_updating', world.invalid === 0, world.invalid);
    player.stop();
  }

  // a seek while a fetch is in flight: the stale fragment must never be appended
  {
    const world = fakeWorld(table);
    const m = install(world);
    const player = new m.MsePlayer(world.el, { setInterval: null });
    await player.start(TRACKS, 0);
    player.play();
    let open; world.fetchGate = new Promise(r => { open = r; });
    world.el.currentTime = 10;
    const inflight = player.tick();               // fetch for the fragment after the lookahead
    await settle();
    const before = world.appends.length;
    const seekTo = 3600;
    const seeking = player.seek(seekTo);
    open(); world.fetchGate = null;
    await inflight; await seeking; await settle();
    const after = world.appends.slice(before).filter(x => x.start !== undefined);
    const target = table[m.fragmentAt(table, seekTo)];
    check('seek_drops_the_stale_fetch', after.length > 0 && after[0].start === target.start,
          { first_after_seek: after[0] && after[0].start, expected: target.start });
    check('seek_lands_on_the_target', Math.abs(world.el.currentTime - seekTo) < 0.01, world.el.currentTime);
    check('seek_never_collides', world.invalid === 0, world.invalid);
    player.stop();
  }

  // the end of the stream: endOfStream once, after the last fragment
  {
    const world = fakeWorld(table);
    const m = install(world);
    const player = new m.MsePlayer(world.el, { setInterval: null });
    await player.start(TRACKS, 0);
    const lastT = table[table.length - 1].time;
    await player.seek(lastT - 20); await settle();
    for (let t = lastT - 20; t <= lastT + 10; t += 0.5) {
      world.el.currentTime = t; await player.tick(); await settle();
    }
    const lastStart = table[table.length - 1].start;
    check('last_fragment_appended', world.appends.some(x => x.start === lastStart), lastStart);
    check('end_of_stream_signalled_once', world.eos === 1, world.eos);
    player.stop();
  }

  // a full buffer: evict what is behind the playhead and append again, no error
  {
    const world = fakeWorld(table);
    const m = install(world);
    const errors = [];
    const player = new m.MsePlayer(world.el, { setInterval: null, onError: (c, w) => errors.push([c, w]) });
    await player.start(TRACKS, 0);
    world.buffer.quotaS = 26;
    for (let t = 0; t <= 120; t += 0.5) { world.el.currentTime = t; await player.tick(); await settle(); }
    const b = world.el.buffered;
    check('quota_recovers_by_eviction', errors.length === 0 && b.end(b.length - 1) > 125,
          { errors, quota_hits: world.quota, end: b.end(b.length - 1) });
    player.stop();
  }

  // the interval is started by start() and cleared by stop()
  {
    const world = fakeWorld(table);
    const m = install(world);
    const timers = { set: 0, cleared: 0 };
    const player = new m.MsePlayer(world.el, {
      setInterval: () => { timers.set += 1; return 7; },
      clearInterval: (id) => { if (id === 7) { timers.cleared += 1; } } });
    await player.start(TRACKS, 0);
    player.stop();
    check('refill_timer_started_and_cleared', timers.set === 1 && timers.cleared === 1, timers);
  }

  // the page's own interval: the real setInterval is a window method, and Chrome refuses it
  // when called on anything but the window ("Illegal invocation", measured on the device)
  {
    const world = fakeWorld(table);
    const m = install(world);
    const real = { set: global.setInterval, clear: global.clearInterval };
    const seen = { set: 0, cleared: 0, illegal: 0 };
    global.setInterval = function (fn, ms) {
      if (this !== undefined && this !== globalThis) { seen.illegal += 1; throw new TypeError('Illegal invocation'); }
      seen.set += 1; return 99;
    };
    global.clearInterval = function (id) {
      if (this !== undefined && this !== globalThis) { seen.illegal += 1; throw new TypeError('Illegal invocation'); }
      if (id === 99) { seen.cleared += 1; }
    };
    let failed = null;
    try {
      const player = new m.MsePlayer(world.el, {});
      await player.start(TRACKS, 0);
      player.stop();
    } catch (e) { failed = e.message; }
    global.setInterval = real.set; global.clearInterval = real.clear;
    check('window_timers_called_as_window_methods', !failed && seen.illegal === 0 && seen.set === 1 && seen.cleared === 1,
          { failed, ...seen });
  }

  // the audio element follows the video by rate; only a large error re-seats it
  {
    const world = fakeWorld(table);
    const m = install(world);
    const player = new m.MsePlayer(world.el, { setInterval: null });
    const audio = { currentTime: 100.05, playbackRate: 1, paused: false, play() { this.paused = false; }, pause() { this.paused = true; } };
    player.groups = [{ el: world.el }, { el: audio }];
    world.el.paused = false; world.el.currentTime = 100; world.el.playbackRate = 1.002;
    player.sync();
    check('audio_nudged_not_seeked', audio.currentTime === 100.05 && audio.playbackRate < 1.002,
          { t: audio.currentTime, rate: audio.playbackRate });
    audio.currentTime = 100.01; player.sync();
    check('audio_back_on_the_video_rate', audio.playbackRate === 1.002, audio.playbackRate);
    audio.currentTime = 101; player.sync();
    check('audio_reseated_on_a_large_error', audio.currentTime === 100, audio.currentTime);
  }
}

(async () => {
  try {
    await playTest(mse.fragmentTable(sidx));
  } catch (e) {
    check('harness_completed', false, (e && e.message) || String(e));
  }
  const failed = checks.filter(c => !c.ok);
  console.log(JSON.stringify({ page: 'receiver/mse.js', checks: checks.length,
                               passed: checks.length - failed.length, failed: failed.length,
                               results: checks }, null, 1));
  process.exit(failed.length ? 1 : 0);
})();
