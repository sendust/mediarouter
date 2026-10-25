"use strict";
/* mediarouter UI: encoder cards, monitor cards, system panel, backend log */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const txt = await res.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (_) { data = txt; }
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

function toast(msg, isErr) {
  console[isErr ? "error" : "log"](msg);
  const el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = `position:fixed;right:16px;bottom:${16 + 40 * $$(".toast").length}px;z-index:99;padding:6px 12px;border-radius:4px;` +
    `background:${isErr ? "#e5534b" : "#2a313b"};color:#fff;max-width:520px`;
  el.className = "toast";
  document.body.append(el);
  setTimeout(() => el.remove(), isErr ? 6000 : 2500);
}

const fmt = {
  bytes(n) {
    if (n == null) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  },
  bps(n) {
    if (n == null) return "-";
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " Gb/s";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + " Mb/s";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + " kb/s";
    return n.toFixed(0) + " b/s";
  },
  dur(s) {
    if (s == null) return "-";
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const hms = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`;
    return d ? `${d}d ${hms}` : hms;
  },
  time(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  },
};
/* Clipboard. navigator.clipboard only exists in a secure context (https or
 * localhost); over plain http://<ip> copy falls back to execCommand and paste
 * can only be done with Ctrl+V. */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.append(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("복사 실패");
}
async function readClipboard() {
  if (navigator.clipboard && navigator.clipboard.readText && window.isSecureContext) return navigator.clipboard.readText();
  return null;
}
function flash(btn) {
  btn.classList.add("done");
  setTimeout(() => btn.classList.remove("done"), 1000);
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const App = {
  settings: {},
  encoders: new Map(),   // id -> EncoderCard
  monitors: new Map(),   // id -> MonitorCard
  mtx: { ok: false, paths: [] },
  pathInfo: {},
  whepBase() {
    const s = this.settings;
    return (s.webrtc_url || `${location.protocol}//${location.hostname}:${s.webrtc_port || 8889}`).replace(/\/$/, "");
  },
};

/* ------------------------------------------------------------ drag & drop */
function makeSortable(container, kind) {
  // While dragging, the card is hidden and a same-sized dashed placeholder marks
  // exactly where it will land; the other cards reflow around it.
  let dragEl = null, ph = null, origNext = null, dropped = false;
  const cards = () => $$(".card", container).filter((c) => c !== dragEl);
  const multiCol = () => getComputedStyle(container).gridTemplateColumns.split(" ").filter(Boolean).length > 1;

  container.addEventListener("pointerdown", (e) => {
    const h = e.target.closest(".handle");
    if (h) h.closest(".card").draggable = true;
  });
  container.addEventListener("dragstart", (e) => {
    dragEl = e.target.closest(".card");
    if (!dragEl) return;
    dropped = false;
    origNext = dragEl.nextSibling;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragEl.dataset.id);
    ph = document.createElement("div");
    ph.className = "card-placeholder";
    ph.style.height = dragEl.offsetHeight + "px";
    ph.innerHTML = `<span>여기에 놓기 — ${esc($(".title-in", dragEl)?.value || "")}</span>`;
    // hide the card only after the browser has taken its drag image
    setTimeout(() => {
      if (!dragEl) return;
      dragEl.after(ph);
      dragEl.classList.add("dragging");
    }, 0);
  });
  container.addEventListener("dragover", (e) => {
    if (!dragEl || !ph) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const over = e.target.closest(".card");
    if (over && over !== dragEl) {
      const r = over.getBoundingClientRect();
      const before = multiCol() ? e.clientX < r.left + r.width / 2 : e.clientY < r.top + r.height / 2;
      if (before) { if (over.previousElementSibling !== ph) over.before(ph); }
      else if (over.nextElementSibling !== ph) over.after(ph);
    } else if (!over && !e.target.closest(".card-placeholder")) {
      // empty area: past the last card -> end of the list
      const list = cards();
      const last = list[list.length - 1];
      if (last) {
        const r = last.getBoundingClientRect();
        if (e.clientY > r.bottom || (e.clientY > r.top && e.clientX > r.right)) last.after(ph);
      }
    }
  });
  container.addEventListener("drop", (e) => {
    if (!dragEl || !ph) return;
    e.preventDefault();
    dropped = true;
    container.dataset.lockUntil = Date.now() + 1500;   // keep SSE from reverting before the new order is saved
    ph.replaceWith(dragEl);
    ph = null;
    const ids = $$(".card", container).map((c) => c.dataset.id);
    api("POST", `/api/${kind}/order`, { ids }).catch((err) => toast(err.message, true));
  });
  container.addEventListener("dragend", () => {
    if (!dragEl) return;
    if (!dropped) container.insertBefore(dragEl, origNext && origNext.parentNode === container ? origNext : null);
    if (ph) ph.remove();
    dragEl.classList.remove("dragging");
    dragEl.draggable = false;
    dragEl = ph = origNext = null;
  });
}

/* ------------------------------------------------------------ encoders */
class EncoderCard {
  constructor(cfg) {
    this.id = cfg.id;
    this.cfg = cfg;
    this.el = $("#tpl-encoder").content.firstElementChild.cloneNode(true);
    this.el.dataset.id = cfg.id;
    this.cli = $(".cli", this.el);
    this.cliDirty = false;
    this.fflogTimer = null;
    this.bind();
    this.applyConfig(cfg, true);
  }
  f(name) { return $(`[data-f="${name}"]`, this.el); }
  async patch(data) {
    try {
      this.cfg = await api("PATCH", `/api/encoders/${this.id}`, data);
    } catch (e) { toast(e.message, true); }
  }
  bind() {
    const el = this.el;
    ["title", "comment"].forEach((k) => {
      const inp = this.f(k);
      inp.addEventListener("change", () => this.patch({ [k]: inp.value }));
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
    });
    ["autostart", "auto_restart", "speed_restart"].forEach((k) => {
      this.f(k).addEventListener("change", (e) => this.patch({ [k]: e.target.checked }));
    });
    const sched = () => this.patch({
      scheduled_restart: {
        enabled: this.f("sched_enabled").checked,
        mode: this.f("sched_mode").value,
        time: this.f("sched_time").value || "04:00",
        interval_min: Number(this.f("sched_interval").value) || 1440,
      },
    });
    ["sched_enabled", "sched_mode", "sched_time", "sched_interval"].forEach((k) => this.f(k).addEventListener("change", sched));
    this.f("sched_mode").addEventListener("change", () => this.schedVis());

    this.cli.addEventListener("input", () => this.setDirty(this.cli.value !== (this.cfg.cli || "")));
    $(".fflog-box", el).addEventListener("toggle", (e) => this.fflogPoll(e.target.open));

    el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-a]");
      if (!b) return;
      const a = b.dataset.a;
      try {
        if (a === "start" || a === "stop" || a === "restart") {
          if (a === "start" && this.cliDirty && !(await this.saveCli())) return;
          b.disabled = true;
          await api("POST", `/api/encoders/${this.id}/${a}`);
        } else if (a === "cli-save") {
          await this.saveCli();
        } else if (a === "cli-save-restart") {
          if (await this.saveCli()) await api("POST", `/api/encoders/${this.id}/restart`);
        } else if (a === "cli-copy") {
          await copyText(this.cli.value);
          flash(b);
          toast("CLI 복사됨");
        } else if (a === "cli-paste") {
          const text = await readClipboard().catch(() => null);
          if (text == null) {
            // insecure origin: select the text so a Ctrl+V replaces it
            this.cli.focus();
            this.cli.select();
            toast("http 접속에서는 브라우저가 붙여넣기 버튼을 막습니다 — 전체 선택했으니 Ctrl+V 로 붙여넣으세요");
          } else {
            this.cli.value = text;
            this.setDirty(this.cli.value !== (this.cfg.cli || ""));
            flash(b);
            toast("붙여넣음 — 저장해야 적용됩니다");
          }
        } else if (a === "fflog-copy") {
          await copyText($$(".fflog div", this.el).map((d) => d.textContent).join("\n"));
          flash(b);
          toast("fflog 복사됨");
        } else if (a === "cli-revert") {
          this.cli.value = this.cfg.cli || "";
          this.setDirty(false);
        } else if (a === "helper") {
          EncoderHelper.open(this.cfg);
        } else if (a === "delete") {
          if (await confirmBox(`인코더 "${this.cfg.title}" 를 삭제할까요? (실행중이면 정지됩니다)`)) {
            await api("DELETE", `/api/encoders/${this.id}`);
            this.destroy();
          }
        } else if (a === "fflog-clear") {
          await api("DELETE", `/api/encoders/${this.id}/fflog`);
          this.fflogLoad();
        }
      } catch (err) {
        toast(err.message, true);
      } finally {
        b.disabled = false;
      }
    });
  }
  setDirty(d) {
    this.cliDirty = d;
    $(".dirty", this.el).hidden = !d;
  }
  async saveCli() {
    try {
      this.cfg = await api("PATCH", `/api/encoders/${this.id}`, { cli: this.cli.value });
      this.setDirty(false);
      toast("CLI 저장됨");
      return true;
    } catch (e) { toast(e.message, true); return false; }
  }
  schedVis() {
    const daily = this.f("sched_mode").value === "daily";
    this.f("sched_time").hidden = !daily;
    this.f("sched_interval").hidden = daily;
  }
  applyConfig(cfg, force) {
    this.cfg = cfg;
    const set = (k, v, prop = "value") => {
      const el = this.f(k);
      if (force || document.activeElement !== el) el[prop] = v;
    };
    set("title", cfg.title || "");
    set("comment", cfg.comment || "");
    set("autostart", !!cfg.autostart, "checked");
    set("auto_restart", !!cfg.auto_restart, "checked");
    set("speed_restart", !!cfg.speed_restart, "checked");
    const s = cfg.scheduled_restart || {};
    set("sched_enabled", !!s.enabled, "checked");
    set("sched_mode", s.mode || "daily");
    set("sched_time", s.time || "04:00");
    set("sched_interval", s.interval_min || 1440);
    this.schedVis();
    if (!this.cliDirty && document.activeElement !== this.cli) this.cli.value = cfg.cli || "";
  }
  applyStatus(st) {
    const badge = $(".state", this.el);
    badge.textContent = st.state;
    badge.className = `badge state ${st.state}`;
    this.el.classList.toggle("live", st.state === "running");
    this.el.classList.toggle("slow", !!st.slow);
    const sb = $(".slowb", this.el);
    sb.hidden = !st.slow;
    if (st.slow) {
      sb.textContent = `SLOW ${st.speed_now.toFixed(2)}x`;
      sb.title = `최근 구간 처리속도 ${st.speed_now.toFixed(2)}x (실시간 미달) — ${fmt.dur(st.slow_for)} 째`;
    }
    const active = ["running", "starting", "restarting", "stopping"].includes(st.state);
    $('[data-a="start"]', this.el).disabled = active;
    $('[data-a="stop"]', this.el).disabled = !active;
    const p = st.progress || {};
    const cells = [
      ["pid", st.pid ?? "-"],
      ["uptime", fmt.dur(st.uptime)],
      ["fps", p.fps ?? "-"],
      ["frame", p.frame ?? "-"],
      ["speed", st.speed_now != null ? `${st.speed_now.toFixed(2)}x` : p.speed ?? "-"],
      ["bitrate", p.bitrate ?? "-"],
      ["drop/dup", p.drop_frames != null ? `${p.drop_frames}/${p.dup_frames}` : "-"],
      ["out time", p.out_time ? p.out_time.split(".")[0] : "-"],
      ["cpu", st.cpu != null ? st.cpu.toFixed(0) + "%" : "-"],
      ["mem", st.rss != null ? fmt.bytes(st.rss) : "-"],
      ["restarts", st.restarts],
      ["last exit", st.last_exit_code ?? "-"],
      ["next sched", fmt.time(st.next_scheduled_restart)],
      ["fflog", fmt.bytes(st.fflog_size)],
    ];
    if (st.restart_in != null) cells.splice(1, 1, ["restart in", st.restart_in.toFixed(1) + "s"]);
    $(".stats", this.el).innerHTML = cells.map(([k, v]) => `<div><span>${k}</span><b title="${esc(v)}">${esc(v)}</b></div>`).join("");
    const err = $(".err", this.el);
    let msg = ["error", "restarting"].includes(st.state) ? st.last_reason : "";
    if (st.last_error) msg = (msg ? msg + " — " : "") + `${st.last_error}  (${fmt.dur(st.last_error_age)} 전)`;
    err.hidden = !msg;
    err.textContent = msg;
    // a stale error from a healthy, running process is shown dimmed
    err.style.opacity = st.state === "running" && st.last_error_age > 60 ? 0.5 : 1;
    this.renderCodecs(st);
    $(".fflog-size", this.el).textContent = `${fmt.bytes(st.fflog_size)} (backup 포함 ${fmt.bytes(st.fflog_total)})`;
    $(".argv", this.el).textContent = st.argv || "(not started)";
  }
  renderCodecs(st) {
    const box = $(".codecs", this.el);
    const c = st.codecs || {};
    const fmtV = (v) => [v.name, v.size, v.fps && `${v.fps}fps`, v.kbps && `${v.kbps}k`].filter(Boolean).join(" ");
    const fmtA = (a) => [a.name, a.layout, a.hz && `${a.hz / 1000}k`, a.kbps && `${a.kbps}k`].filter(Boolean).join(" ") + (a.count > 1 ? ` ×${a.count}` : "");
    const parts = [];
    if ((c.video || []).length) parts.push(`<span class="ck">V</span>${c.video.map((v) => esc(fmtV(v))).join(", ")}`);
    if ((c.audio || []).length) parts.push(`<span class="ck">A</span>${c.audio.map((a) => esc(fmtA(a))).join(", ")}`);
    box.hidden = !parts.length;
    box.innerHTML = parts.join('<span class="sep">·</span>');
    // last run's codecs stay visible but dimmed while stopped
    box.classList.toggle("stale", st.state !== "running");
  }
  fflogPoll(on) {
    clearInterval(this.fflogTimer);
    this.fflogTimer = null;
    if (on) {
      this.fflogLoad();
      this.fflogTimer = setInterval(() => this.fflogLoad(), 2000);
    }
  }
  async fflogLoad() {
    try {
      const d = await api("GET", `/api/encoders/${this.id}/fflog?lines=400`);
      const pre = $(".fflog", this.el);
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
      pre.innerHTML = d.lines.map((l) => {
        const c = /\[(error|fatal|panic)\]/i.test(l) ? "e" : /\[warning\]/i.test(l) ? "w" : /=====/.test(l) ? "m" : "";
        return `<div class="${c}">${esc(l)}</div>`;
      }).join("");
      $(".fflog-path", this.el).textContent = d.path;
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    } catch (e) { /* card may be gone */ }
  }
  destroy() {
    this.fflogPoll(false);
    this.el.remove();
    App.encoders.delete(this.id);
    emptyHints();
  }
}

function syncEncoders(list) {
  const box = $("#encoders");
  const seen = new Set();
  list.forEach(({ config, status }, idx) => {
    seen.add(config.id);
    let card = App.encoders.get(config.id);
    if (!card) {
      card = new EncoderCard(config);
      App.encoders.set(config.id, card);
      box.append(card.el);
    } else {
      card.applyConfig(config);
    }
    card.applyStatus(status);
    // follow server order unless the user is dragging
    const locked = box.querySelector(".dragging, .card-placeholder") || Date.now() < Number(box.dataset.lockUntil || 0);
    if (!locked && box.children[idx] !== card.el) box.insertBefore(card.el, box.children[idx] || null);
  });
  [...App.encoders.keys()].forEach((id) => { if (!seen.has(id)) App.encoders.get(id).destroy(); });
  emptyHints();
}

/* ------------------------------------------------------------ monitors */
class MonitorCard {
  constructor(cfg) {
    this.id = cfg.id;
    this.cfg = cfg;
    this.el = $("#tpl-monitor").content.firstElementChild.cloneNode(true);
    this.el.dataset.id = cfg.id;
    this.view = new MonitorView(this.el, cfg, App.whepBase());
    Meters.views.add(this.view);
    this.sig = "";
    const t = $(".title-in", this.el);
    t.value = cfg.title || "";
    t.addEventListener("change", () => api("PATCH", `/api/monitors/${this.id}`, { title: t.value }).catch((e) => toast(e.message, true)));
    t.addEventListener("keydown", (e) => { if (e.key === "Enter") t.blur(); });
    this.infoTimer = setInterval(() => this.renderInfo(), 1000);
    const en = $('[data-f="enabled"]', this.el);
    en.checked = this.enabled();
    en.addEventListener("change", () => {
      this.cfg.enabled = en.checked;
      this.start();
      api("PATCH", `/api/monitors/${this.id}`, { enabled: en.checked }).catch((e) => toast(e.message, true));
    });
    this.el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-a]");
      if (!b) return;
      if (b.dataset.a === "reconnect") this.start();
      if (b.dataset.a === "edit") MonitorHelper.open(this.cfg);
      if (b.dataset.a === "delete" && (await confirmBox(`모니터 "${this.cfg.title}" 를 삭제할까요?`))) {
        await api("DELETE", `/api/monitors/${this.id}`).catch((err) => toast(err.message, true));
        this.destroy();
      }
    });
  }
  signature() {
    return (this.cfg.paths || []).map((p) => `${p}:${(App.pathInfo[p]?.tracks || []).map((t) => t.codec).join("+")}`).join(",");
  }
  enabled() { return this.cfg.enabled !== false; }
  renderInfo() {
    const box = $(".path-info", this.el);
    const html = (this.cfg.paths || []).map((name) => {
      const p = App.pathInfo[name];
      const st = this.view.stats[name] || {};
      if (!p) return `<div class="pi"><div class="pi-h"><span class="n">${esc(name)}</span> <span class="badge off">no path</span></div></div>`;
      const rows = (p.tracks || []).map((t) => {
        const isV = !!t.width || ["H264", "H265", "VP8", "VP9", "AV1", "MPEG-4 Video", "MPEG-1/2 Video", "M-JPEG"].includes(t.codec);
        const ws = isV ? st.video : st.audio;
        const parts = isV
          ? [t.codec + (t.profile ? ` ${t.profile}${t.level ? "@" + t.level : ""}` : ""),
             t.width ? `${t.width}x${t.height}` : (ws?.w ? `${ws.w}x${ws.h}` : ""),
             ws?.fps != null ? `${ws.fps.toFixed(1).replace(/\.?0+$/, "")} fps` : ""]
          : [t.codec, t.channels ? `${t.channels}ch` : "", t.sample_rate ? `${t.sample_rate / 1000} kHz` : ""];
        if (ws?.bps != null) parts.push(fmt.bps(ws.bps));
        if (ws?.lost) parts.push(`<span class="warn">lost ${ws.lost}</span>`);
        return `<div class="pi-t"><b>${isV ? "V" : "A"}</b> ${parts.filter(Boolean).join(" · ")}</div>`;
      }).join("");
      const head = [p.bps_in != null ? `in ${fmt.bps(p.bps_in)}` : "",
        p.source_type ? esc(p.source_type) : "",
        `readers ${p.readers.length}`].filter(Boolean).join(" · ");
      return `<div class="pi"><div class="pi-h"><span class="n">${esc(name)}</span> <span class="badge ${p.ready ? "on" : "off"}">${p.ready ? "ready" : "not ready"}</span> <span class="muted">${head}</span></div>${rows}</div>`;
    }).join("");
    if (box.innerHTML !== html) box.innerHTML = html;
  }
  start() {
    this.sig = this.signature();
    this.view.cfg = this.cfg;
    const on = this.enabled();
    this.el.classList.toggle("on", on);
    this.el.classList.toggle("off", !on);
    $('[data-a="reconnect"]', this.el).disabled = !on;
    if (on) this.view.start(App.pathInfo);
    else this.view.disable();
  }
  update(cfg) {
    const changed = JSON.stringify(cfg.paths) !== JSON.stringify(this.cfg.paths)
      || (cfg.enabled !== false) !== this.enabled();
    this.cfg = cfg;
    const t = $(".title-in", this.el);
    if (document.activeElement !== t) t.value = cfg.title || "";
    $('[data-f="enabled"]', this.el).checked = this.enabled();
    if (changed) this.start();
  }
  onPathInfo() {
    if (!this.enabled()) return;
    // the stream layout of a path changed (e.g. publisher came up) -> rebuild readers
    const sig = this.signature();
    if (sig !== this.sig) {
      clearTimeout(this.sigTimer);
      this.sigTimer = setTimeout(() => this.start(), 1500);
      this.sig = sig;
    }
  }
  destroy() {
    clearInterval(this.infoTimer);
    this.view.stop();
    Meters.views.delete(this.view);
    this.el.remove();
    App.monitors.delete(this.id);
    emptyHints();
  }
}

async function loadMonitors() {
  const list = await api("GET", "/api/monitors");
  const box = $("#monitors");
  const seen = new Set();
  list.forEach((cfg, idx) => {
    seen.add(cfg.id);
    let card = App.monitors.get(cfg.id);
    if (!card) {
      card = new MonitorCard(cfg);
      App.monitors.set(cfg.id, card);
      box.append(card.el);
      card.start();
    } else {
      card.update(cfg);
    }
    if (box.children[idx] !== card.el) box.insertBefore(card.el, box.children[idx] || null);
  });
  [...App.monitors.keys()].forEach((id) => { if (!seen.has(id)) App.monitors.get(id).destroy(); });
  emptyHints();
}

function emptyHints() {
  [["#encoders", App.encoders, "인코더가 없습니다. + Encoder 로 추가하세요."],
   ["#monitors", App.monitors, "모니터가 없습니다. + Monitor 로 mediamtx path 를 추가하세요."]].forEach(([sel, map, text]) => {
    const box = $(sel);
    let hint = $(".empty", box);
    if (map.size === 0 && !hint) {
      hint = document.createElement("div");
      hint.className = "empty";
      hint.textContent = text;
      box.append(hint);
    } else if (map.size && hint) hint.remove();
  });
}

/* ------------------------------------------------------------ system */
function renderSystem(s) {
  $("#hostname").textContent = s.hostname || "";
  const title = s.hostname ? `${s.hostname} — MEDIAROUTER by sendust` : "MEDIAROUTER by sendust";
  if (document.title !== title) document.title = title;
  $("#clock").textContent = s.time || "-";
  $("#top-cpu").textContent = s.cpu_percent != null ? s.cpu_percent.toFixed(1) + "%" : "-";
  $("#top-ff").textContent = s.ffmpeg_count ?? "-";
  $("#top-mem").textContent = s.mem_available != null ? fmt.bytes(s.mem_available) : "-";
  const ips = (s.nics || []).map((n) => `${n.name}: ${n.ips.join(", ") || "-"}`).join("<br>");
  const kv = [
    ["Hostname", esc(s.hostname)],
    ["IP", ips || "-"],
    ["CPU", esc(s.cpu_model)],
    ["Cores / Threads", `${s.cpu_cores ?? "-"} / ${s.cpu_threads ?? "-"}`],
    ["CPU 사용률", s.cpu_percent != null ? `${s.cpu_percent.toFixed(1)}% <span class="muted">(load ${(s.load || []).map((x) => x.toFixed(2)).join(" ")})</span>` : "-"],
    ["ffmpeg 프로세스", s.ffmpeg_count ?? "-"],
    ["RAM 전체", fmt.bytes(s.mem_total)],
    ["RAM free", `${fmt.bytes(s.mem_free)} <span class="muted">(available ${fmt.bytes(s.mem_available)})</span>`],
    ["시스템 시간", esc(s.time)],
  ];
  $("#sys-kv").innerHTML = kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("#sys-nics tbody").innerHTML = (s.nics || []).map((n) => {
    const cap = n.speed_mbps ? n.speed_mbps * 1e6 : 0;
    const bar = (v) => cap ? `<div class="bar"><i style="width:${Math.min(100, (v / cap) * 100).toFixed(1)}%"></i></div>` : "";
    return `<tr><td>${esc(n.name)}<div class="muted small">${n.speed_mbps ? n.speed_mbps + " Mb/s" : ""}</div></td>` +
      `<td>${n.ips.map(esc).join("<br>") || "-"}</td><td>${fmt.bps(n.rx_bps)}${bar(n.rx_bps)}</td><td>${fmt.bps(n.tx_bps)}${bar(n.tx_bps)}</td></tr>`;
  }).join("");
}

function renderMtx(m) {
  App.mtx = m;
  const info = {};
  (m.paths || []).forEach((p) => { info[p.name] = p; });
  App.pathInfo = info;
  const st = $("#mtx-state");
  st.textContent = m.ok ? `${(m.paths || []).length} paths` : "API offline";
  st.className = `badge ${m.ok ? "on" : "off"}`;
  const box = $("#mtx-paths");
  if (!m.ok) {
    box.innerHTML = `<div class="muted small">${esc(m.error)}<br>mediamtx.yml 의 api: true 및 config.json mediamtx.api_url 을 확인하세요.</div>`;
  } else if (!m.paths.length) {
    box.innerHTML = '<div class="muted small">활성화된 path 가 없습니다.</div>';
  } else {
    box.innerHTML = m.paths.map((p) => {
      const tracks = p.tracks.map((t) => t.codec + (t.width ? ` ${t.width}x${t.height}` : "") + (t.channels ? ` ${t.channels}ch` : "")).join(", ");
      const readers = p.readers.length
        ? p.readers.map((r) => `<div class="r">↳ ${esc(r.type)} ${esc(r.remote)} ${esc(r.transport || "")}</div>`).join("")
        : '<div class="r">↳ reader 없음</div>';
      return `<div class="mtx-path"><span class="n">${esc(p.name)}</span> <span class="badge ${p.ready ? "on" : "off"}">${p.ready ? "ready" : "not ready"}</span>` +
        `<div class="muted small">${esc(tracks)}</div>` +
        `<div class="small">source: ${esc(p.source_type || "-")} ${esc(p.source_remote || "")} · in ${fmt.bytes(p.bytes_in)} · out ${fmt.bytes(p.bytes_out)}</div>${readers}</div>`;
    }).join("");
  }
  App.monitors.forEach((c) => c.onPathInfo());
}

/* ------------------------------------------------------------ logs */
const Logs = {
  rows: [],
  max: 1000,
  add(list) {
    this.rows.push(...list);
    if (this.rows.length > this.max) this.rows.splice(0, this.rows.length - this.max);
    this.render(list.length > 200 ? null : list);
  },
  pass(r) {
    const lvl = $("#log-level").value;
    const order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
    if (lvl && order.indexOf(r.level) < order.indexOf(lvl)) return false;
    const f = $("#log-filter").value.toLowerCase();
    return !f || r.msg.toLowerCase().includes(f) || r.name.toLowerCase().includes(f);
  },
  line(r) {
    const d = new Date(r.ts * 1000);
    const p = (n, w = 2) => String(n).padStart(w, "0");
    const t = `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    return `<div class="${r.level}"><span class="t">${t}</span> ${r.level.padEnd(7)} [${esc(r.name)}] ${esc(r.msg)}</div>`;
  },
  render(appendOnly) {
    const box = $("#log");
    if (appendOnly) {
      box.insertAdjacentHTML("beforeend", appendOnly.filter((r) => this.pass(r)).map((r) => this.line(r)).join(""));
      while (box.childElementCount > this.max) box.firstElementChild.remove();
    } else {
      box.innerHTML = this.rows.filter((r) => this.pass(r)).map((r) => this.line(r)).join("");
    }
    $("#log-count").textContent = `(${this.rows.length})`;
    if ($("#log-follow").checked) box.scrollTop = box.scrollHeight;
  },
};
["#log-level", "#log-filter"].forEach((s) => $(s).addEventListener("input", () => Logs.render(null)));

/* ------------------------------------------------------------ confirm */
function confirmBox(text) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "dlg";
    d.innerHTML = `<form method="dialog" class="dlg-inner"><div>${esc(text)}</div><div class="dlg-foot">` +
      `<button value="no" class="btn ghost">취소</button><button value="yes" class="btn primary">확인</button></div></form>`;
    document.body.append(d);
    d.addEventListener("close", () => { resolve(d.returnValue === "yes"); d.remove(); });
    d.showModal();
  });
}

/* ------------------------------------------------------------ SSE */
function connectEvents() {
  const es = new EventSource("/events");
  const conn = $("#conn");
  es.onopen = () => { conn.textContent = "online"; conn.className = "badge on"; };
  es.onerror = () => { conn.textContent = "offline"; conn.className = "badge off"; };
  es.addEventListener("encoders", (e) => syncEncoders(JSON.parse(e.data)));
  es.addEventListener("system", (e) => renderSystem(JSON.parse(e.data)));
  es.addEventListener("mediamtx", (e) => renderMtx(JSON.parse(e.data)));
  es.addEventListener("logs", (e) => {
    const list = JSON.parse(e.data);
    // a reconnect re-sends the whole ring buffer; keep only what we don't have.
    // a lower max seq than ours means the backend restarted (seq starts over)
    let last = Logs.rows.length ? Logs.rows[Logs.rows.length - 1].seq : 0;
    if (list.length && list[list.length - 1].seq < last) { Logs.rows = []; last = 0; Logs.render(null); }
    const fresh = list.filter((r) => r.seq > last);
    if (fresh.length) Logs.add(fresh);
  });
}

async function init() {
  App.settings = await api("GET", "/api/settings");
  makeSortable($("#encoders"), "encoders");
  makeSortable($("#monitors"), "monitors");
  $("#btn-add-encoder").onclick = () => EncoderHelper.open(null);
  $("#btn-add-monitor").onclick = () => MonitorHelper.open(null);
  $("#audio-unlock").onclick = () => AudioMgr.ctx && AudioMgr.ctx.resume().then(() => AudioMgr.checkUnlock());
  try { renderMtx((await api("GET", "/api/system")).mediamtx); } catch (_) { /* SSE will fill it */ }
  connectEvents();
  await loadMonitors();
  setInterval(() => AudioMgr.checkUnlock(), 1000);
}
window.addEventListener("DOMContentLoaded", init);
