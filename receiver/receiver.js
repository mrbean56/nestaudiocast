(function () {
  'use strict';

  var NS = 'urn:x-cast:com.beansden.nestaudiocast.v1';
  var VERSION = '0.2.0';
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

  function mediaEl() {
    if (dashMode) {
      var caf = document.querySelector('cast-media-player video');
      if (caf) { return caf; }
    }
    return ownElement();
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
      version: VERSION, app: APP_NAME, app_id: '28028CD2', element: elementInfo()
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
        dashMode = true;   // from here CAF owns the media pipeline and its element
        playerManager.load(new cast.framework.messages.LoadRequestData({
          media: new cast.framework.messages.MediaInformation({ contentId: url, contentType: ct, streamType: 'BUFFERED' }),
          currentTime: start,
          autoplay: !!m.autoplay
        }));
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
    try { v.pause(); } catch (e) {}
    sendState(true);
  }

  function doSeek(m) {
    var v = mediaEl();
    var pos = Number(m.media);
    if (!v || !isFinite(pos)) { sendError('bad_seek', 'media is required'); return; }
    markExpect('seek');
    try { v.currentTime = Math.max(0, pos); } catch (e) { sendError('seek_failed', (e && e.message) || e); }
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
    sendState(true);
  }

  function doStop(tellReady) {
    var v = mediaEl();
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
