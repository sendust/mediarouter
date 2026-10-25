"use strict";
/* Monitor cards: WHEP (reader.js) per mediamtx path, Web Audio level meters.
 * skill §3: zero-gain routing keeps the RTP audio decoder running, fftSize 1024
 * skill §4: dBFS linear-to-pixel, fixed colour track + mask, attack/release, peak hold
 */
const METER_MIN_DB = -60;
const METER_MAX_DB = 0;
const METER_TICKS = [-60, -45, -30, -18, -6, 0];  // -12 omitted: it collides with -18/-6 on narrow meters
const ATTACK = 0.6;          // EMA coefficient on rise
const RELEASE = 0.08;        // EMA coefficient on fall
const PEAK_HOLD_MS = 1200;
const PEAK_DECAY_DB_S = 20;

const AudioMgr = {
  ctx: null,
  get() {
    if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    return this.ctx;
  },
  checkUnlock() {
    const el = document.getElementById("audio-unlock");
    el.hidden = !this.ctx || this.ctx.state === "running";
  },
};
document.addEventListener("click", () => {
  if (AudioMgr.ctx && AudioMgr.ctx.state !== "running") AudioMgr.ctx.resume().then(() => AudioMgr.checkUnlock());
});

function dbToPct(db) {
  const c = Math.min(METER_MAX_DB, Math.max(METER_MIN_DB, db));
  return ((c - METER_MIN_DB) / (METER_MAX_DB - METER_MIN_DB)) * 100;
}

class ChannelMeter {
  constructor(parent) {
    this.track = document.createElement("div");
    this.track.className = "mtrack";
    this.mask = document.createElement("div");
    this.mask.className = "mmask";
    this.peakEl = document.createElement("div");
    this.peakEl.className = "mpeak";
    this.track.append(this.mask, this.peakEl);
    parent.append(this.track);
    this.level = METER_MIN_DB;
    this.peak = METER_MIN_DB;
    this.peakTs = 0;
    this.analyser = null;
    this.buf = null;
  }
  attach(analyser) {
    this.analyser = analyser;
    this.buf = new Float32Array(analyser.fftSize);
  }
  update(now, dt) {
    let db = METER_MIN_DB;
    if (this.analyser) {
      this.analyser.getFloatTimeDomainData(this.buf);
      let sum = 0;
      for (let i = 0; i < this.buf.length; i++) sum += this.buf[i] * this.buf[i];
      const rms = Math.sqrt(sum / this.buf.length);
      db = rms > 0 ? 20 * Math.log10(rms) : METER_MIN_DB;   // full-scale sine -> -3.01 dBFS
    }
    const k = db > this.level ? ATTACK : RELEASE;
    this.level += (db - this.level) * k;
    if (this.level >= this.peak) {
      this.peak = this.level;
      this.peakTs = now;
    } else if (now - this.peakTs > PEAK_HOLD_MS) {
      this.peak = Math.max(this.level, this.peak - PEAK_DECAY_DB_S * dt);
    }
    const pct = dbToPct(this.level);
    this.mask.style.width = (100 - pct).toFixed(2) + "%";
    this.peakEl.style.left = `calc(${dbToPct(this.peak).toFixed(2)}% - 2px)`;
    return this.peak;
  }
}

class PathMeter {
  /* one row per mediamtx path: label + L/R bars */
  constructor(list, path, onListen) {
    this.path = path;
    this.row = document.createElement("div");
    this.row.className = "mgroup";
    this.label = document.createElement("div");
    this.label.className = "mlabel off";
    this.label.textContent = path;
    this.label.title = `${path} — 클릭하면 이 path 의 오디오를 모니터링(청취)`;
    this.label.onclick = (e) => { e.stopPropagation(); onListen(this); };
    this.bars = document.createElement("div");
    this.bars.className = "mbars";
    this.read = document.createElement("div");
    this.read.className = "mread";
    this.read.textContent = "-∞";
    // bars span the full meter width (same box as the dB scale, so ticks line up);
    // path label + peak reading sit on a line underneath
    const foot = document.createElement("div");
    foot.className = "mfoot";
    foot.append(this.label, this.read);
    this.row.append(this.bars, foot);
    list.append(this.row);
    this.ch = [new ChannelMeter(this.bars), new ChannelMeter(this.bars)];
    this.nodes = [];
    this.listenGain = null;
  }
  connect(stream) {
    this.disconnect();
    const ctx = AudioMgr.get();
    AudioMgr.checkUnlock();
    // Chrome bug: a remote WebRTC stream that is not attached to a media element
    // reaches Web Audio as pure silence. A muted, hidden <audio> sink makes Chrome
    // actually decode it (audible output still goes only through listenGain).
    this.sink = document.createElement("audio");
    this.sink.muted = true;
    this.sink.autoplay = true;
    this.sink.srcObject = stream;
    this.sink.play().catch(() => { /* muted autoplay is always allowed; ignore */ });
    const src = ctx.createMediaStreamSource(stream);
    // silent routing: keeps the browser's RTP audio decoder pulling frames
    const silent = ctx.createGain();
    silent.gain.value = 0;
    src.connect(silent).connect(ctx.destination);
    this.listenGain = ctx.createGain();
    this.listenGain.gain.value = 0;
    src.connect(this.listenGain).connect(ctx.destination);
    // a mono track is up-mixed so both bars show it. ChannelSplitter's channel
    // properties are fixed by the spec (setting them throws), so the up-mix is
    // done by a unity GainNode in front of it
    const upmix = ctx.createGain();
    upmix.channelCount = 2;
    upmix.channelCountMode = "explicit";
    upmix.channelInterpretation = "speakers";
    const split = ctx.createChannelSplitter(2);
    src.connect(upmix).connect(split);
    this.nodes = [src, silent, upmix, split, this.listenGain];
    this.ch.forEach((m, i) => {
      const an = ctx.createAnalyser();
      an.fftSize = 1024;
      split.connect(an, i);
      m.attach(an);
      this.nodes.push(an);
    });
    this.label.classList.remove("off");
  }
  setListen(on) {
    if (this.listenGain) this.listenGain.gain.value = on ? 1 : 0;
    this.label.classList.toggle("listen", on);
  }
  disconnect() {
    this.nodes.forEach((n) => { try { n.disconnect(); } catch (_) { /* already gone */ } });
    this.nodes = [];
    if (this.sink) { this.sink.pause(); this.sink.srcObject = null; this.sink = null; }
    this.ch.forEach((m) => { m.analyser = null; });
    this.label.classList.add("off");
    this.label.classList.remove("listen");
  }
  update(now, dt) {
    const p = Math.max(this.ch[0].update(now, dt), this.ch[1].update(now, dt));
    this.read.textContent = p <= METER_MIN_DB + 0.5 ? "-∞" : p.toFixed(1);
  }
}

class MonitorView {
  constructor(card, cfg, whepBase) {
    this.card = card;
    this.cfg = cfg;
    this.whepBase = whepBase;
    this.video = card.querySelector("video");
    this.msg = card.querySelector(".video-msg");
    this.list = card.querySelector(".meter-list");
    this.readers = [];
    this.meters = [];
    this.videoPath = null;
    this.listening = null;
    this.receivers = [];  // {path, kind, receiver, hist}
    this.stats = {};      // path -> {video:{fps,bps,w,h,lost}, audio:{bps,lost,jitter}}
    const scale = card.querySelector(".scale");
    scale.innerHTML = METER_TICKS.map((d) => `<span style="left:${dbToPct(d)}%">${d}</span>`).join("");
  }
  start(pathInfo) {
    this.stop();
    const paths = this.cfg.paths || [];
    // first path that carries video becomes the video window
    const hasVideo = (p) => (pathInfo[p]?.tracks || []).some((t) => ["H264", "H265", "VP8", "VP9", "AV1"].includes(t.codec));
    this.videoPath = paths.find(hasVideo) || paths[0] || null;
    this.msg.textContent = this.videoPath ? `connecting ${this.videoPath}…` : "no path";
    this.msg.hidden = false;
    paths.forEach((p) => {
      const info = pathInfo[p];
      const audio = !info || (info.tracks || []).some((t) => !["H264", "H265", "VP8", "VP9", "AV1"].includes(t.codec));
      const m = audio ? new PathMeter(this.list, p, (pm) => this.toggleListen(pm)) : null;
      if (m) this.meters.push(m);
      if (!audio && p !== this.videoPath) return;
      const r = new MediaMTXWebRTCReader({
        url: `${this.whepBase}/${encodeURIComponent(p)}/whep`,
        onError: (err) => {
          if (m) m.disconnect();
          if (p === this.videoPath) { this.msg.hidden = false; this.msg.textContent = `${p}: ${err}`; }
        },
        onTrack: (evt) => {
          const stream = evt.streams[0] || new MediaStream([evt.track]);
          if (evt.receiver) this.receivers.push({ path: p, kind: evt.track.kind, receiver: evt.receiver, hist: [] });
          if (evt.track.kind === "video" && p === this.videoPath) {
            this.video.srcObject = stream;
            this.msg.hidden = true;
          } else if (evt.track.kind === "audio" && m) {
            m.connect(new MediaStream([evt.track]));
            if (this.listening === m) m.setListen(true);
          }
        },
      });
      this.readers.push(r);
    });
    if (!paths.length) this.list.innerHTML = '<div class="muted small">path 없음</div>';
    this.statsTimer = setInterval(() => this.pollStats(), 1000);
  }
  async pollStats() {
    const out = {};
    await Promise.all(this.receivers.map(async (r) => {
      let rep;
      try { rep = await r.receiver.getStats(); } catch { return; }
      rep.forEach((st) => {
        if (st.type !== "inbound-rtp") return;
        // 5 s sliding window: smooth bitrate, and fractional fps (framesPerSecond is an integer)
        const cur = { t: st.timestamp, bytes: st.bytesReceived || 0, frames: st.framesDecoded || 0 };
        r.hist = (r.hist || []).filter((h) => cur.t - h.t <= 5000);
        const p = r.hist[0], dt = p ? (cur.t - p.t) / 1000 : 0;
        r.hist.push(cur);
        const s = { bps: dt > 0 ? ((cur.bytes - p.bytes) * 8) / dt : null, lost: st.packetsLost || 0 };
        if (r.kind === "video") {
          s.fps = dt > 0 ? (cur.frames - p.frames) / dt : null;
          s.w = st.frameWidth; s.h = st.frameHeight;
        } else {
          s.jitter = st.jitter;
        }
        (out[r.path] ||= {})[r.kind] = s;
      });
    }));
    this.stats = out;
  }
  disable() {
    this.stop();
    this.listening = null;
    this.msg.textContent = "disabled";
    this.msg.hidden = false;
    this.list.innerHTML = '<div class="muted small">disabled</div>';
  }
  toggleListen(pm) {
    const on = this.listening !== pm;
    this.meters.forEach((m) => m.setListen(false));
    this.listening = on ? pm : null;
    if (on) pm.setListen(true);
    if (AudioMgr.ctx && AudioMgr.ctx.state !== "running") AudioMgr.ctx.resume();
  }
  update(now, dt) { this.meters.forEach((m) => m.update(now, dt)); }
  stop() {
    clearInterval(this.statsTimer);
    this.receivers = [];
    this.stats = {};
    this.readers.forEach((r) => r.close());
    this.readers = [];
    this.meters.forEach((m) => m.disconnect());
    this.meters = [];
    this.list.innerHTML = "";
    this.video.srcObject = null;
  }
}

const Meters = {
  views: new Set(),
  last: performance.now(),
  loop(now) {
    const dt = Math.min(0.2, (now - Meters.last) / 1000);
    Meters.last = now;
    Meters.views.forEach((v) => v.update(now, dt));
    requestAnimationFrame(Meters.loop);
  },
};
requestAnimationFrame(Meters.loop);
