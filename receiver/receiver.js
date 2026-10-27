(function () {
  'use strict';

  var NS = 'urn:x-cast:com.beansden.nestaudiocast.v1';
  var VERSION = '0.2.0';
  //: the build stamp from our own script URL (/r/<stamp>/receiver.js): without it there is no
  //: way to tell which build a cached device is running, and we paid for that twice tonight
  var BUILD = (function () {
    try {
      var m = /\/r\/([^\/]+)\//.exec(document.currentScript && document.currentScript.src);
      if (m) { return m[1]; }
    } catch (e) {}
    return 'dev';
  })();
  var APP_NAME = 'VideoNetCaster';
  var STATE_PERIOD_MS = 250;          // contracts §3.2: state at 4 Hz (+ on every transition)

  var context = cast.framework.CastReceiverContext.getInstance();
  var playerManager = context.getPlayerManager();
  var idleEl = document.getElementById('idle');
  var idleStatus = document.getElementById('idleStatus');
  var hudEl = document.getElementById('hud');

  var seq = 0;                 // seq of the last controller message we saw; echoed on ours
  var helo = {};               // senderId -> true once we greeted it on this connection
  var mediaLoaded = false;
  var pendingPos = null;       // a load/seek waiting for metadata
  var METADATA_GIVE_UP_MS = 5000;
  var loadedContentType = '';
  var endedSent = false;
  var ticker = null;
  var msePlayer = null;      // the MSE reader (receiver/mse.js), used for DASH sources
  var mseLoading = false;
  var expect = null;           // {kind, until}: an element event we caused ourselves
  var lastJumpAt = 0;
  var lastError = null;

  // ---- clock and sampling ------------------------------------------------------------------
  // rx_ms and the media position are read in ONE statement, as contracts §3 demands: a sample
  // pair that straddles a frame boundary would look like a rate error.
  function sample() {
    var rx_ms = performance.timeOrigin + performance.now(), media = mediaEl() ? mediaEl().currentTime : 0;
    return { rx_ms: rx_ms, media: media };
  }

  // The Styled Receiver ships a <cast-media-player><video>; a plain custom receiver page has
  // none, and `getMediaElement()`-style helpers are not enough here: measured on the device
  // (2026-10-26) the page answered `no_media_element` to the first LOAD. So the page owns its
  // element: created once, reused, and preferred over nothing. If CAF later inserts its own
  // (on a DASH load), that one wins, so the state we report is always the element we drive.
  var ownEl = null;
  var ownAudioEl = null;

  function ownElement() {
    if (ownEl) { return ownEl; }
    var v = document.createElement('video');
    try {
      v.id = 'nac-media';
      v.setAttribute('playsinline', '');
      v.style.width = '100%';
      v.style.height = '100%';
      document.body.appendChild(v);
    } catch (e) { /* a body that refuses it still leaves us the element */ }
    ownEl = v;
    return v;
  }

  // Exactly one element is driven, and it is the one whose events are bound. Measured
  // (2026-10-26): the page bound its own element at boot but mediaEl() then returned CAF's
  // empty one, so `v.currentTime = start` landed nowhere and a LOAD at 20 s played from 0.
  var dashMode = false;

  function audioElement() {
    if (ownAudioEl) { return ownAudioEl; }
    var a = document.createElement('audio');
    try {
      a.id = 'nac-audio';
      document.body.appendChild(a);
    } catch (e) {}
    ownAudioEl = a;
    return a;
  }

  function mediaEl() {
    // The page drives exactly one element: its own. CAF keeps a <video> of its own in the DOM
    // (measured 2026-10-26: 'cast-media-player' exists), and handing our MediaSource to *that*
    // element made CAF report error 905 and refuse to load — so it is never picked implicitly.
    return ownElement();
  }

  function apiSurface() {
    // what CAF actually offers here (measured 2026-10-26: a custom page has no
    // <cast-media-player>, so the DASH path needs to know how to reach CAF's element)
    var pm = playerManager;
    return {
      getMediaElement: typeof pm.getMediaElement,
      setMediaElement: typeof pm.setMediaElement,
      getPlaybackConfig: typeof pm.getPlaybackConfig,
      getPlayerData: typeof pm.getPlayerData,
      mse: (typeof window.MediaSource !== 'undefined') ? 'yes' : 'no',
      cafMediaPlayer: !!document.querySelector('cast-media-player')
    };
  }

  function elementInfo() {
    var caf = document.querySelector('cast-media-player video') || document.querySelector('video');
    return { caf: !!caf, own: ownEl ? 'created' : (caf ? 'not-needed' : 'lazy') };
  }

  // ---- protocol ----------------------------------------------------------------------------
  function envelope(type, extra) {
    var msg = { type: type, seq: seq };
    if (extra) { for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) { msg[k] = extra[k]; } } }
    return msg;
  }

  function sendTo(senderId, msg) {
    try { context.sendCustomMessage(NS, senderId, msg); }
    catch (e) { /* a sender that left is not an error worth reporting */ }
  }

  // CAF's getSenders() lists *CAF* senders; a plain CASTv2 controller is not one of them, so a
  // broadcast alone reaches nobody (measured 2026-10-26: clock, a direct reply, arrived; state,
  // error and ended, all broadcasts, did not). Every message now also goes to the sender we last
  // heard from, which is the id the transport hands us and which sendTo() demonstrably reaches.
  var lastSender = null;

  function targets() {
    var ids = [];
    try { ids = (context.getSenders() || []).slice(); } catch (e) { ids = []; }
    if (lastSender && ids.indexOf(lastSender) < 0) { ids.push(lastSender); }
    return ids;
  }

  function broadcast(type, extra) {
    var msg = envelope(type, extra);
    var ids = targets();
    for (var i = 0; i < ids.length; i++) { sendTo(ids[i], msg); }
    return msg;
  }

  function hello(senderId) {
    var s = sample();
    var body = {
      rx_ms: s.rx_ms, ua: navigator.userAgent, codecs: codecClaims(),
      version: VERSION, build: BUILD, app: APP_NAME, app_id: '28028CD2', element: elementInfo(),
      apis: apiSurface()
    };
    if (senderId) { sendTo(senderId, envelope('hello', body)); }
    else { broadcast('hello', body); }
  }

  function sendError(code, message) {
    lastError = { code: code, message: String(message), at: rxNow() };
    broadcast('error', { code: code, message: String(message) });
    log('error ' + code + ': ' + message);
  }

  function rxNow() { return performance.timeOrigin + performance.now(); }

  function codecClaims() {
    var p = document.createElement('video');
    function t(s) { try { return p.canPlayType(s) || 'no'; } catch (e) { return 'error'; } }
    return {
      h264: t('video/mp4; codecs="avc1.42E01E"'),
      hevc: t('video/mp4; codecs="hvc1.1.6.L93.B0"'),
      vp9: t('video/mp4; codecs="vp09.00.10.08"'),
      av1: t('video/mp4; codecs="av01.0.05M.08"'),
      mp4: t('video/mp4'),
      // a manifest type is not what MSE accepts; ask whether MSE exists at all
      dash: (typeof window.MediaSource !== 'undefined' || typeof MediaSource !== 'undefined')
        ? 'maybe' : 'no',
      ua: navigator.userAgent
    };
  }

  // ---- player state ------------------------------------------------------------------------
  function playerState() {
    if (!mediaLoaded) { return 'idle'; }
    var v = mediaEl();
    if (!v) { return 'loading'; }
    if (v.error) { return 'error'; }
    if (v.ended) { return 'ended'; }
    if (v.paused) { return v.readyState >= 2 ? 'paused' : 'loading'; }
    if (v.seeking || v.readyState < 3) { return 'buffering'; }
    return 'playing';
  }

  function bufferedEnd() {
    var v = mediaEl();
    if (!v) { return null; }
    try {
      var b = v.buffered;
      return b && b.length ? b.end(b.length - 1) : null;
    } catch (e) { return null; }
  }

  function droppedFrames() {
    var v = mediaEl();
    if (!v || !v.getVideoPlaybackQuality) { return null; }
    try { return v.getVideoPlaybackQuality().droppedVideoFrames; } catch (e) { return null; }
  }

  function currentRate() {
    var v = mediaEl();
    return v ? v.playbackRate : 1;
  }

  function statePayload() {
    var s = sample();
    var v = mediaEl();
    return {
      rx_ms: s.rx_ms,
      media: s.media,
      rate: currentRate(),
      player_state: playerState(),
      buffered_end: bufferedEnd(),
      dropped: droppedFrames(),
      // diagnosis, not protocol: what the element thinks, and what we still owe it (2026-10-26:
      // a LOAD at 20 s kept playing from 0 and the state alone did not say why)
      el: v ? ((v.id || 'noid') + '/' + typeof v.readyState) : 'none',
      audio: msePlayer ? (msePlayer.audioElement() ? msePlayer.audioElement().currentTime : null) : null,
      ready: v ? v.readyState : null,
      pending: pendingPos,
      logTail: logs.slice(-4).map(function (l) { return l.line; })
    };
  }

  function sendState(force) {
    if (!mediaLoaded && !force) { return; }
    var p = statePayload();
    broadcast('state', p);          // 4 Hz regardless of whether anything changed (§3.2)
    paintHud(p);
  }

  function paintHud(p) {
    if (!p || p.player_state === 'idle') { hudEl.className = 'hidden'; hudEl.textContent = ''; return; }
    hudEl.className = '';
    hudEl.textContent =
      APP_NAME + ' ' + VERSION + '\n' +
      p.player_state + '  rate ' + p.rate.toFixed(3) + '\n' +
      'media ' + p.media.toFixed(3) + ' s' + (p.buffered_end == null ? '' : '  buf ' + p.buffered_end.toFixed(2));
  }

  // ---- idle page ---------------------------------------------------------------------------
  function showIdle(text) {
    idleStatus.textContent = text || 'ready';
    idleEl.className = '';
    hudEl.className = 'hidden';
  }

  function hideIdle() { idleEl.className = 'hidden'; }

  // ---- our own element events are not intents ----------------------------------------------
  function markExpect(kind) { expect = { kind: kind, until: rxNow() + 2000 }; }

  function consumeExpect(kind) {
    if (expect && expect.kind === kind && rxNow() <= expect.until) { expect = null; return true; }
    return false;
  }

  // ---- transport commands ------------------------------------------------------------------
  function doLoad(m) {
    var url = m.url, ct = m.content_type || 'video/mp4';
    var start = typeof m.media === 'number' ? m.media : 0;
    if (!url) { sendError('bad_load', 'missing url'); return; }
    endedSent = false;
    loadedContentType = ct;
    mediaLoaded = true;
    pendingPos = start;
    hideIdle();
    var isDash = /dash|\/\*\*mpd/.test(ct) || /\.mpd(\?|$)/.test(url);
    markExpect('load');
    try {
      if (isDash) {
        // Measured 2026-10-26 (lab note C6): neither CAF's DASH player nor the platform DMR will
        // play the manifest we generate, while its byte ranges are provably correct. So the page
        // reads the sidx and feeds SourceBuffers itself (receiver/mse.js), on the same element it
        // already owns — which keeps playbackRate and the playhead under our control.
        dashMode = false;              // our own element stays the one we drive
        startMse(url, start, !!m.autoplay, m).catch(function (e) {
          sendError('mse_failed', (e && e.message) || String(e));
        });
      } else {
        var v = mediaEl();
        if (!v) { sendError('no_media_element', 'no <video> element could be obtained'); return; }
        v.src = url;
        v.load();
        v.playbackRate = 1;
        v.currentTime = start;
        if (m.autoplay) { markExpect('play'); var pp = v.play(); if (pp && pp.catch) { pp.catch(function () {}); } }
      }
    } catch (e) {
      sendError('load_failed', (e && e.message) || e);
    }
    log('load ' + ct + ' at ' + start);
    setTimeout(function () { sendState(true); }, 50);
  }

  function loadScriptOnce(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('could not load ' + src)); };
      document.head.appendChild(s);
    });
  }

  function mpdTracks(mpdText, mpdUrl) {
    var doc = new DOMParser().parseFromString(mpdText, 'application/xml');
    var sets = doc.getElementsByTagName('AdaptationSet');
    var out = [];
    for (var i = 0; i < sets.length; i++) {
      var reps = sets[i].getElementsByTagName('Representation');
      if (!reps.length) { continue; }
      var rep = reps[0];
      var base = rep.getElementsByTagName('BaseURL')[0];
      var seg = rep.getElementsByTagName('SegmentBase')[0];
      var init = seg && seg.getElementsByTagName('Initialization')[0];
      if (!base || !seg || !init) { continue; }
      var idx = seg.getAttribute('indexRange').split('-');
      var ini = init.getAttribute('range').split('-');
      var mime = sets[i].getAttribute('mimeType') || (sets[i].getAttribute('contentType') === 'audio' ? 'audio/mp4' : 'video/mp4');
      var codecs = rep.getAttribute('codecs');
      var url = new URL(base.textContent.trim(), mpdUrl).href;
      out.push({
        mime: mime + (codecs ? '; codecs="' + codecs + '"' : ''),
        initUrl: url, sidxUrl: url,
        initStart: Number(ini[0]), initEnd: Number(ini[1]),
        sidxStart: Number(idx[0]), sidxEnd: Number(idx[1])
      });
    }
    return out;
  }

  function startMse(mpdUrl, atTime, autoplay, m) {
    if (mseLoading) { return Promise.resolve(); }
    mseLoading = true;
    // The reader lives behind the same cache-proof scheme as the page: the device caches by
    // path, so ask for a stamp fetched *now*, not the path this page was loaded from (measured:
    // mse.js was served from cache for a whole hour because only it had changed).
    var ready = (typeof NacMse !== 'undefined')
      ? Promise.resolve()
      : fetch('/b/' + Date.now() + '/build.txt', { cache: 'no-store' })
          .then(function (r) { return r.text(); })
          .then(function (t) { return loadScriptOnce('/r/' + (t.trim() || BUILD) + '/mse.js'); });
    return ready.then(function () {
      return fetch(mpdUrl, { cache: 'no-store' });
    }).then(function (r) {
      if (!r.ok) { throw new Error('manifest ' + r.status); }
      return r.text();
    }).then(function (text) {
      var tracks = mpdTracks(text, mpdUrl);
      if (!tracks.length) { throw new Error('the manifest has no usable track'); }
      if (msePlayer) { try { msePlayer.stop(); } catch (e) {} }
      msePlayer = new NacMse.MsePlayer(mediaEl(), {
        log: function (line) { log('mse ' + line); },
        onUpdate: function () { sendState(true); },
        onError: function (code, why) { sendError('mse_' + code, why); }
      });
      return msePlayer.start(tracks, atTime, [audioElement()]);
    }).then(function () {
      if (autoplay) { msePlayer.play(); }
      mseLoading = false;
      sendState(true);
    }).catch(function (err) {
      mseLoading = false;
      sendError('mse_start_failed', (err && err.message) || String(err));
    });
  }

  function doPlayAt(m) {
    var target = Number(m.rx_ms), pos = Number(m.media), rate = Number(m.rate);
    if (!isFinite(target) || !isFinite(pos)) { sendError('bad_play_at', 'rx_ms and media are required'); return; }
    var v = mediaEl();
    if (!v) { sendError('no_media_element', 'nothing loaded'); return; }
    if (isFinite(rate) && rate > 0) { setElementRate(v, rate); }
    // Park at the target position, paused, then release with exactly one play() at the target
    // receiver time. No burst of seeks: the controller may re-land once, not ten times.
    // mark before acting: a pause event is asynchronous in a browser but must never be mistaken
    // for somebody else's press, whichever order it arrives in.
    markExpect('pause');
    markExpect('seek');
    try { v.pause(); } catch (e) {}
    pendingPos = null;
    var park = function () {
      try { v.currentTime = Math.max(0, pos); } catch (e) { sendError('seek_failed', (e && e.message) || e); }
      waitForRelease(v, pos, target);
    };
    if (v.readyState >= 1) { park(); }
    else {
      // metadata is not in yet: park as soon as it is, the release stays scheduled on the clock
      pendingPos = pos;
      var once = function () {
        v.removeEventListener('loadedmetadata', once);
        pendingPos = null;
        park();
      };
      v.addEventListener('loadedmetadata', once);
      waitForRelease(v, pos, target, true);
    }
  }

  function waitForRelease(v, pos, target, metadataPending) {
    var released = false;
    var fire = function () {
      if (released) { return; }
      released = true;
      markExpect('play');
      try {
        if (msePlayer) { msePlayer.play(); sendState(true); return; }
        var p = v.play();
        if (p && p.catch) { p.catch(function (e) { sendError('play_failed', (e && e.name) || e); }); }
      } catch (e) { sendError('play_failed', (e && e.message) || e); }
      sendState(true);
    };
    var attempt = function () {
      if (released) { return; }
      var wait = target - rxNow();
      if (metadataPending && v.readyState < 1) {
        // The seek cannot land yet. Releasing anyway would play from wherever the element
        // happens to be (measured: 20 s early), so wait — and give up loudly rather than lie.
        if (rxNow() > target + METADATA_GIVE_UP_MS) {
          released = true;
          sendError('metadata_timeout',
                    'no metadata ' + METADATA_GIVE_UP_MS + ' ms past the release: not playing');
          return;
        }
        setTimeout(attempt, 100);
        return;
      }
      if (wait > 250) { setTimeout(attempt, 200); return; }
      if (wait > 0) { setTimeout(function () { attempt(); }, Math.max(0, wait)); return; }
      fire();
    };
    attempt();
  }

  function setElementRate(v, rate) {
    try { v.playbackRate = rate; } catch (e) { sendError('rate_failed', (e && e.message) || e); return; }
    try { playerManager.setPlaybackRate(v.playbackRate); } catch (e) { /* CAF may not track it */ }
  }

  function doPause() {
    var v = mediaEl();
    if (!v) { return; }
    markExpect('pause');
    if (msePlayer) { msePlayer.pause(); } else { try { v.pause(); } catch (e) {} }
    sendState(true);
  }

  function doSeek(m) {
    var v = mediaEl();
    var pos = Number(m.media);
    if (!v || !isFinite(pos)) { sendError('bad_seek', 'media is required'); return; }
    markExpect('seek');
    if (msePlayer) {
      msePlayer.seek(Math.max(0, pos)).then(function () { sendState(true); });
    } else {
      try { v.currentTime = Math.max(0, pos); } catch (e) { sendError('seek_failed', (e && e.message) || e); }
    }
    sendState(true);
  }

  function doRate(m) {
    var v = mediaEl();
    var value = Number(m.value);
    if (!v || !isFinite(value)) { sendError('bad_rate', 'value is required'); return; }
    if (value < 0.5 || value > 2.0) {          // contracts §3.1: rate is 0.5 … 2.0
      sendError('rate_out_of_range', 'asked ' + value + ', the contract allows 0.5 … 2.0');
      return;
    }
    setElementRate(v, value);
    if (msePlayer) { msePlayer.setRate(value); }
    sendState(true);
  }

  function doStop(tellReady) {
    var v = mediaEl();
    if (msePlayer) { try { msePlayer.stop(); } catch (e) {} msePlayer = null; }
    markExpect('pause');
    if (v) {
      try { v.pause(); } catch (e) {}
      try { v.removeAttribute('src'); v.load(); } catch (e) {}
    }
    try { playerManager.stop(); } catch (e) {}
    mediaLoaded = false;
    pendingPos = null;
    endedSent = false;
    showIdle('ready');
    sendState(true);
    if (tellReady) { hello(null); }
  }

  // ---- elements events: transitions and foreign intents ------------------------------------
  function onElementEvent(name) {
    var v = mediaEl();
    if (!v || !mediaLoaded) { return; }
    if (name === 'play') {
      if (!consumeExpect('play') && !consumeExpect('load')) {
        broadcast('intent', { action: 'play' });
      }
    } else if (name === 'pause') {
      if (!consumeExpect('pause')) { broadcast('intent', { action: 'pause' }); }
    } else if (name === 'seeked') {
      if (!consumeExpect('seek')) {
        var now = rxNow();
        if (now - lastJumpAt > 500) {
          lastJumpAt = now;
          broadcast('intent', { action: 'seek', media: v.currentTime });
        }
      }
    } else if (name === 'ended') {
      if (!endedSent) {
        endedSent = true;
        var s = sample();
        broadcast('ended', { rx_ms: s.rx_ms, media: s.media });
      }
    } else if (name === 'error') {
      var err = (v && v.error) || {};
      sendError('media_error', err.code != null ? ('code ' + err.code) : 'unknown');
    } else if (name === 'loadedmetadata') {
      if (pendingPos != null) {
        var p = pendingPos; pendingPos = null;
        try { v.currentTime = p; } catch (e) {}
      }
    }
    sendState(name === 'play' || name === 'pause' || name === 'ended' || name === 'error');
  }

  function attachElement(v) {
    if (!v || v.__nacBound) { return; }
    v.__nacBound = true;
    ['play', 'pause', 'playing', 'waiting', 'seeked', 'ended', 'error', 'ratechange',
     'loadedmetadata', 'canplay', 'stalled'].forEach(function (name) {
      v.addEventListener(name, function () { onElementEvent(name); });
    });
  }

  // ---- controller messages -----------------------------------------------------------------
  function onMessage(senderId, data) {
    var m = data;
    if (typeof m === 'string') { try { m = JSON.parse(m); } catch (e) { sendError('bad_json', 'payload is not JSON'); return; } }
    if (!m || typeof m !== 'object') { sendError('bad_message', 'payload is not an object'); return; }
    if (typeof m.seq === 'number') { seq = m.seq; }
    if (senderId) { lastSender = senderId; }
    if (!helo[senderId]) { helo[senderId] = true; hello(senderId); }
    var type = m.type;
    log('<- ' + type);
    switch (type) {
      case 'clock': {
        var s = sample();
        sendTo(senderId, envelope('clock_reply', { rx_ms: s.rx_ms }));
        break;
      }
      case 'load': doLoad(m); break;
      case 'play_at': doPlayAt(m); break;
      case 'pause': doPause(); break;
      case 'seek': doSeek(m); break;
      case 'rate': doRate(m); break;
      case 'stop': doStop(false); break;
      case 'idle': doStop(true); break;
      default: sendError('unknown_type', String(type));
    }
  }

  // ---- boot --------------------------------------------------------------------------------
  var logs = [];
  function log(line) {
    logs.push({ t: Math.round(rxNow()), line: String(line) });
    if (logs.length > 200) { logs.shift(); }
  }

  function onSystemEvent(name, fn) {
    try {
      var t = cast.framework.system.EventType[name];
      if (t === undefined) { return false; }
      context.addEventListener(t, fn);
      return true;
    } catch (e) { return false; }
  }

  function start() {
    showIdle('ready');
    onSystemEvent('SENDER_CONNECTED', function (e) {
      var id = e && e.senderId;
      if (id) { if (!helo[id]) { helo[id] = true; hello(id); } }
      attachElement(mediaEl());
    });
    onSystemEvent('SENDER_DISCONNECTED', function (e) {
      if (e && e.senderId) { delete helo[e.senderId]; }
    });
    // CAF reports media problems as events, not as exceptions (measured 2026-10-26: a DASH load
    // never reached metadata and the page said nothing, so the controller could not know why).
    try {
      playerManager.addEventListener(cast.framework.events.EventType.ERROR, function (e) {
        var code = (e && (e.detailedErrorCode || (e.error && e.error.code))) || 'unknown';
        sendError('caf_error', String(code));
      });
      playerManager.addEventListener(cast.framework.events.EventType.LOAD, function () {
        log('caf load');
      });
    } catch (e) { log('no caf events'); }

    context.addCustomMessageListener(NS, function (ev) {
      attachElement(mediaEl());
      onMessage(ev.senderId, ev.data);
    });
    try {
      // disableIdleTimeout: the device must never fall back to Backdrop on its own while the
      // controller still owns the session (contracts §3.3 — only a foreign cast ends us).
      context.start({
        disableIdleTimeout: true,
        statusText: APP_NAME,
        maxInactivity: 24 * 60 * 60
      });
    } catch (e) {
      showIdle('start failed');
      return;
    }
    attachElement(mediaEl());
    ticker = setInterval(function () {
      attachElement(mediaEl());
      var st = playerState();
      if (mediaLoaded) {
        sendState(false);
      } else if (st !== 'idle') {
        sendState(true);
      }
    }, STATE_PERIOD_MS);
    log('started ' + VERSION);
  }

  // Expose a tiny read-only surface for the device-side sanity check (and nothing that can be
  // driven from a web page).
  window.__nac = {
    version: VERSION,
    namespace: NS,
    state: function () { return { mediaLoaded: mediaLoaded, player_state: playerState(), rate: currentRate() }; },
    log: function () { return logs.slice(); }
  };

  start();
})();
