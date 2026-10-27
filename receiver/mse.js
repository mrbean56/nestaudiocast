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
 *   fragments   → fetched by byte range, appended in order, kept `lookahead` seconds ahead
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
  var LOOKAHEAD = 20;
  //: never let the buffer run further than this (memory on a Chromecast is small)
  var HORIZON = 60;

  function MsePlayer(el, opts) {
    this.el = el;
    this.opts = opts || {};
    //: extra elements, one per additional track (this device allows one SourceBuffer each)
    this.flock = [];
    this.log = this.opts.log || function () {};
    this.mediaSource = null;
    this.tracks = [];
    this.playing = false;
    this.rate = 1;
    this.stopped = false;
    this.pendingSeek = null;
    this.onUpdate = this.opts.onUpdate || function () {};
    this.onError = this.opts.onError || function () {};
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
      self.pendingSeek = atTime || 0;
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
        self.log('add ' + spec.mime + ' state=' + g.ms.readyState);
        try {
          buffer = g.ms.addSourceBuffer(spec.mime);
        } catch (e) {
          throw new Error('addSourceBuffer refused "' + spec.mime + '" on ' + g.el.tagName +
                          ': ' + ((e && e.message) || String(e)));
        }
        buffer.mode = 'segments';
        spec.buffer = buffer;
        spec.next = 0;
        spec.el = g.el;
        self.tracks.push(spec);
        return appendBuffer(buffer, spec.init);
      });
    }));
  };

  //: Keep both tracks about LOOKAHEAD seconds ahead of the playhead.
  MsePlayer.prototype.pump = function () {
    var self = this;
    if (this.stopped || !this.tracks.length) { return Promise.resolve(); }
    var now = this.el.currentTime;
    var jobs = this.tracks.map(function (spec) {
      return pumpTrack(self, spec, now);
    });
    return Promise.all(jobs).then(function () {
      self._afterPump();
    }).catch(function (err) {
      self.onError('mse_pump', (err && err.message) || String(err));
    });
  };

  function pumpTrack(self, spec, now) {
    var table = spec.table;
    if (spec.next >= table.length) { return Promise.resolve(); }
    if (spec.next > 0) {
      var last = table[spec.next - 1];
      if (last.time + last.duration > now + HORIZON) { return Promise.resolve(); }
      if (last.time + last.duration > now + LOOKAHEAD) { return Promise.resolve(); }
    }
    var frag = table[spec.next];
    spec.next += 1;
    return fetchRange(spec.sidxUrl, frag.start, frag.end).then(function (bytes) {
      return appendBuffer(spec.buffer, bytes);
    }).then(function () {
      // a seek that is waiting for its position to become buffered gets released here
      if (spec.next === 1 && self.pendingSeek !== null) { /* handled in _afterPump */ }
      return pumpTrack(self, spec, now);
    });
  }

  MsePlayer.prototype._afterPump = function () {
    if (this.pendingSeek !== null) {
      var self = this;
      var want = this.pendingSeek;
      if (this.bufferedStart() <= want + 0.25) {
        this._elements().forEach(function (el) {
          try { el.currentTime = want; } catch (e) { /* the buffer will catch up */ }
        });
        this.pendingSeek = null;
        if (this.playing) { self._play(); }
      }
    }
    this.sync();
    this.onUpdate();
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
    this.tracks.forEach(function (spec) {
      var i = fragmentAt(spec.table, Math.max(0, t));
      spec.next = i;
      try { spec.buffer.abort(); } catch (e) {}
      removeBuffered(spec.buffer);
    });
    this.pendingSeek = Math.max(0, t);
    return this.pump().then(function () { self.onUpdate(); });
  };

  MsePlayer.prototype.position = function () {
    return this.el.currentTime;
  };

  MsePlayer.prototype.stop = function () {
    this.stopped = true;
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
  //: MediaSource, so the audio lives on its own element).
  MsePlayer.prototype.sync = function () {
    var self = this;
    var master = this.el;
    var other = this._elements().filter(function (el) { return el !== master; });
    other.forEach(function (el) {
      try {
        if (!el.paused && Math.abs(el.currentTime - master.currentTime) > 0.06) {
          el.currentTime = master.currentTime;
        }
        if (master.paused && !el.paused) { el.pause(); }
        if (!master.paused && el.paused) { self._play(); }
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

  function appendBuffer(buffer, bytes) {
    return new Promise(function (resolve, reject) {
      if (buffer.updating) {
        buffer.addEventListener('updateend', resolve, { once: true });
        return;
      }
      buffer.addEventListener('updateend', resolve, { once: true });
      buffer.addEventListener('error', function () { reject(new Error('append failed')); }, { once: true });
      try { buffer.appendBuffer(bytes); } catch (e) { reject(e); }
    });
  }

  function removeBuffered(buffer) {
    try {
      if (buffer.buffered && buffer.buffered.length && buffer.updating === false) {
        buffer.remove(0, buffer.buffered.end(buffer.buffered.length - 1));
      }
    } catch (e) { /* nothing buffered */ }
  }

  return { parseSidx: parseSidx, fragmentTable: fragmentTable, fragmentAt: fragmentAt,
           MsePlayer: MsePlayer, readBoxes: readBoxes, appendBuffer: appendBuffer };
});
