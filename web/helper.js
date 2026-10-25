"use strict";
/* Encoder helper dialog (input/output/video/audio -> ffmpeg CLI) and monitor helper */

const NV_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"];
const NV_TUNES = ["", "hq", "ll", "ull", "lossless"];
const X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"];
const X264_TUNES = ["", "film", "animation", "grain", "stillimage", "fastdecode"];
const SIZES = ["", "1920x1080", "1280x720", "960x540", "854x480", "640x360", "3840x2160"];
const VIDEO_CODECS = ["H264", "H265", "VP8", "VP9", "AV1", "MPEG-4 Video", "M-JPEG"];

function opts(sel, values, labels) {
  sel.innerHTML = values.map((v, i) => `<option value="${esc(v)}">${esc(labels ? labels[i] : v || "-")}</option>`).join("");
}
function radio(name) { return $(`input[name="${name}"]:checked`).value; }
function setRadio(name, v) { const r = $(`input[name="${name}"][value="${v}"]`); if (r) r.checked = true; }

const EncoderHelper = {
  meta: null,
  devices: [],
  editing: null,
  layout: [],
  timer: null,
  ready: false,

  async init() {
    if (this.ready) return;
    this.ready = true;
    $$(".sizesel").forEach((s) => opts(s, SIZES));
    $$('input[name="in_type"]').forEach((r) => r.addEventListener("change", () => { this.applyDefaults(); this.refresh(); }));
    $$('input[name="out_type"]').forEach((r) => r.addEventListener("change", () => this.refresh()));
    $("#v-codec").addEventListener("change", () => { this.moveCudaFilters(); this.codecLists(); this.refresh(); });
    ["#v-preset", "#v-tune", "#v-zl"].forEach((s) => $(s).addEventListener("change", () => this.hint()));
    $("#dl-device").addEventListener("change", () => {
      const d = this.devices[$("#dl-device").selectedIndex];
      if (d) $("#op-base").value = d.suggest_base;
      this.refresh();
    });
    $("#ts-standard").addEventListener("change", () => { $("#ts-custom").hidden = $("#ts-standard").value !== "custom"; this.refresh(); });
    $("#dl-query").addEventListener("click", () => this.queryFormats());
    $("#mx-refresh").addEventListener("click", () => this.loadPaths());
    $("#mx-probe").addEventListener("click", () => this.probe());
    $("#ou-add").addEventListener("click", () => { this.addUrl(); this.refresh(); });
    $("#helper-ok").addEventListener("click", () => this.submit());
    $("#e-cli-lock").addEventListener("change", (e) => { if (!e.target.checked) this.build(); });
    $("#helper-form").addEventListener("input", (e) => {
      if (e.target.id === "e-cli") { $("#e-cli-lock").checked = true; return; }
      if (["e-title", "e-comment", "e-autostart", "e-autorestart", "e-cli-lock"].includes(e.target.id)) return;
      this.refresh();
    });
    $("#helper-form").addEventListener("change", (e) => {
      if (e.target.matches("select, input[type=checkbox]") && !e.target.id.startsWith("e-")) this.refresh();
    });
  },

  async open(cfg) {
    await this.init();
    this.editing = cfg;
    $("#helper-title").textContent = cfg ? `Encoder 편집 — ${cfg.title}` : "Encoder 추가";
    $("#helper-ok").textContent = cfg ? "저장" : "추가";
    const dlg = $("#dlg-encoder");
    dlg.showModal();
    if (!this.meta) {
      this.meta = await api("GET", "/api/helper/meta");
      opts($("#dl-format"), this.meta.decklink_formats.map((f) => f.code), this.meta.decklink_formats.map((f) => `${f.label} (${f.code.trim()})`));
      opts($("#ts-source"), this.meta.lavfi_sources);
      opts($("#ts-standard"), [...this.meta.video_standards, "custom"]);
      $("#mx-transport").value = this.meta.rtsp_transport || "udp";
    }
    const c = this.meta.caps || {};
    const warn = [];
    if (!c.nvenc) warn.push(`NVENC 사용 불가 (${c.nvenc_error || "probe failed"})`);
    if (!c.cuda) warn.push(`CUDA filter 사용 불가 (${c.cuda_error || "probe failed"})`);
    $("#caps-warn").hidden = !warn.length;
    $("#caps-warn").textContent = warn.length ? `이 시스템에서 ${warn.join(" / ")} — libx264 / CPU filter 를 사용하세요.` : "";

    await Promise.all([this.loadDevices(), this.loadPaths()]);
    $("#ou-list").innerHTML = "";
    $("#e-cli-lock").checked = false;

    if (cfg && cfg.helper) {
      this.setSpec(cfg.helper);
    } else {
      setRadio("in_type", "decklink");
      this.applyDefaults();
    }
    $("#e-title").value = cfg ? cfg.title : "";
    $("#e-comment").value = cfg ? cfg.comment : "";
    $("#e-autostart").checked = cfg ? !!cfg.autostart : false;
    $("#e-autorestart").checked = cfg ? !!cfg.auto_restart : true;
    if (cfg) {
      // keep the stored CLI unless it is exactly what the helper would generate
      // (i.e. it was never edited by hand); unticking the lock regenerates it
      let same = false;
      if (cfg.helper) {
        try { same = (await api("POST", "/api/helper/build", this.getSpec())).cli === cfg.cli; } catch (_) { /* keep */ }
      }
      $("#e-cli-lock").checked = !same;
      $("#e-cli").value = cfg.cli || "";
    }
    this.refresh();
  },

  async loadDevices() {
    try { this.devices = await api("GET", "/api/helper/decklink"); } catch (_) { this.devices = []; }
    const sel = $("#dl-device");
    const keep = sel.value;
    opts(sel, this.devices.map((d) => d.name), this.devices.map((d) => `${d.name}`));
    if (!this.devices.length) sel.innerHTML = '<option value="">(DeckLink 장치 없음)</option>';
    if (keep) sel.value = keep;
  },

  async loadPaths() {
    let d = { paths: [] };
    try { d = await api("GET", "/api/helper/paths"); } catch (_) { /* offline */ }
    this.paths = d.paths || [];
    const names = this.paths.map((p) => p.name);
    const labels = this.paths.map((p) => `${p.name}  [${p.tracks.map((t) => t.codec + (t.channels ? ` ${t.channels}ch` : "")).join(", ")}]${p.ready ? "" : " (not ready)"}`);
    ["#mx-path", "#mx-extra"].forEach((s) => {
      const sel = $(s);
      const keep = [...sel.selectedOptions].map((o) => o.value);
      opts(sel, names, labels);
      [...sel.options].forEach((o) => { o.selected = keep.includes(o.value); });
    });
    if (!names.length) $("#mx-path").innerHTML = '<option value="">(활성 path 없음 — mediamtx API 확인)</option>';
    // an empty multi-select collapses to a thin vertical bar: show a disabled placeholder row instead
    const extra = $("#mx-extra");
    extra.disabled = !names.length;
    if (!names.length) extra.innerHTML = '<option disabled>(활성 path 없음)</option>';
  },

  async queryFormats() {
    const dev = $("#dl-device").value;
    $("#dl-query-msg").textContent = "조회중… (사용중인 장치는 조회 불가)";
    try {
      const d = await api("GET", `/api/helper/decklink/formats?device=${encodeURIComponent(dev)}`);
      if (d.formats.length) {
        const cur = $("#dl-format").value;
        opts($("#dl-format"), d.formats.map((f) => f.code), d.formats.map((f) => `${f.label} (${f.code})`));
        $("#dl-format").value = cur;
        $("#dl-query-msg").textContent = `${d.formats.length} formats`;
      } else {
        $("#dl-query-msg").textContent = "조회 실패: " + (d.raw || "").split("\n").slice(-2).join(" ");
      }
    } catch (e) { $("#dl-query-msg").textContent = e.message; }
  },

  async probe() {
    const out = $("#mx-probe-out");
    out.textContent = "probing…";
    const path = $("#mx-path").value;
    try {
      const d = await api("POST", "/api/helper/probe", { path, transport: $("#mx-transport").value });
      if (!d.ok) { out.textContent = d.error; return; }
      out.textContent = d.streams.map((s) => s.type === "video"
        ? `v:${s.index} ${s.codec} ${s.width}x${s.height} ${s.rate} ${s.field_order || ""}`
        : `a:${s.index} ${s.codec} ${s.channels}ch ${s.sample_rate}`).join("\n");
      const v = d.streams.find((s) => s.type === "video");
      if (v && v.field_order && !["progressive", "unknown"].includes(v.field_order)) $("#mx-interlaced").checked = true;
      this.probed = { path, streams: d.streams };
      this.refresh();
    } catch (e) { out.textContent = e.message; }
  },

  addUrl(url = "", format = "auto") {
    const row = document.createElement("div");
    row.className = "url-row";
    row.innerHTML = `<input class="ou-url" placeholder="rtmp://a.rtmp.youtube.com/live2/KEY" value="${esc(url)}">` +
      `<select class="ou-fmt"><option>auto</option><option>flv</option><option>mpegts</option><option>rtsp</option><option>rtp_mpegts</option></select>` +
      `<button type="button" class="btn ghost sm">✕</button>`;
    $(".ou-fmt", row).value = format;
    $("button", row).onclick = () => { row.remove(); this.refresh(); };
    $("#ou-list").append(row);
  },

  /* CUDA filters exist only on the NVENC path: carry the choice over to the CPU equivalent */
  moveCudaFilters() {
    const nv = $("#v-codec").value === "h264_nvenc";
    const cpuOk = !(nv && this.meta?.caps?.cuda);   // CPU filter row is hidden on the NVENC+CUDA path
    const cd = $("#f-cuda-deint"), cr = $("#f-cuda-resize"), d = $("#f-deint"), sc = $("#f-scale");
    if (!nv) {
      if (cd.value && !d.value) d.value = cd.value.replace("_cuda", "");
      if (cr.value && !sc.value) sc.value = cr.value;
      cd.value = "";
      cr.value = "";
    } else if (this.meta?.caps?.cuda) {
      // estdif has no CUDA version -> bwdif_cuda
      if (d.value && !cd.value) cd.value = d.value === "yadif" ? "yadif_cuda" : "bwdif_cuda";
      if (sc.value && !cr.value) cr.value = sc.value;
      d.value = "";
      sc.value = "";
    }
  },

  hint() {
    const codec = $("#v-codec").value;
    const p = $("#v-preset").value, t = $("#v-tune").value;
    let html;
    if (codec === "h264_nvenc") {
      const presets = {
        p1: "가장 빠름 · 화질 최저", p2: "매우 빠름", p3: "빠름", p4: "중간 (기본값) · 속도/화질 균형",
        p5: "느림 · 화질 좋음", p6: "더 느림 · 화질 더 좋음", p7: "가장 느림 · 화질 최고",
      };
      const tunes = {
        "": "지정 안함 — ffmpeg 기본값 hq 와 같음",
        hq: "<b>high quality</b> — 화질 우선. lookahead/B-frame 사용 가능 → 지연이 생길 수 있음. 녹화·VOD·외부 송출(지연 무관)용",
        ll: "<b>low latency</b> — 저지연. B-frame/lookahead 없이 즉시 출력, 화질은 hq 보다 약간 낮음. 라이브 전달·모니터용 권장",
        ull: "<b>ultra low latency</b> — 지연 최소, 화질 가장 낮음. 같은 비트레이트에서 블록이 더 보일 수 있음",
        lossless: "<b>lossless</b> — 무손실. 비트레이트 매우 큼, 스트리밍 부적합",
      };
      html = `<b>preset ${esc(p)}</b>: ${presets[p] || ""}. p1→p7 로 갈수록 화질은 좋아지고 GPU 부하가 커집니다. ` +
        `프리셋 자체는 프레임 지연을 거의 늘리지 않습니다 (P2200 1080p 기준 p4~p5 권장, 여러 채널이면 p4 이하).<br>` +
        `<b>tune ${esc(t || "-")}</b>: ${tunes[t] || ""}<br>` +
        `<b>zerolatency</b>: 프레임 재정렬 지연 없이 출력 (B-frame 0 과 함께 사용). 지연을 줄이려면 tune ll/ull + zerolatency + B-frame 0.`;
    } else if (codec === "libx264") {
      html = `<b>preset ${esc(p)}</b>: ultrafast→veryslow 로 갈수록 화질↑ CPU 부하↑. 라이브는 veryfast~fast 권장.<br>` +
        `<b>tune</b>: film/animation/grain 등은 소스 특성별 화질 튜닝. <b>zerolatency</b> 는 B-frame·lookahead·sliced-thread 설정으로 지연을 최소화 (화질 약간 하락).`;
    } else {
      html = "";
    }
    $("#v-hint").innerHTML = html;
    $("#v-hint").hidden = !html;
  },

  codecLists() {
    const codec = $("#v-codec").value;
    const nv = codec === "h264_nvenc";
    const p = $("#v-preset").value, t = $("#v-tune").value;
    opts($("#v-preset"), nv ? NV_PRESETS : X264_PRESETS);
    opts($("#v-tune"), nv ? NV_TUNES : X264_TUNES);
    $("#v-preset").value = (nv ? NV_PRESETS : X264_PRESETS).includes(p) ? p : nv ? "p4" : "veryfast";
    $("#v-tune").value = (nv ? NV_TUNES : X264_TUNES).includes(t) ? t : nv ? "ll" : "";
    $$(".nv-only").forEach((e) => { e.hidden = !nv; });
  },

  /* sensible defaults for the chosen input; only for a new encoder or when the input type changes */
  applyDefaults() {
    const inType = radio("in_type");
    const caps = (this.meta && this.meta.caps) || {};
    const live = inType === "decklink" || inType === "testsrc";
    setRadio("out_type", live ? "mediamtx_pairs" : "url");
    $("#v-codec").value = caps.nvenc ? "h264_nvenc" : "libx264";
    this.codecLists();
    const interlaced = inType === "decklink" || (inType === "testsrc" && ($("#ts-standard").value || "1080i5994").includes("i"));
    const gpu = caps.cuda && $("#v-codec").value === "h264_nvenc";
    $("#f-cuda-deint").value = interlaced && gpu ? "bwdif_cuda" : "";
    $("#f-deint").value = interlaced && !gpu ? "bwdif" : "";
    $("#f-cuda-resize").value = "";
    $("#f-scale").value = "";
    $("#f-fps").value = "";
    $("#v-br").value = live ? "8M" : "6M";
    $("#v-gop").value = 30;
    $("#v-bf").value = 0;
    $("#v-zl").checked = live;
    $("#a-codec").value = live ? "opus" : "aac";
    $("#a-br").value = "128k";
    if (inType === "testsrc") $("#op-base").value = "test1";
    if (inType === "decklink") {
      const d = this.devices[$("#dl-device").selectedIndex];
      $("#op-base").value = d ? d.suggest_base : "sdi1";
    }
    if (!$("#ou-list").children.length) this.addUrl();
  },

  /* flat channel list [{input,index,channels,label}] for the L/R pickers */
  computeLayout() {
    const inType = radio("in_type");
    if (inType === "decklink") return [{ input: 0, index: 0, channels: Number($("#dl-channels").value), label: "decklink" }];
    if (inType === "testsrc") return [{ input: 1, index: 0, channels: Number($("#ts-ach").value) || 1, label: "testsrc" }];
    if (inType === "custom") {
      return ($("#cu-layout").value || "2").split(",").map((n, i) => ({ input: 0, index: i, channels: Number(n) || 2, label: `0:a:${i}` }));
    }
    const paths = this.mxPaths();
    const out = [];
    paths.forEach((p, inputIdx) => {
      let tracks;
      if (this.probed && this.probed.path === p) {
        tracks = this.probed.streams.filter((s) => s.type === "audio").map((s) => s.channels || 2);
      } else {
        const info = (this.paths || []).find((x) => x.name === p);
        tracks = info ? info.tracks.filter((t) => !VIDEO_CODECS.includes(t.codec)).map((t) => t.channels || 2) : [2];
      }
      tracks.forEach((ch, k) => out.push({ input: inputIdx, index: k, channels: ch, label: `${p} a${k}` }));
    });
    return out;
  },

  mxPaths() {
    const main = $("#mx-path").value;
    const extra = [...$("#mx-extra").selectedOptions].map((o) => o.value).filter((v) => v && v !== main);
    return main ? [main, ...extra] : extra;
  },

  renderChannels() {
    this.layout = this.computeLayout();
    const vals = [], labels = [];
    let n = 0;
    this.layout.forEach((s) => {
      for (let c = 0; c < s.channels; c++) {
        vals.push(String(n));
        labels.push(`${s.label} ch${c + 1}`);
        n++;
      }
    });
    ["#a-left", "#a-right"].forEach((sel, i) => {
      const el = $(sel);
      const keep = el.value;
      opts(el, vals, labels);
      el.value = vals.includes(keep) ? keep : vals[Math.min(i, vals.length - 1)] || "";
    });
    // stereo pairs for the mediamtx_pairs sink
    const total = n;
    const base = $("#op-base").value || "sdi1";
    const box = $("#op-pairs");
    const prev = [...$$("input", box)].reduce((m, i) => { m[i.value] = i.checked; return m; }, {});
    box.innerHTML = "";
    for (let p = 0; p < Math.floor(total / 2); p++) {
      const name = `${base}_${p === 0 ? "va" : "a"}${2 * p + 1}${2 * p + 2}`;
      const lab = document.createElement("label");
      lab.innerHTML = `<input type="checkbox" value="${p}" ${prev[p] === false ? "" : "checked"}> ${esc(name)}`;
      box.append(lab);
    }
    if (total < 2) box.innerHTML = '<span class="muted small">스테레오 쌍을 만들 채널이 부족합니다</span>';
  },

  getSpec() {
    const inType = radio("in_type");
    const nv = $("#v-codec").value === "h264_nvenc";
    const cpuOk = !(nv && this.meta?.caps?.cuda);   // CPU filter row is hidden on the NVENC+CUDA path
    const outType = radio("out_type");
    const spec = {
      input: {
        type: inType,
        decklink: {
          device: $("#dl-device").value, format_code: $("#dl-format").value, channels: Number($("#dl-channels").value),
          raw_format: $("#dl-raw").value, video_input: $("#dl-vin").value,
        },
        mediamtx: { paths: this.mxPaths(), transport: $("#mx-transport").value, interlaced: $("#mx-interlaced").checked },
        testsrc: {
          source: $("#ts-source").value, standard: $("#ts-standard").value, size: $("#ts-size").value,
          rate: $("#ts-rate").value, interlaced: $("#ts-inter").checked,
          audio_channels: Number($("#ts-ach").value), sine_freq: Number($("#ts-freq").value),
        },
        custom: { args: $("#cu-args").value, layout: $("#cu-layout").value },
      },
      audio_layout: this.layout,
      sink: { type: outType, base: $("#op-base").value, pairs: $$("#op-pairs input:checked").map((i) => Number(i.value)) },
      outputs: [],
      video: {
        codec: $("#v-codec").value,
        filters: {
          // CUDA filters only on the NVENC path (the UI hides them otherwise)
          cuda_deint: nv ? $("#f-cuda-deint").value : "", cuda_resize: nv ? $("#f-cuda-resize").value : "",
          deint: cpuOk ? $("#f-deint").value : "", scale: cpuOk ? $("#f-scale").value : "", fps: $("#f-fps").value, deint_rate: $("#f-deint-rate").value,
        },
        bitrate: $("#v-br").value, maxrate: $("#v-maxrate").value, bufsize: $("#v-bufsize").value,
        gop: $("#v-gop").value, bframes: $("#v-bf").value, profile: $("#v-profile").value,
        preset: $("#v-preset").value, tune: $("#v-tune").value, rc: $("#v-rc").value, zerolatency: $("#v-zl").checked,
      },
      audio: {
        codec: $("#a-codec").value, left: Number($("#a-left").value || 0), right: Number($("#a-right").value || 1),
        bitrate: $("#a-br").value, sample_rate: Number($("#a-sr").value),
        loudnorm: { enabled: $("#a-ln").checked, I: $("#a-ln-i").value, LRA: $("#a-ln-lra").value, TP: $("#a-ln-tp").value },
      },
      custom: { global: $("#c-global").value, input: $("#c-input").value, output: $("#c-output").value },
    };
    if (outType === "mediamtx") spec.outputs = [{ type: "mediamtx", path: $("#om-path").value }];
    if (outType === "url") spec.outputs = $$("#ou-list .url-row").map((r) => ({ type: "url", url: $(".ou-url", r).value, format: $(".ou-fmt", r).value })).filter((o) => o.url);
    if (outType === "null") spec.outputs = [{ type: "null" }];
    return spec;
  },

  setSpec(s) {
    const i = s.input || {};
    setRadio("in_type", i.type || "decklink");
    const d = i.decklink || {};
    if (d.device) $("#dl-device").value = d.device;
    if (d.format_code) $("#dl-format").value = d.format_code;
    $("#dl-channels").value = String(d.channels || 8);
    $("#dl-raw").value = d.raw_format || "auto";
    $("#dl-vin").value = d.video_input || "";
    const m = i.mediamtx || {};
    const mp = m.paths || [];
    if (mp[0]) {
      if (![...$("#mx-path").options].some((o) => o.value === mp[0])) $("#mx-path").insertAdjacentHTML("beforeend", `<option>${esc(mp[0])}</option>`);
      $("#mx-path").value = mp[0];
    }
    [...$("#mx-extra").options].forEach((o) => { o.selected = mp.slice(1).includes(o.value); });
    $("#mx-transport").value = m.transport || "udp";
    $("#mx-interlaced").checked = !!m.interlaced;
    const t = i.testsrc || {};
    if (t.source) $("#ts-source").value = t.source;
    if (t.standard) $("#ts-standard").value = t.standard;
    $("#ts-custom").hidden = t.standard !== "custom";
    $("#ts-size").value = t.size || "1920x1080";
    $("#ts-rate").value = t.rate || "30000/1001";
    $("#ts-inter").checked = !!t.interlaced;
    $("#ts-ach").value = t.audio_channels || 8;
    $("#ts-freq").value = t.sine_freq || 1000;
    const c = i.custom || {};
    $("#cu-args").value = c.args || "";
    $("#cu-layout").value = c.layout || "2";

    const sk = s.sink || {};
    setRadio("out_type", sk.type || "mediamtx_pairs");
    $("#op-base").value = sk.base || "sdi1";
    $("#ou-list").innerHTML = "";
    (s.outputs || []).forEach((o) => {
      if (o.type === "url") this.addUrl(o.url, o.format || "auto");
      if (o.type === "mediamtx") $("#om-path").value = o.path;
    });
    if (!$("#ou-list").children.length) this.addUrl();

    const v = s.video || {};
    const f = v.filters || {};
    $("#v-codec").value = v.codec || "libx264";
    this.codecLists();
    $("#f-cuda-deint").value = f.cuda_deint || "";
    $("#f-cuda-resize").value = f.cuda_resize || "";
    $("#f-deint").value = f.deint || "";
    $("#f-scale").value = f.scale || "";
    $("#f-fps").value = f.fps || "";
    $("#f-deint-rate").value = f.deint_rate || "frame";
    $("#v-br").value = v.bitrate || "6M";
    $("#v-maxrate").value = v.maxrate || "";
    $("#v-bufsize").value = v.bufsize || "";
    $("#v-gop").value = v.gop ?? 30;
    $("#v-bf").value = v.bframes ?? 0;
    $("#v-profile").value = v.profile || "";
    if (v.preset) $("#v-preset").value = v.preset;
    $("#v-tune").value = v.tune || "";
    $("#v-rc").value = v.rc || "cbr";
    $("#v-zl").checked = !!v.zerolatency;
    const a = s.audio || {};
    $("#a-codec").value = a.codec || "aac";
    $("#a-br").value = a.bitrate || "128k";
    $("#a-sr").value = String(a.sample_rate || 48000);
    const ln = a.loudnorm || {};
    $("#a-ln").checked = !!ln.enabled;
    $("#a-ln-i").value = ln.I ?? -23;
    $("#a-ln-lra").value = ln.LRA ?? 7;
    $("#a-ln-tp").value = ln.TP ?? -2;
    this.renderChannels();
    $("#a-left").value = String(a.left ?? 0);
    $("#a-right").value = String(a.right ?? 1);
    const cu = s.custom || {};
    $("#c-global").value = cu.global || "";
    $("#c-input").value = cu.input || "";
    $("#c-output").value = cu.output || "";
    // pair selection
    if (sk.pairs) $$("#op-pairs input").forEach((el) => { el.checked = sk.pairs.includes(Number(el.value)); });
  },

  // true / false for the selected DeckLink standard, null when unknown (e.g. auto-detect)
  dlInterlaced() {
    const sel = $("#dl-format");
    const code = (sel.value || "").trim();
    if (!code) return null;
    const label = sel.selectedOptions[0]?.textContent || "";
    if (/\d+[ip]\d/.test(label)) return /interlaced|\d+i\d/.test(label);
    return code.startsWith("Hi") || ["ntsc", "nt23", "pal"].includes(code);
  },

  refresh() {
    const inType = radio("in_type");
    const outType = radio("out_type");
    $$(".in-pane").forEach((p) => { p.hidden = p.dataset.pane !== inType; });
    $$(".out-pane").forEach((p) => { p.hidden = p.dataset.pane !== outType; });
    const codec = $("#v-codec").value;
    $(".v-enc").hidden = codec === "copy" || codec === "none";
    // NVENC path: CUDA filters replace the CPU ones (CPU row only as a fallback when CUDA is unusable)
    const gpuFilters = codec === "h264_nvenc" && !!this.meta?.caps?.cuda;
    $$(".cuda-only").forEach((e) => { e.hidden = codec !== "h264_nvenc"; });
    $$(".cpu-only").forEach((e) => { e.hidden = gpuFilters; });
    // DeckLink progressive standard -> nothing to deinterlace
    const progressive = inType === "decklink" && this.dlInterlaced() === false;
    ["#f-cuda-deint", "#f-deint"].forEach((sel) => {
      const el = $(sel);
      if (progressive) el.value = "";
      else if (this.dlProgressive && el.disabled && inType === "decklink" && !el.closest("[hidden]"))
        el.value = sel === "#f-deint" ? "bwdif" : "bwdif_cuda";  // back to interlaced: restore the default
      el.disabled = progressive;
      el.title = progressive ? "progressive 비디오 표준에서는 deinterlace 를 사용하지 않습니다" : "";
    });
    this.dlProgressive = progressive;
    // deint output rate only means something when a (visible) deinterlacer is chosen
    const cudaDeint = codec === "h264_nvenc" && $("#f-cuda-deint").value;
    const cpuDeint = !gpuFilters && $("#f-deint").value;
    $("#f-deint-rate").disabled = !(cudaDeint || cpuDeint);
    this.hint();
    this.renderChannels();
    const pairs = outType === "mediamtx_pairs";
    $(".a-lr").hidden = pairs || $("#a-codec").value === "copy";
    $(".a-ln").hidden = ["copy", "none"].includes($("#a-codec").value);
    $$(".a-ln input[type=number]").forEach((i) => { i.disabled = !$("#a-ln").checked; });
    const notes = [];
    if (pairs) notes.push("각 스테레오 쌍이 개별 mediamtx path 로 전송됩니다 (L/R 선택 불필요).");
    const toMtx = outType.startsWith("mediamtx");
    if (toMtx && ["aac", "mp3"].includes($("#a-codec").value)) notes.push("WebRTC 모니터(브라우저)는 AAC/MP3 를 재생하지 못합니다 — 모니터할 path 는 opus 권장.");
    if (toMtx && Number($("#v-bf").value) > 0 && codec !== "copy") notes.push("WebRTC 재생에는 B-frame 0 을 권장합니다.");
    if (inType === "decklink" && !$("#f-cuda-deint").value && !$("#f-deint").value && codec !== "copy" && this.dlInterlaced())
      notes.push("interlaced 소스입니다 — deinterlace 필터를 고려하세요.");
    $("#a-note").textContent = notes.join(" ");
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.build(), 200);
  },

  async build() {
    if ($("#e-cli-lock").checked) return;
    try {
      const d = await api("POST", "/api/helper/build", this.getSpec());
      $("#e-cli").value = d.cli;
      $("#cli-err").textContent = "";
    } catch (e) {
      $("#cli-err").textContent = e.message;
    }
  },

  async submit() {
    const spec = this.getSpec();
    const locked = $("#e-cli-lock").checked;
    const body = {
      title: $("#e-title").value || this.autoTitle(spec),
      comment: $("#e-comment").value,
      autostart: $("#e-autostart").checked,
      auto_restart: $("#e-autorestart").checked,
      cli: $("#e-cli").value,
      // a hand-edited CLI that no longer matches the helper spec: keep the spec anyway
      // so the helper can be reopened, the stored cli is what runs
      helper: spec,
    };
    if (!body.cli.trim()) { toast("command line 이 비어있습니다", true); return; }
    try {
      if (this.editing) {
        await api("PATCH", `/api/encoders/${this.editing.id}`, body);
        const card = App.encoders.get(this.editing.id);
        if (card) { card.setDirty(false); card.cli.value = body.cli; }
        toast(locked ? "저장됨 (직접 편집한 CLI)" : "저장됨 — 다음 시작/재시작부터 적용");
      } else {
        await api("POST", "/api/encoders", body);
        toast("인코더 추가됨");
      }
      $("#dlg-encoder").close();
    } catch (e) { toast(e.message, true); }
  },

  autoTitle(spec) {
    const i = spec.input;
    const src = i.type === "decklink" ? i.decklink.device : i.type === "mediamtx" ? (i.mediamtx.paths[0] || "mediamtx") : i.type === "testsrc" ? i.testsrc.source : "custom";
    const dst = spec.sink.type === "mediamtx_pairs" ? `${spec.sink.base}_*` : spec.sink.type === "mediamtx" ? spec.outputs[0]?.path : spec.sink.type === "url" ? (spec.outputs[0]?.url || "").split("/")[2] || "url" : "null";
    return `${src} → ${dst}`;
  },
};

/* ------------------------------------------------------------ monitor helper */
const MonitorHelper = {
  editing: null,
  selected: [],
  bound: false,

  async open(cfg) {
    if (!this.bound) {
      this.bound = true;
      $("#m-refresh").onclick = () => this.load();
      $("#mon-ok").onclick = () => this.submit();
      $("#m-manual").addEventListener("change", () => {
        this.selected = $("#m-manual").value.split(",").map((s) => s.trim()).filter(Boolean);
        this.render();
      });
    }
    this.editing = cfg;
    this.selected = cfg ? [...(cfg.paths || [])] : [];
    $("#mon-dlg-title").textContent = cfg ? `Monitor 편집 — ${cfg.title}` : "Monitor 추가";
    $("#m-title").value = cfg ? cfg.title : "";
    $("#dlg-monitor").showModal();
    await this.load();
  },

  async load() {
    let d = { paths: [] };
    try { d = await api("GET", "/api/helper/paths"); } catch (_) { /* offline */ }
    this.paths = d.paths || [];
    if (!d.ok) toast("mediamtx API 에 연결할 수 없습니다: " + (d.error || ""), true);
    // group by prefix before the last "_" : sdi1_va12, sdi1_a34 -> sdi1
    const groups = {};
    this.paths.forEach((p) => {
      const g = p.name.includes("_") ? p.name.slice(0, p.name.lastIndexOf("_")) : p.name;
      (groups[g] = groups[g] || []).push(p.name);
    });
    const chips = $("#m-groups");
    chips.innerHTML = "";
    Object.entries(groups).forEach(([g, names]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn ghost sm";
      b.textContent = `${g} (${names.length})`;
      b.onclick = () => {
        // video-carrying path first, then the rest in name order
        const info = (n) => this.paths.find((p) => p.name === n);
        const hasV = (n) => (info(n)?.tracks || []).some((t) => VIDEO_CODECS.includes(t.codec));
        this.selected = [...names].sort((a, b2) => (hasV(b2) - hasV(a)) || a.localeCompare(b2));
        if (!$("#m-title").value) $("#m-title").value = g;
        this.render();
      };
      chips.append(b);
    });
    this.render();
  },

  render() {
    const box = $("#m-paths");
    const known = this.paths.map((p) => p.name);
    const all = [...known, ...this.selected.filter((s) => !known.includes(s))];
    box.innerHTML = all.length ? "" : '<div class="muted small">활성화된 path 가 없습니다. 아래에 직접 입력할 수 있습니다.</div>';
    all.forEach((name) => {
      const p = this.paths.find((x) => x.name === name);
      const idx = this.selected.indexOf(name);
      const lab = document.createElement("label");
      const tracks = p ? p.tracks.map((t) => t.codec + (t.channels ? ` ${t.channels}ch` : "")).join(", ") : "offline";
      lab.innerHTML = `<input type="checkbox" ${idx >= 0 ? "checked" : ""}><span class="ord">${idx >= 0 ? idx + 1 : ""}</span>` +
        `<span>${esc(name)}</span><span class="muted">${esc(tracks)}</span>`;
      $("input", lab).onchange = (e) => {
        if (e.target.checked) this.selected.push(name); else this.selected = this.selected.filter((s) => s !== name);
        this.render();
      };
      box.append(lab);
    });
    $("#m-manual").value = this.selected.join(", ");
  },

  async submit() {
    const body = { title: $("#m-title").value || this.selected[0] || "monitor", paths: this.selected };
    try {
      if (this.editing) await api("PATCH", `/api/monitors/${this.editing.id}`, body);
      else await api("POST", "/api/monitors", body);
      $("#dlg-monitor").close();
      await loadMonitors();
    } catch (e) { toast(e.message, true); }
  },
};
