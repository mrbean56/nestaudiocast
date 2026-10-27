#!/usr/bin/env node
/*
 * Offline exercise of receiver/receiver.html, with the CAF SDK stubbed out.
 *
 * Why it exists: the page only proves anything on a real Chromecast, and a Cast device refuses an
 * unpublished app until its serial is registered (evidence/41-preflight-unavailable.json, tonight).
 * This harness loads the page's own <script> into a stub environment and drives the protocol, so a
 * broken handler is caught here instead of months later on the hardware. It is not a simulator: it
 * verifies the page speaks contracts §3 correctly, not that the device plays video.
 *
 *   node receiver/selftest.js            # prints one JSON object, exit 0 = all checks passed
 *
 * No dependency, no network, no browser. Wired into tests/v020/test_receiver_page.py when node is
 * available (skipped otherwise).
 */
'use strict';

const fs = require('fs');
const path = require('path');

const PAGE = path.join(__dirname, 'receiver.js');
const NS = 'urn:x-cast:com.beansden.nestaudiocast.v1';

function extractScript(html) {
  // receiver.html is a stable loader; the page itself is receiver.js (a plain IIFE, no markup)
  if (!/<script>/.test(html)) { return html; }
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (!blocks.length) { throw new Error('no script found'); }
  return blocks[blocks.length - 1][1];
}

// ---- the stub world -------------------------------------------------------------------------
function makeWorld() {
  const sent = [];
  const listeners = { custom: [], system: {}, element: {} };
  let clock = 1000000;                       // ms, monotone fake time for performance.now()

  const video = {
    currentTime: 0, playbackRate: 1, paused: true, ended: false, seeking: false,
    readyState: 4, error: null, src: '', dur: 90,
    buffered: { length: 1, end: () => 42.5 },
    play() { this.paused = false; this.seeking = false; return Promise.resolve(); },
    pause() { this.paused = true; },
    load() {},
    removeAttribute(k) { if (k === 'src') { this.src = ''; } },
    getVideoPlaybackQuality() { return { droppedVideoFrames: 7 }; },
    addEventListener(name, fn) { (listeners.element[name] = listeners.element[name] || []).push(fn); },
    removeEventListener() {},
    fire(name) { (listeners.element[name] || []).forEach(fn => fn()); },
  };

  const els = {
    idle: { className: '', textContent: '' },
    idleStatus: { textContent: '' },
    hud: { className: '', textContent: '' },
  };

  const globalDoc = {
    getElementById: (id) => els[id] || { className: '', textContent: '' },
    querySelector: (sel) => (String(sel).indexOf('video') >= 0 ? video : null),
    createElement: () => ({ canPlayType: () => 'probably' }),
  };

  const playerManager = {
    loaded: null,
    load(req) { this.loaded = req; video.playbackRate = 1; video.paused = true; },
    stop() { video.paused = true; },
    setPlaybackRate(r) { this.rate = r; },
    rate: 1,
    addEventListener() {},
  };

  const context = {
    started: null,
    start(opts) { this.started = opts || {}; },
    getPlayerManager() { return playerManager; },
    senders: ['sender-test'],
    getSenders() { return this.senders; },
    sendCustomMessage(ns, senderId, msg) { sent.push({ ns, senderId, msg }); },
    addCustomMessageListener(ns, fn) { listeners.custom.push({ ns, fn }); },
    addEventListener(type, fn) { (listeners.system[type] = listeners.system[type] || []).push(fn); },
  };

  const cast = {
    framework: {
      CastReceiverContext: { getInstance: () => context },
      system: { EventType: { SENDER_CONNECTED: 'sender_connected', SENDER_DISCONNECTED: 'sender_disconnected' } },
      messages: {
        LoadRequestData: function (o) { Object.assign(this, o); },
        MediaInformation: function (o) { Object.assign(this, o); },
      },
      events: { EventType: {} },
    },
  };

  const performance_ = { timeOrigin: 1.7e12, now: () => clock };
  const navigator_ = { userAgent: 'stub/0.0 (offline harness)' };

  return {
    video, els, sent, listeners, context, playerManager, cast, performance: performance_,
    navigator: navigator_,
    advance(ms) { clock += ms; },
    emit(ns, payload, senderId) {
      const l = listeners.custom.filter(x => x.ns === ns).pop();
      if (!l) { throw new Error('page never subscribed to ' + ns); }
      l.fn({ data: payload, senderId: senderId || 'sender-test' });
    },
    of(type) { return sent.filter(m => m.msg && m.msg.type === type); },
  };
}

function load(world) {
  const code = extractScript(fs.readFileSync(PAGE, 'utf8'));
  const fn = new Function(
    'cast', 'document', 'performance', 'navigator', 'window', 'setInterval', 'setTimeout',
    'clearInterval', code);
  const win = {};
  // setTimeout runs for real (the page schedules a single play() from the target time);
  // setInterval is captured instead, so a test can fire the 4 Hz ticker by hand.
  const ticks = [];
  fn(world.cast, globalDocOf(world), world.performance, world.navigator, win,
     (cb) => { ticks.push(cb); return ticks.length; }, setTimeout, () => 0, );
  world.window = win;
  world.ticks = ticks;
  return win;
}

function globalDocOf(world) {
  return {
    getElementById: (id) => world.els[id] || { className: '', textContent: '' },
    querySelector: (sel) => {
      if (world.noVideoInDom) { return null; }
      return String(sel).indexOf('video') >= 0 ? world.video : null;
    },
    // in the device, a <video> element exists in the DOM even when our own is not driving it
    querySelectorAll: (sel) => (String(sel).indexOf('video') >= 0 ? [world.video] : []),
    body: { appendChild() {} },
    createElement: (tag) => (String(tag) === 'video' ? world.video : { canPlayType: () => 'probably' }),
  };
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail === undefined ? null : detail });
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function main() {
  const w = makeWorld();
  load(w);

  // 1. the page started CAF, with the idle timeout disabled and a status text
  check('caf_started', !!w.context.started && w.context.started.disableIdleTimeout === true,
        w.context.started);
  check('subscribed_to_our_namespace', w.listeners.custom.some(x => x.ns === NS));
  check('idle_page_says_ready', /ready/.test(w.els.idleStatus.textContent), w.els.idleStatus.textContent);

  // 2. a sender connects -> hello, with the contract's fields
  (w.listeners.system.sender_connected || []).forEach(fn => fn({ senderId: 'sender-test' }));
  await sleep(5);
  let hello = w.of('hello');
  check('hello_on_connect', hello.length === 1, hello.length);
  if (hello.length) {
    const h = hello[0].msg;
    check('hello_fields', ['rx_ms', 'ua', 'codecs', 'version', 'seq'].every(k => k in h),
          Object.keys(h));
    check('hello_seq_present', typeof h.seq === 'number', h.seq);
    check('hello_codecs_has_the_matrix', ['h264', 'hevc', 'vp9', 'av1'].every(k => k in h.codecs),
          h.codecs && Object.keys(h.codecs));
    check('hello_rx_ms_from_timeorigin', h.rx_ms >= 1.7e12, h.rx_ms);
  }

  // 3. clock -> clock_reply, echoing the sender's seq, addressed to that sender
  w.emit(NS, { type: 'clock', seq: 41 }, 'sender-test');
  let pong = w.of('clock_reply');
  check('clock_reply', pong.length === 1, pong.length);
  if (pong.length) {
    check('clock_reply_echoes_seq', pong[0].msg.seq === 41, pong[0].msg.seq);
    check('clock_reply_to_the_sender', pong[0].senderId === 'sender-test', pong[0].senderId);
  }

  // 4. load (progressive mp4): metadata, one src, no autoplay
  w.emit(NS, { type: 'load', seq: 42, url: 'http://192.168.2.21:7640/media/a.mp4',
               content_type: 'video/mp4', media: 1.5, autoplay: false }, 'sender-test');
  check('load_sets_src', w.video.src === 'http://192.168.2.21:7640/media/a.mp4', w.video.src);
  check('load_parks_at_media', w.video.currentTime === 1.5, w.video.currentTime);
  check('load_does_not_autoplay', w.video.paused === true, w.video.paused);
  await sleep(80);                     // the page emits the first state 50 ms after a load
  const st = w.of('state');
  check('load_emits_state', st.length >= 1, st.length);
  if (st.length) {
    const s = st[st.length - 1].msg;
    check('state_fields',
          ['rx_ms', 'media', 'rate', 'player_state', 'buffered_end', 'dropped'].every(k => k in s),
          Object.keys(s));
    check('state_seq_present', typeof s.seq === 'number', s.seq);
  }

  // 5. pause -> the element pauses, a state follows
  w.emit(NS, { type: 'pause', seq: 43 }, 'sender-test');
  check('pause_pauses', w.video.paused === true, w.video.paused);
  check('pause_no_false_intent', w.of('intent').length === 0, w.of('intent').length);

  // 6. seek preserves the playing state (it stays paused here, but moves)
  w.emit(NS, { type: 'seek', seq: 44, media: 12.25 }, 'sender-test');
  check('seek_moves', w.video.currentTime === 12.25, w.video.currentTime);
  check('seek_no_false_intent', w.of('intent').length === 0, w.of('intent').map(m => m.msg));

  // 7. rate -> playbackRate on the element and echoed in state
  w.emit(NS, { type: 'rate', seq: 45, value: 1.002 }, 'sender-test');
  check('rate_applied', w.video.playbackRate === 1.002, w.video.playbackRate);
  check('rate_echoed_in_state', w.of('state').pop().msg.rate === 1.002, w.of('state').pop().msg.rate);

  // 8. play_at: one play(), at the target time, not a burst of seeks
  const target = w.performance.now() + 120;
  const before = w.sent.length;
  w.emit(NS, { type: 'play_at', seq: 46, rx_ms: target, media: 3.0 }, 'sender-test');
  check('play_at_parks_immediately', w.video.currentTime === 3.0, w.video.currentTime);
  await sleep(200);
  const afterPlay = w.sent.slice(before).filter(m => m.msg.type === 'state');
  check('play_at_single_play', w.video.paused === false, w.video.paused);
  check('play_at_emits_state', afterPlay.length >= 1, afterPlay.length);

  // 9. a foreign pause (the Google Home app) is reported as an intent, not acted on silently.
  //    The harness clock is frozen, so move it past the park's own suppression window first:
  //    on the device, performance.now() moves and that window is 120 ms.
  w.advance(400);
  w.video.fire('pause');
  const intents = w.of('intent');
  check('foreign_pause_intent', intents.length === 1 && intents[0].msg.action === 'pause', intents.map(i => i.msg));

  // 10. stop -> idle page, no source, and a final state
  w.emit(NS, { type: 'stop', seq: 47 }, 'sender-test');
  check('stop_clears_source', w.video.src === '', w.video.src);
  check('stop_shows_idle', /ready/.test(w.els.idleStatus.textContent), w.els.idleStatus.textContent);
  const last = w.of('state').pop().msg;
  check('stop_reports_idle_state', last.player_state === 'idle', last.player_state);

  // 11. idle -> stop + a fresh hello (the controller needs to know we are ready again)
  const hellosBefore = w.of('hello').length;
  w.emit(NS, { type: 'idle', seq: 48 }, 'sender-test');
  check('idle_announces_ready', w.of('hello').length === hellosBefore + 1, w.of('hello').length);

  // 12. an unknown type is an error, not a crash
  const errsBefore = w.of('error').length;
  w.emit(NS, { type: 'no_such_thing', seq: 49 }, 'sender-test');
  check('unknown_type_reported', w.of('error').length === errsBefore + 1, w.of('error').length);

  // 13. a JSON string payload is accepted (some senders send text)
  w.emit(NS, JSON.stringify({ type: 'clock', seq: 50 }), 'sender-test');
  check('json_string_payload', w.of('clock_reply').some(m => m.msg.seq === 50), null);

  // 13b. a pause before the scheduled release must win (measured 2026-10-27: the release fired
  //      after the pause and playback resumed, so the service reported 'playing' while paused)
  w.advance(5000);
  w.emit(NS, { type: 'play_at', seq: 48.5, rx_ms: (1.7e12 + 3000), media: 12.0 }, 'sender-test');
  w.emit(NS, { type: 'pause', seq: 48.6 }, 'sender-test');
  const beforeCancelled = w.video.paused;
  await new Promise((r) => setTimeout(r, 120));
  check('pause_cancels_a_pending_release', beforeCancelled === true && w.video.paused === true,
        { at_pause: beforeCancelled, after: w.video.paused });

  // 14. a raw CASTv2 controller is not a CAF sender: getSenders() is empty for it, so state,
  //     error and ended must still reach the sender we last heard from (measured 2026-10-26:
  //     clock_reply, a direct reply, arrived; state, a broadcast, never did)
  w.context.senders = [];
  w.emit(NS, { type: 'load', seq: 51, url: 'http://192.168.2.21:7640/media/b.mp4',
               content_type: 'video/mp4', media: 0, autoplay: true }, 'raw-controller');
  w.advance(200);
  w.ticks.forEach((cb) => cb());          // one 4 Hz tick, with no CAF sender registered
  const toRaw = w.sent.filter(m => m.senderId === 'raw-controller').map(m => m.msg.type);
  check('state_reaches_a_non_caf_sender', toRaw.includes('state'), toRaw.slice(0, 8));
  const errsBefore2 = w.of('error').length;
  w.emit(NS, { type: 'bogus_after_senders_empty', seq: 52 }, 'raw-controller');
  check('error_reaches_a_non_caf_sender',
        w.sent.some(m => m.senderId === 'raw-controller' && m.msg.type === 'error'),
        w.of('error').length - errsBefore2);

  // 15. no <video> in the DOM at all (a custom receiver page, measured on the device): the page
  //     must create and use its own element instead of answering no_media_element
  w.noVideoInDom = true;
  const errsBefore3 = w.of('error').length;
  w.emit(NS, { type: 'load', seq: 53, url: 'http://192.168.2.21:7640/media/c.mp4',
               content_type: 'video/mp4', media: 0, autoplay: true }, 'raw-controller');
  w.ticks.forEach((cb) => cb());
  check('own_element_when_dom_has_none', w.of('error').length === errsBefore3,
        { errors: w.of('error').length - errsBefore3, src: w.video.src });

  // 16. the element that is driven is the element whose events are bound: a LOAD at 20 s must
  //     park at 20 s, not at 0 (measured on the device as a 20 000 ms "start error")
  w.noVideoInDom = false;
  w.video.readyState = 1;
  w.emit(NS, { type: 'load', seq: 54, url: 'http://192.168.2.21:7640/media/d.mp4',
               content_type: 'video/mp4', media: 20, autoplay: false }, 'raw-controller');
  check('load_parks_where_asked', Math.abs((w.video.currentTime || 0) - 20) < 0.01,
        w.video.currentTime);

  const failed = checks.filter(c => !c.ok);
  const out = { page: 'receiver/receiver.html', checks: checks.length,
                passed: checks.length - failed.length, failed: failed.length, results: checks };
  console.log(JSON.stringify(out, null, 1));
  process.exit(failed.length ? 1 : 0);
}

main().catch(err => { console.error(err); process.exit(2); });
