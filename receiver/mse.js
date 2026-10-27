/*
 * A very small MSE player for our own receiver page.
 *
 * Why this exists (measured 2026-10-26, notes/labo-receiver.md §C6): the DASH manifest generated
 * for a YouTube stream plays on **neither** player on the device — our page gets `loading` then
 * no metadata, and the platform DMR drops the session — while every byte range of the same media
 * is provably correct (206 + Content-Range, `ftyp`/`moov` init, `sidx` index, 2.5 GB total).
 * CAF's DASH player is therefore not usable here, and it is not needed: the page already owns its
 * `<video>` element and drives `playbackRate` on it.
 *
 * So the page reads the `sidx` itself and feeds `SourceBuffer`s, one per track:
 *   init range  → appended once
 *   sidx range  → parsed, gives the size and duration of every subsegment
 *   fragments   → fetched by byte range, appended in order, kept `LOOKAHEAD` seconds ahead
 *                 by a 2 Hz refill, and removed once they are `BEHIND` seconds behind
 *   last fragment → `endOfStream()`, so the element fires `ended`
 * Seeking is exact: the fragment that contains the requested time is found in the sidx table and
 * the buffer restarts from there.
 *
 * One constraint found on the device (measured 2026-10-26): this Chromecast's MediaSource accepts
 * **one** SourceBuffer — the video buffer is created and the audio one is refused with "This
 * MediaSource has reached the limit of SourceBuffer objects it can handle". So each track gets
 * its own element and its own MediaSource, and the audio element is slaved to the video one.
 *
 * No dependency, no manifest parser beyond the two ranges the MPD already gave us.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; }
  root.NacMse = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // ---- box parsing -------------------------------------------------------------------------
  function readBoxes(view, start, end) {
    var out = [], o = start;
    while (o + 8 <= end) {
      var size = view.getUint32(o);
      var type = String.fromCharCode(view.getUint8(o + 4), view.getUint8(o + 5),
                                    view.getUint8(o + 6), view.getUint8(o + 7));
      var header = 8;
      if (size === 1) {                       // 64-bit size
        size = Number(view.getBigUint64(o + 8));
        header = 16;
      } else if (size === 0) {
        size = end - o;
      }
      if (size < header || o + size > end) { break; }
      out.push({ type: type, start: o, end: o + size, body: o + header });
      o += size;
    }
    return out;
  }

  //: Parse the `sidx` box of one track. ``at`` is the byte offset of ``buffer`` in the file, so
  //: every offset it returns is file-absolute (the first version returned buffer-relative ones and
  //: put the first fragment 742 bytes early — the init length — which the fixture caught).
  function parseSidx(buffer, at) {
    at = at || 0;
    var view = new DataView(buffer);
    var boxes = readBoxes(view, 0, buffer.byteLength);
    var sidx = null;
    for (var i = 0; i < boxes.length; i++) { if (boxes[i].type === 'sidx') { sidx = boxes[i]; break; } }
    if (!sidx) { return null; }
    var o = sidx.body;
    var version = view.getUint8(o);
    var timescale = view.getUint32(o + 8);
    var earliest, firstOffset;
    if (version === 0) {
      earliest = view.getUint32(o + 12);
      firstOffset = view.getUint32(o + 16);
      o += 20;
    } else {
      earliest = Number(view.getBigUint64(o + 12));
      firstOffset = Number(view.getBigUint64(o + 20));
      o += 28;
    }
    o += 2;                                   // reserved
    var count = view.getUint16(o); o += 2;
    var refs = [], time = 0, offset = 0;
    for (var k = 0; k < count; k++) {
      var word = view.getUint32(o);
      var refType = (word & 0x80000000) ? 'index' : 'media';
      var refSize = word & 0x7fffffff;
      var duration = view.getUint32(o + 4);
      refs.push({ type: refType, size: refSize, duration: duration,
                  start: offset, time: time / timescale });
      offset += refSize;
      time += duration;
      o += 12;
    }
    return {
      timescale: timescale,
      earliest: earliest / timescale,
      duration: time / timescale,
      firstOffset: firstOffset,
      // ISO: the first subsegment starts at the end of the sidx box plus first_offset
      anchor: at + sidx.end + firstOffset,
      refs: refs,
      boxEnd: sidx.end,
    };
  }

  //: Byte offsets of every fragment of one track, ready to fetch.
  function fragmentTable(sidx) {
    return sidx.refs.filter(function (r) { return r.type === 'media'; })
      .map(function (r) {
        return { start: sidx.anchor + r.start, end: sidx.anchor + r.start + r.size - 1,
                 time: r.time, duration: r.duration / sidx.timescale };
      });
  }

  //: The fragment that contains `t` (or the last one that starts before it).
  function fragmentAt(table, t) {
    var lo = 0, hi = table.length - 1, best = 0;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (table[mid].time <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return best;
  }

  // ---- the player ---------------------------------------------------------------------------
  //: how far ahead of the playhead we keep the buffer full, in seconds
  var LOOKAHEAD = 12;
  //: what we keep behind the playhead; older data is removed (a 2 h stream on a Chromecast
  //: would otherwise run into the SourceBuffer quota)
  var BEHIND = 20;
  //: how often the buffers are refilled and the audio element is checked against the video
  var TICK_MS = 500;
  //: audio-on-video: below this the audio is left alone, above it the audio is nudged by rate,
  //: and beyond HARD it is re-seated (a seek is an audible glitch, so it is the last resort)
  var AUDIO_DEAD_S = 0.02;
  var AUDIO_NUDGE = 0.005;
  var AUDIO_HARD_S = 0.25;

  function MsePlayer(el, opts) {
    this.el = el;
    this.opts = opts || {};
    this.log = this.opts.log || function () {};
    this.tracks = [];
    this.groups = [];
    this.playing = false;
    this.rate = 1;
    this.stopped = false;
    this.pendingSeek = null;
    this.timer = null;
    this.onUpdate = this.opts.onUpdate || function () {};
    this.onError = this.opts.onError || function () {};
    //: tests drive tick() by hand; the page uses the real interval
    this.setInterval = 'setInterval' in this.opts ? this.opts.setInterval
      : (typeof setInterval === 'function' ? setInterval : null);
    this.clearInterval = this.opts.clearInterval
      || (typeof clearInterval === 'function' ? clearInterval : function () {});
  }

  MsePlayer.prototype.supported = function () {
    return typeof MediaSource !== 'undefined' && typeof window !== 'undefined' &&
           typeof window.fetch === 'function';
  };

  MsePlayer.prototype.start = function (tracks, atTime, extraEls) {
    var self = this;
    if (!this.supported()) { return Promise.reject(new Error('no MSE in this browser')); }
    this.stopped = false;
    var elements = [this.el].concat(extraEls || []);
    var groups = tracks.map(function (spec, i) {
      return { spec: spec, el: elements[i] || elements[0] };
    });
    return Promise.all(groups.map(function (g) {
      g.ms = new MediaSource();
      g.url = URL.createObjectURL(g.ms);
      g.el.src = g.url;
      return new Promise(function (resolve) {
        g.ms.addEventListener('sourceopen', resolve, { once: true });
      });
    })).then(function () {
      self.groups = groups;
      return self._loadTracks(groups);
    }).then(function () {
      if (self.stopped) { return null; }
      self.pendingSeek = atTime || 0;
      self._startTimer();
      return self.pump();
    }).catch(function (err) {
      self.onError('mse_start', (err && err.message) || String(err));
      throw err;
    });
  };

  MsePlayer.prototype._loadTracks = function (groups) {
    var self = this;
    return Promise.all(groups.map(function (g) {
      var spec = g.spec;
      return fetchRange(spec.initUrl, spec.initStart, spec.initEnd).then(function (init) {
        spec.init = init;
        return fetchRange(spec.sidxUrl, spec.sidxStart, spec.sidxEnd);
      }).then(function (sidxBuf) {
        var sidx = parseSidx(sidxBuf, spec.sidxStart);
        if (!sidx) { throw new Error('no usable sidx for ' + spec.mime); }
        spec.sidx = sidx;
        spec.table = fragmentTable(sidx);
        if (!spec.table.length) { throw new Error('empty fragment table for ' + spec.mime); }
        var buffer;
        self.log('add ' + spec.mime + ' #' + bufferCount(g.ms) + ' state=' + g.ms.readyState);
        try {
          buffer = g.ms.addSourceBuffer(spec.mime);
        } catch (e) {
          throw new Error('addSourceBuffer refused "' + spec.mime + '" on ' + g.el.tagName +
                          ': ' + ((e && e.message) || String(e)));
        }
        buffer.mode = 'segments';
        spec.buffer = buffer;
        spec.ms = g.ms;
        spec.next = 0;
        spec.epoch = 0;          // bumped by every seek: work started before it is dropped
        spec.busy = null;        // the epoch whose fetch loop is running, if any
        spec.eos = false;
        spec.el = g.el;
        self.tracks.push(spec);
        return appendBuffer(buffer, spec.init);
      });
    }));
  };

  MsePlayer.prototype._startTimer = function () {
    var self = this;
    if (this.timer !== null || !this.setInterval) { return; }
    this.timer = this.setInterval(function () { self.tick(); }, TICK_MS);
  };

  //: One refill + one audio check. The page's interval calls it; nothing else has to.
  MsePlayer.prototype.tick = function () {
    if (this.stopped) { return Promise.resolve(); }
    return this.pump();
  };

  //: Keep every track LOOKAHEAD seconds ahead of the playhead and BEHIND seconds behind it.
  //: One fetch loop per track at a time: a second call while one runs is a no-op, so the page's
  //: interval and a seek can never append the same track concurrently.
  MsePlayer.prototype.pump = function () {
    var self = this;
    if (this.stopped || !this.tracks.length) { return Promise.resolve(); }
    var jobs = this.tracks.map(function (spec) { return pumpTrack(self, spec); });
    return Promise.all(jobs).then(function () {
      self._afterPump();
    }).catch(function (err) {
      self.onError('mse_pump', (err && err.message) || String(err));
    });
  };

  function pumpTrack(self, spec) {
    if (self.stopped || spec.buffer === null || spec.busy === spec.epoch) { return Promise.resolve(); }
    var epoch = spec.epoch;
    spec.busy = epoch;
    var live = function () { return !self.stopped && spec.buffer !== null && spec.epoch === epoch; };
    var done = function () { if (spec.busy === epoch) { spec.busy = null; } };

    function step() {
      if (!live()) { return null; }
      var now = self._playhead();
      var table = spec.table;
      if (spec.next >= table.length) { return endOfStream(spec); }
      if (spec.next > 0) {
        var last = table[spec.next - 1];
        if (last.time + last.duration > now + LOOKAHEAD) { return null; }
      }
      var frag = table[spec.next];
      return evict(spec.buffer, now - BEHIND).then(function () {
        if (!live()) { return null; }
        return fetchRange(spec.sidxUrl, frag.start, frag.end);
      }).then(function (bytes) {
        if (bytes === null || !live()) { return null; }   // a seek or a stop during the fetch
        return appendWithRoom(spec, bytes, now).then(function () {
          if (!live()) { return null; }
          spec.next += 1;                                 // only once the bytes are in
          return step();
        });
      });
    }

    return Promise.resolve().then(step).then(done, function (err) {
      done();
      // "This SourceBuffer has been removed" is what a stop racing a pump looks like; it is
      // not a session failure (measured 2026-10-27, and it aborted a share).
      if (!live()) { return null; }
      throw err;
    });
  }

  //: The position the buffers are filled around: a pending seek target counts as the playhead.
  MsePlayer.prototype._playhead = function () {
    return this.pendingSeek !== null ? this.pendingSeek : this.el.currentTime;
  };

  MsePlayer.prototype._afterPump = function () {
    if (this.pendingSeek !== null) {
      var self = this;
      var want = this.pendingSeek;
      var ready = this.tracks.every(function (spec) { return hasTime(spec.buffer, want); });
      if (ready) {
        this._elements().forEach(function (el) {
          try { el.currentTime = want; } catch (e) { /* the buffer will catch up */ }
        });
        this.pendingSeek = null;
        if (this.playing) { self._play(); }
      }
    }
    this.sync();
  };

  MsePlayer.prototype.bufferedStart = function () {
    try {
      var b = this.el.buffered;
      return b && b.length ? b.start(0) : 0;
    } catch (e) { return 0; }
  };

  MsePlayer.prototype.bufferedEnd = function () {
    try {
      var b = this.el.buffered;
      return b && b.length ? b.end(b.length - 1) : null;
    } catch (e) { return null; }
  };

  MsePlayer.prototype._play = function () {
    this._elements().forEach(function (el) {
      try {
        var p = el.play();
        if (p && p.catch) { p.catch(function () {}); }
      } catch (e) { /* one element refusing must not stop the other */ }
    });
  };

  MsePlayer.prototype._elements = function () {
    var els = [];
    this.groups.forEach(function (g) { if (els.indexOf(g.el) < 0) { els.push(g.el); } });
    return els.length ? els : [this.el];
  };

  MsePlayer.prototype.play = function () {
    this.playing = true;
    this._play();
  };

  MsePlayer.prototype.pause = function () {
    this.playing = false;
    this._elements().forEach(function (el) { try { el.pause(); } catch (e) {} });
  };

  MsePlayer.prototype.setRate = function (value) {
    this.rate = value;
    this._elements().forEach(function (el) { try { el.playbackRate = value; } catch (e) {} });
  };

  MsePlayer.prototype.seek = function (t) {
    var self = this;
    var target = Math.max(0, t);
    this.tracks.forEach(function (spec) {
      spec.epoch += 1;                 // any fetch still in flight is dropped when it lands
      spec.busy = null;
      spec.eos = false;
      spec.next = fragmentAt(spec.table, target);
      // (an `ended` MediaSource reopens by itself on the next append)
      try { if (spec.buffer.updating) { spec.buffer.abort(); } } catch (e) {}
    });
    this.pendingSeek = target;
    return Promise.all(this.tracks.map(function (spec) {
      return removeAll(spec.buffer);
    })).then(function () {
      return self.pump();
    }).then(function () { self.onUpdate(); });
  };

  MsePlayer.prototype.position = function () {
    return this.el.currentTime;
  };

  MsePlayer.prototype.stop = function () {
    this.stopped = true;
    if (this.timer !== null) { try { this.clearInterval(this.timer); } catch (e) {} this.timer = null; }
    (this.tracks || []).forEach(function (spec) { spec.buffer = null; });
    this.playing = false;
    var self = this;
    this._elements().forEach(function (el) {
      try { el.pause(); } catch (e) {}
      try { el.removeAttribute('src'); el.load(); } catch (e) {}
    });
    (this.groups || []).forEach(function (g) {
      try { if (g.url) { URL.revokeObjectURL(g.url); } } catch (e) {}
    });
    self.tracks = [];
    self.groups = [];
  };

  //: Keep the extra elements on the master clock (this device needs one SourceBuffer per
  //: MediaSource, so the audio lives on its own element). Small errors are taken out by rate,
  //: so the TV's sound never jumps; only a large one (a stall, a seek) re-seats the audio.
  MsePlayer.prototype.sync = function () {
    var self = this;
    var master = this.el;
    var other = this._elements().filter(function (el) { return el !== master; });
    other.forEach(function (el) {
      try {
        if (master.paused) {
          if (!el.paused) { el.pause(); }
          return;
        }
        var base = master.playbackRate || self.rate || 1;
        var d = el.currentTime - master.currentTime;      // > 0: audio ahead
        if (Math.abs(d) > AUDIO_HARD_S) {
          el.currentTime = master.currentTime;
          el.playbackRate = base;
        } else if (Math.abs(d) > AUDIO_DEAD_S) {
          el.playbackRate = base * (d > 0 ? 1 - AUDIO_NUDGE : 1 + AUDIO_NUDGE);
        } else if (el.playbackRate !== base) {
          el.playbackRate = base;
        }
        if (el.paused) { self._play(); }
      } catch (e) { /* the sync must never throw into the caller */ }
    });
  };

  MsePlayer.prototype.ready = function () {
    return this.tracks.length > 0 && this.bufferedEnd() !== null;
  };

  MsePlayer.prototype.audioElement = function () {
    var extra = this._elements().filter(function (el) { return el !== this.el; }, this);
    return extra.length ? extra[0] : null;
  };

  // ---- helpers ------------------------------------------------------------------------------
  function bufferCount(ms) {
    try { return ms.sourceBuffers ? ms.sourceBuffers.length + 1 : '?'; } catch (e) { return '?'; }
  }

  function fetchRange(url, start, end) {
    return fetch(url, { headers: { Range: 'bytes=' + start + '-' + end } }).then(function (r) {
      if (!r.ok && r.status !== 206) { throw new Error('range ' + start + '-' + end + ' -> ' + r.status); }
      return r.arrayBuffer();
    });
  }

  //: Resolves once the buffer is idle. A SourceBuffer refuses any operation while `updating`.
  function idle(buffer) {
    return new Promise(function (resolve) {
      if (!buffer || !buffer.updating) { resolve(); return; }
      buffer.addEventListener('updateend', function () { resolve(); }, { once: true });
    });
  }

  //: Run one SourceBuffer operation and resolve on its own `updateend` — never drops the work
  //: when the buffer happens to be busy (it waits, then does it).
  function operate(buffer, op) {
    return idle(buffer).then(function () {
      return new Promise(function (resolve, reject) {
        var onEnd = function () { buffer.removeEventListener('error', onErr); resolve(); };
        var onErr = function () {
          buffer.removeEventListener('updateend', onEnd);
          reject(new Error('sourcebuffer error'));
        };
        buffer.addEventListener('updateend', onEnd, { once: true });
        buffer.addEventListener('error', onErr, { once: true });
        try { op(buffer); } catch (e) {
          buffer.removeEventListener('updateend', onEnd);
          buffer.removeEventListener('error', onErr);
          reject(e);
        }
      });
    });
  }

  function appendBuffer(buffer, bytes) {
    return operate(buffer, function (b) { b.appendBuffer(bytes); });
  }

  //: Append, and on a full buffer free what is behind the playhead once and try again.
  function appendWithRoom(spec, bytes, now) {
    return appendBuffer(spec.buffer, bytes).catch(function (err) {
      if (!err || err.name !== 'QuotaExceededError') { throw err; }
      return evict(spec.buffer, now - 1).then(function () {
        return appendBuffer(spec.buffer, bytes);
      });
    });
  }

  //: Remove everything buffered before `until` (seconds). No-op when there is nothing to free.
  function evict(buffer, until) {
    try {
      var b = buffer.buffered;
      if (!b || !b.length || until <= 0 || b.start(0) >= until - 0.5) { return Promise.resolve(); }
    } catch (e) { return Promise.resolve(); }
    return operate(buffer, function (sb) { sb.remove(0, until); });
  }

  function removeAll(buffer) {
    try {
      var b = buffer && buffer.buffered;
      if (!b || !b.length) { return Promise.resolve(); }
      var end = b.end(b.length - 1);
      return operate(buffer, function (sb) { sb.remove(0, end + 1); });
    } catch (e) { return Promise.resolve(); }
  }

  function hasTime(buffer, t) {
    try {
      var b = buffer.buffered;
      for (var i = 0; i < b.length; i++) {
        if (b.start(i) <= t + 0.25 && t < b.end(i)) { return true; }
      }
    } catch (e) { /* removed */ }
    return false;
  }

  //: Every fragment of the track is in: tell the MediaSource, so the element can fire `ended`.
  function endOfStream(spec) {
    if (spec.eos) { return null; }
    return idle(spec.buffer).then(function () {
      if (spec.eos || !spec.ms || spec.ms.readyState !== 'open') { return null; }
      try { spec.ms.endOfStream(); spec.eos = true; } catch (e) { /* retried next tick */ }
      return null;
    });
  }

  return { parseSidx: parseSidx, fragmentTable: fragmentTable, fragmentAt: fragmentAt,
           MsePlayer: MsePlayer, readBoxes: readBoxes, appendBuffer: appendBuffer };
});
