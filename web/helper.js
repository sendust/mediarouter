"use strict";
/* Encoder helper dialog (input/output/video/audio -> ffmpeg CLI) and monitor helper */

const NV_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"];
const NV_TUNES = ["", "hq", "ll", "ull", "lossless"];
/* filename-safe base name, same rule as the backend (cli_builder.safe_filename) */
const safeName = (s, fallback = "record") =>
  (s || "").replace(/[^\p{L}\p{M}\p{N}_.-]+/gu, "_").replace(/_{2,}/g, "_").replace(/\.{2,}/g, ".").replace(/^[._-]+|[._-]+$/g, "").slice(0, 100) || fallback;
const isNv = (codec) => codec === "h264_nvenc" || codec === "hevc_nvenc";
const PROFILES = { h264: ["", "high", "main", "baseline"], hevc_nvenc: ["", "main", "main10"] };
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
    // extra audio paths: a plain click toggles the option (no Ctrl needed to add or to deselect)
    $("#mx-extra").addEventListener("mousedown", (e) => {
      const o = e.target.closest("option");
      if (!o || o.disabled || e.button !== 0) return;
      e.preventDefault();
      o.selected = !o.selected;
      $("#mx-extra").focus();
      $("#mx-extra").dispatchEvent(new Event("change", { bubbles: true }));
    });
    $("#ts-standard").addEventListener("change", () => { $("#ts-custom").hidden = $("#ts-standard").value !== "custom"; this.refresh(); });
    $("#dl-query").addEventListener("click", () => this.queryFormats());
    $("#al-refresh").addEventListener("click", () => this.loadAlsa().then(() => this.refresh()));
    $("#al-device").addEventListener("change", () => {
      // keep following the device while the path name is still the generated one
      if (/^alsa_/.test($("#om-path").value)) $("#om-path").value = this.alsaPath();
      this.refresh();
    });
    $("#al-channels").addEventListener("change", () => this.refresh());
    $("#al-mixer").addEventListener("click", () => MixerDialog.open(this.alsaDev()?.card));
    $("#mx-refresh").addEventListener("click", () => this.loadPaths());
    $("#mx-probe").addEventListener("click", () => this.probe());
    $("#ou-add").addEventListener("click", () => { this.addUrl(); this.refresh(); });
    $("#ou-add-mc").addEventListener("click", () => {
      // remove the untouched empty URL row so the preset replaces it
      $$("#ou-list .url-row").forEach((r) => { if (!r.classList.contains("mc") && !$(".ou-url", r).value) r.remove(); });
      this.addMcast();
      if ($("#a-codec").value === "opus") $("#a-codec").value = "aac";  // most MPEG-TS receivers don't decode Opus
      this.refresh();
    });
    // file output: folder picker, write test, base filename follows the title until edited
    $("#of-browse").addEventListener("click", () => this.browse($("#of-dir").value));
    $("#of-up").addEventListener("click", () => { if (this.browseParent) this.browse(this.browseParent); });
    $("#of-list").addEventListener("click", (e) => {
      const a = e.target.closest("a[data-path]");
      if (a) { e.preventDefault(); this.browse(a.dataset.path); }
    });
    $("#of-pick").addEventListener("click", () => {
      $("#of-dir").value = this.browseCur;
      $("#of-picker").hidden = true;
      this.checkDir();
      this.refresh();
    });
    $("#of-cancel").addEventListener("click", () => { $("#of-picker").hidden = true; });
    $("#of-dir").addEventListener("change", () => this.checkDir());
    $("#of-base").addEventListener("input", () => { this.baseEdited = !!$("#of-base").value; });
    $("#of-base").addEventListener("change", () => {
      const b = $("#of-base");
      if (b.value) b.value = safeName(b.value, "");
      this.refresh();
    });
    $("#e-title").addEventListener("input", () => {
      if (this.baseEdited) return;
      $("#of-base").value = safeName($("#e-title").value, "");
      if (radio("out_type") === "file") this.refresh();
    });
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
    $("#helper-title").textContent = cfg ? t("Encoder 편집 — {0}", cfg.title) : t("Encoder 추가");
    $("#helper-ok").textContent = cfg ? t("저장") : t("추가");
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
    if (!c.nvenc) warn.push(t("NVENC 사용 불가 ({0})", c.nvenc_error || "probe failed"));
    if (c.nvenc && c.hevc_nvenc === false) warn.push(t("HEVC NVENC 사용 불가 ({0})", c.hevc_nvenc_error || "probe failed"));
    const hevcOpt = $('#v-codec option[value="hevc_nvenc"]');
    hevcOpt.disabled = c.hevc_nvenc === false;
    hevcOpt.textContent = c.hevc_nvenc === false ? t("hevc_nvenc (사용 불가)") : "hevc_nvenc (h265)";
    if (!c.cuda) warn.push(t("CUDA filter 사용 불가 ({0})", c.cuda_error || "probe failed"));
    if (c.nvdec === false) warn.push(t("NVDEC 사용 불가 ({0})", c.nvdec_error || "probe failed"));
    $("#caps-warn").hidden = !warn.length;
    $("#caps-warn").textContent = warn.length ? t("이 시스템에서 {0} — libx264 / CPU filter 를 사용하세요.", warn.join(" / ")) : "";

    await Promise.all([this.loadDevices(), this.loadPaths(), this.loadLocalIps(), this.loadAlsa()]);
    $("#ou-list").innerHTML = "";
    $("#e-cli-lock").checked = false;

    // file output fields: reset (setSpec fills them only when the encoder records to file)
    $("#of-ext").value = "ts";
    $("#of-dir").value = this.meta.recording?.default_dir || "";
    $("#of-base").value = "";
    $("#of-picker").hidden = true;
    this.dirOk = null;
    if (cfg && cfg.helper) {
      this.setSpec(cfg.helper);
    } else {
      setRadio("in_type", "decklink");
      $("#in-nvdec").checked = false;
      this.applyDefaults();
      $("#out-transport").value = this.meta.rtsp_transport || "udp";
    }
    $("#e-title").value = cfg ? cfg.title : "";
    $("#e-comment").value = cfg ? cfg.comment : "";
    const base = $("#of-base").value;
    this.baseEdited = !!base && base !== safeName($("#e-title").value, "");
    this.checkDir();
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

  async loadAlsa() {
    try { this.alsa = await api("GET", "/api/helper/alsa"); } catch (_) { this.alsa = { devices: [] }; }
    const sel = $("#al-device");
    const keep = sel.value;
    const devs = this.alsa.devices || [];
    opts(sel, devs.map((d) => d.device), devs.map((d) => `${d.card}: ${d.pcm_name} (${d.device})${d.busy ? t(" — 사용중") : ""}`));
    if (!devs.length) sel.innerHTML = `<option value="">${t("(ALSA 캡처 장치 없음)")}</option>`;
    if (keep && devs.some((d) => d.device === keep)) sel.value = keep;
  },

  alsaDev() {
    return (this.alsa?.devices || []).find((d) => d.device === $("#al-device").value);
  },

  /* default mediamtx path for the chosen ALSA device: alsa_<card>[_<pcm>] */
  alsaPath() {
    const d = this.alsaDev();
    return d ? `alsa_${d.card.toLowerCase()}${d.pcm ? "_" + d.pcm : ""}` : "alsa1";
  },

  async loadDevices() {
    try { this.devices = await api("GET", "/api/helper/decklink"); } catch (_) { this.devices = []; }
    const sel = $("#dl-device");
    const keep = sel.value;
    opts(sel, this.devices.map((d) => d.name), this.devices.map((d) => `${d.name}`));
    if (!this.devices.length) sel.innerHTML = `<option value="">${t("(DeckLink 장치 없음)")}</option>`;
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
    if (!names.length) $("#mx-path").innerHTML = `<option value="">${t("(활성 path 없음 — mediamtx API 확인)")}</option>`;
    // an empty multi-select collapses to a thin vertical bar: show a disabled placeholder row instead
    const extra = $("#mx-extra");
    extra.disabled = !names.length;
    if (!names.length) extra.innerHTML = `<option disabled>${t("(활성 path 없음)")}</option>`;
  },

  async queryFormats() {
    const dev = $("#dl-device").value;
    $("#dl-query-msg").textContent = t("조회중… (사용중인 장치는 조회 불가)");
    try {
      const d = await api("GET", `/api/helper/decklink/formats?device=${encodeURIComponent(dev)}`);
      if (d.formats.length) {
        const cur = $("#dl-format").value;
        opts($("#dl-format"), d.formats.map((f) => f.code), d.formats.map((f) => `${f.label} (${f.code})`));
        $("#dl-format").value = cur;
        $("#dl-query-msg").textContent = `${d.formats.length} formats`;
      } else {
        $("#dl-query-msg").textContent = t("조회 실패: ") + (d.raw || "").split("\n").slice(-2).join(" ");
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

  /* preset: MPEG-TS over UDP multicast. Stored as a normal url output (+ the fields to edit it again) */
  async loadLocalIps() {
    try {
      const sys = await api("GET", "/api/system");
      this.localIps = ((sys.system || sys).nics || []).flatMap((n) => n.ips.filter((ip) => !ip.startsWith("127.")).map((ip) => ({ ip, nic: n.name })));
    } catch { this.localIps = this.localIps || []; }
  },
  addMcast(mc = {}) {
    this.localIps ||= [];
    const used = $$("#ou-list .url-row.mc").length;
    // proto: rows saved before RTP became the default have no proto and were UDP
    const m = { proto: "rtp", group: "239.1.1." + (1 + used), port: 5000, ttl: 16, localaddr: "",
                fec: false, l: 10, d: 10, ...mc };
    if (!mc.proto && mc.group) m.proto = "udp";
    const row = document.createElement("div");
    row.className = "url-row mc";
    const ifOpts = [`<option value="">NIC: default route</option>`]
      .concat(this.localIps.map((x) => `<option value="${esc(x.ip)}">${esc(x.nic)} ${esc(x.ip)}</option>`));
    if (m.localaddr && !this.localIps.some((x) => x.ip === m.localaddr)) ifOpts.push(`<option value="${esc(m.localaddr)}">${esc(m.localaddr)}</option>`);
    row.innerHTML =
      `<select class="mc-proto" title="${t("RTP: -f rtp_mpegts rtp://… (RTP 시퀀스로 손실 확인, Pro-MPEG FEC 가능) / UDP: -f mpegts udp://… (pkt_size 1316)")}">` +
        `<option value="rtp">RTP-TS</option><option value="udp">UDP-TS</option></select>` +
      `<input class="mc-group" placeholder="239.1.1.1" value="${esc(m.group)}" title="multicast group (224.0.0.0 – 239.255.255.255)">` +
      `<span>:</span><input class="mc-port" type="number" min="1" max="65535" value="${esc(m.port)}" title="port">` +
      `<label>TTL <input class="mc-ttl" type="number" min="1" max="255" value="${esc(m.ttl)}"></label>` +
      `<select class="mc-if" title="${t("송출 NIC (localaddr)")}">${ifOpts.join("")}</select>` +
      `<button type="button" class="btn ghost sm">✕</button>` +
      `<label class="mc-fecw" title="${t("Pro-MPEG CoP3 (SMPTE 2022-1) FEC. column FEC → port+2, row FEC → port+4. L×D ≤ 100, 각 4–20")}">` +
        `<input type="checkbox" class="mc-fec"> FEC (prompeg) L <input class="mc-l" type="number" min="4" max="20" value="${esc(m.l)}">` +
        ` D <input class="mc-d" type="number" min="4" max="20" value="${esc(m.d)}"></label>`;
    $(".mc-proto", row).value = m.proto;
    $(".mc-if", row).value = m.localaddr;
    $(".mc-fec", row).checked = !!m.fec;
    const sync = () => {
      const rtp = $(".mc-proto", row).value === "rtp";
      $(".mc-fecw", row).hidden = !rtp;
      $$(".mc-l, .mc-d", row).forEach((i) => { i.disabled = !$(".mc-fec", row).checked; });
    };
    row.addEventListener("change", sync);
    sync();
    $("button", row).onclick = () => { row.remove(); this.refresh(); };
    $("#ou-list").append(row);
  },
  mcastOutput(row) {
    const mc = { proto: $(".mc-proto", row).value, group: $(".mc-group", row).value.trim(),
                 port: Number($(".mc-port", row).value) || 5000, ttl: Number($(".mc-ttl", row).value) || 16,
                 localaddr: $(".mc-if", row).value, fec: $(".mc-fec", row).checked,
                 l: Number($(".mc-l", row).value) || 10, d: Number($(".mc-d", row).value) || 10 };
    if (!mc.group) return null;
    const q = [`ttl=${mc.ttl}`];
    if (mc.localaddr) q.push(`localaddr=${mc.localaddr}`);
    if (mc.proto === "udp") {
      q.unshift("pkt_size=1316");   // 7 TS packets per datagram
      return { type: "url", url: `udp://${mc.group}:${mc.port}?${q.join("&")}`, format: "mpegts", preset: "mcast", mc };
    }
    if (mc.fec) q.push(`fec=prompeg=l=${mc.l}:d=${mc.d}`);
    return { type: "url", url: `rtp://${mc.group}:${mc.port}?${q.join("&")}`, format: "rtp_mpegts", preset: "mcast", mc };
  },
  mcastProblems() {
    const out = [];
    const rows = $$("#ou-list .url-row.mc").map((r) => this.mcastOutput(r)?.mc).filter(Boolean);
    const bad = rows.map((m) => m.group).filter((g) => !/^2(2[4-9]|3\d)\.\d+\.\d+\.\d+$/.test(g));
    if (bad.length) out.push(t("{0} 은 멀티캐스트 주소(224.0.0.0 – 239.255.255.255)가 아닙니다.", bad.join(", ")));
    rows.filter((m) => m.proto === "rtp" && m.fec).forEach((m) => {
      if (m.l < 4 || m.l > 20 || m.d < 4 || m.d > 20 || m.l * m.d > 100)
        out.push(t("FEC L={0} D={1}: L, D 는 4–20, L×D ≤ 100 이어야 합니다 (ffmpeg 가 출력을 열지 못함).", m.l, m.d));
    });
    // RTP uses port+1 (RTCP), FEC port+2 / port+4: same group with ports closer than that collide
    const span = (m) => (m.proto === "rtp" ? (m.fec ? 5 : 2) : 1);
    rows.forEach((a, i) => rows.slice(i + 1).forEach((b) => {
      if (a.group === b.group && Math.abs(a.port - b.port) < Math.max(span(a), span(b)))
        out.push(t("{0}:{1} 와 :{2} 포트가 겹칩니다 (RTP 는 port+1 RTCP, FEC 는 port+2/+4 사용).", a.group, a.port, b.port));
    }));
    return out;
  },

  /* CUDA filters exist only on the NVENC path: carry the choice over to the CPU equivalent */
  moveCudaFilters() {
    const nv = isNv($("#v-codec").value);
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
    const p = $("#v-preset").value, tune = $("#v-tune").value;
    let html;
    if (isNv(codec)) {
      const presets = {
        p1: t("가장 빠름 · 화질 최저"), p2: t("매우 빠름"), p3: t("빠름"), p4: t("중간 (기본값) · 속도/화질 균형"),
        p5: t("느림 · 화질 좋음"), p6: t("더 느림 · 화질 더 좋음"), p7: t("가장 느림 · 화질 최고"),
      };
      const tunes = {
        "": t("지정 안함 — ffmpeg 기본값 hq 와 같음"),
        hq: t("<b>high quality</b> — 화질 우선. lookahead/B-frame 사용 가능 → 지연이 생길 수 있음. 녹화·VOD·외부 송출(지연 무관)용"),
        ll: t("<b>low latency</b> — 저지연. B-frame/lookahead 없이 즉시 출력, 화질은 hq 보다 약간 낮음. 라이브 전달·모니터용 권장"),
        ull: t("<b>ultra low latency</b> — 지연 최소, 화질 가장 낮음. 같은 비트레이트에서 블록이 더 보일 수 있음"),
        lossless: t("<b>lossless</b> — 무손실. 비트레이트 매우 큼, 스트리밍 부적합"),
      };
      html = t("<b>preset {0}</b>: {1}. p1→p7 로 갈수록 화질은 좋아지고 GPU 부하가 커집니다. " +
        "프리셋 자체는 프레임 지연을 거의 늘리지 않습니다 (P2200 1080p 기준 p4~p5 권장, 여러 채널이면 p4 이하).<br>", esc(p), presets[p] || "") +
        `<b>tune ${esc(tune || "-")}</b>: ${tunes[tune] || ""}<br>` +
        t("<b>zerolatency</b>: 프레임 재정렬 지연 없이 출력 (B-frame 0 과 함께 사용). 지연을 줄이려면 tune ll/ull + zerolatency + B-frame 0.");
      if (codec === "hevc_nvenc") {
        html += t("<br><b>HEVC</b>: 같은 화질에 H.264 보다 비트레이트를 약 30~40% 줄일 수 있음. " +
          "profile <b>main10</b> 은 p010 (10-bit) 으로 변환해 인코딩. " +
          "브라우저 WebRTC 모니터·플레이어는 HEVC 재생이 제한적이고, RTMP 는 enhanced RTMP 를 지원하는 수신측만 받을 수 있음.") +
          (this.meta?.caps?.hevc_bframes ? "" : t(" 이 GPU 는 HEVC B-frame 을 지원하지 않아 B-frames 는 0 으로 고정됩니다."));
      }
    } else if (codec === "libx264") {
      html = t("<b>preset {0}</b>: ultrafast→veryslow 로 갈수록 화질↑ CPU 부하↑. 라이브는 veryfast~fast 권장.<br>" +
        "<b>tune</b>: film/animation/grain 등은 소스 특성별 화질 튜닝. <b>zerolatency</b> 는 B-frame·lookahead·sliced-thread 설정으로 지연을 최소화 (화질 약간 하락).", esc(p));
    } else {
      html = "";
    }
    $("#v-hint").innerHTML = html;
    $("#v-hint").hidden = !html;
  },

  codecLists() {
    const codec = $("#v-codec").value;
    const nv = isNv(codec);
    const p = $("#v-preset").value, tune = $("#v-tune").value;
    opts($("#v-preset"), nv ? NV_PRESETS : X264_PRESETS);
    opts($("#v-tune"), nv ? NV_TUNES : X264_TUNES);
    $("#v-preset").value = (nv ? NV_PRESETS : X264_PRESETS).includes(p) ? p : nv ? "p4" : "veryfast";
    $("#v-tune").value = (nv ? NV_TUNES : X264_TUNES).includes(t) ? t : nv ? "ll" : "";
    $$(".nv-only").forEach((e) => { e.hidden = !nv; });
    const prof = $("#v-profile"), keep = prof.value;
    const plist = PROFILES[codec] || PROFILES.h264;
    opts(prof, plist, plist.map((x) => x || "-"));
    prof.value = plist.includes(keep) ? keep : "";
  },

  /* sensible defaults for the chosen input; only for a new encoder or when the input type changes */
  applyDefaults() {
    const inType = radio("in_type");
    const caps = (this.meta && this.meta.caps) || {};
    const live = inType === "decklink" || inType === "testsrc";
    setRadio("out_type", inType === "alsa" ? "mediamtx" : live ? "mediamtx_pairs" : "url");
    $("#v-codec").value = caps.nvenc ? "h264_nvenc" : "libx264";
    this.codecLists();
    const interlaced = inType === "decklink" || (inType === "testsrc" && ($("#ts-standard").value || "1080i5994").includes("i"));
    const gpu = caps.cuda && isNv($("#v-codec").value);
    $("#f-cuda-deint").value = interlaced && gpu ? "bwdif_cuda" : "";
    $("#f-deint").value = interlaced && !gpu ? "bwdif" : "";
    $("#f-cuda-resize").value = "";
    $("#f-scale").value = "";
    $("#f-fps").value = "";
    $("#v-br").value = live ? "8M" : "6M";
    $("#v-gop").value = 30;
    $("#v-bf").value = 0;
    $("#v-zl").checked = live;
    $("#a-codec").value = live || inType === "alsa" ? "opus" : "aac";
    $("#a-br").value = "128k";
    if (inType === "alsa") {
      // audio only: one stereo opus stream on a mediamtx path
      $("#om-path").value = this.alsaPath();
      $("#a-left").value = "0";
      $("#a-right").value = Number($("#al-channels").value) > 1 ? "1" : "0";
    }
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
    if (inType === "alsa") return [{ input: 0, index: 0, channels: Number($("#al-channels").value) || 2, label: "alsa" }];
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

  /* ---- file output */
  async browse(path) {
    try {
      const d = await api("GET", `/api/helper/browse?path=${encodeURIComponent(path || "")}`);
      this.browseCur = d.path;
      this.browseParent = d.parent;
      $("#of-picker").hidden = false;
      $("#of-cur").innerHTML = `${esc(d.path)} <span class="${d.writable ? "ok" : "bad"}">${d.writable ? "" : t("(쓰기 불가)")}</span>`;
      $("#of-up").disabled = !d.parent;
      const join = (n) => (d.path.endsWith("/") ? d.path : d.path + "/") + n;
      $("#of-list").innerHTML = d.error ? `<span class="muted">${esc(d.error)}</span>`
        : d.dirs.length ? d.dirs.map((n) => `<a href="#" data-path="${esc(join(n))}">📁 ${esc(n)}</a>`).join("")
        : `<span class="muted">${t("(하위 폴더 없음)")}</span>`;
    } catch (e) { toast(e.message, true); }
  },

  async checkDir() {
    const el = $("#of-dir-state"), dir = $("#of-dir").value.trim();
    if (!dir) { el.textContent = ""; el.className = "small"; this.dirOk = null; return; }
    try {
      const d = await api("POST", "/api/helper/check_dir", { path: dir });
      if ($("#of-dir").value.trim() !== dir) return;  // changed meanwhile
      this.dirOk = d.ok;
      el.className = `small ${d.ok ? "ok" : "bad"}`;
      el.textContent = d.ok ? t("✓ 쓰기 가능 · 여유 {0}", fmt.bytes(d.free_bytes)) : t("✗ 쓰기 불가 — {0}", d.error);
    } catch (e) { el.className = "small bad"; el.textContent = e.message; }
    if (radio("out_type") === "file") this.refresh();
  },

  /* base filename: typed value, else the encoder title, else the input name */
  fileBase(spec) {
    return safeName($("#of-base").value || $("#e-title").value || this.autoTitle(spec).split(" → ")[0]);
  },

  filePreview() {
    const seg = Number(this.meta?.recording?.segment_sec) || 3600;
    const ext = $("#of-ext").value;
    const d = new Date(), p2 = (n) => String(n).padStart(2, "0");
    const ts = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}-${p2(d.getHours())}-${p2(d.getMinutes())}-${p2(d.getSeconds())}`;
    const dir = ($("#of-dir").value.trim() || t("<저장 폴더>")).replace(/\/+$/, "");
    const segTxt = seg % 3600 === 0 ? t("{0}시간", seg / 3600) : seg % 60 === 0 ? t("{0}분", seg / 60) : t("{0}초", seg);
    $("#of-preview").innerHTML = `${esc(dir)}/${esc(this.fileBase(this.getSpec()))}_${ts}.${ext}<br>` +
      `<span class="muted">` + t("segment {0} ({1}s, config recording.segment_sec) 마다 새 파일 · 파일명 시각 = 파일 시작 시각", segTxt, seg) +
      (ext === "mp4" ? t(" · mp4 는 +faststart 적용 (segment 종료시 moov 를 앞으로 이동). 비정상 종료시 기록중이던 mp4 는 재생 불가 — 안정성은 ts 가 유리") : "") + "</span>";
  },

  /* index (in mxPaths order) of the first path that carries a video track */
  mxVideoInput() {
    const i = this.mxPaths().findIndex((p) => {
      if (this.probed && this.probed.path === p) return this.probed.streams.some((s) => s.type === "video");
      const info = (this.paths || []).find((x) => x.name === p);
      return info && info.tracks.some((t) => VIDEO_CODECS.includes(t.codec));
    });
    return Math.max(0, i);
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
    if (total < 2) box.innerHTML = `<span class="muted small">${t("스테레오 쌍을 만들 채널이 부족합니다")}</span>`;
  },

  getSpec() {
    const inType = radio("in_type");
    const nv = isNv($("#v-codec").value);
    const cpuOk = !(nv && this.meta?.caps?.cuda);   // CPU filter row is hidden on the NVENC+CUDA path
    const outType = radio("out_type");
    const spec = {
      input: {
        type: inType,
        nvdec: ["mediamtx", "custom"].includes(inType) && !$("#in-nvdec").disabled && $("#in-nvdec").checked,
        decklink: {
          device: $("#dl-device").value, format_code: $("#dl-format").value, channels: Number($("#dl-channels").value),
          raw_format: $("#dl-raw").value, video_input: $("#dl-vin").value,
        },
        mediamtx: { paths: this.mxPaths(), video_input: this.mxVideoInput(), transport: $("#mx-transport").value, interlaced: $("#mx-interlaced").checked },
        testsrc: {
          source: $("#ts-source").value, standard: $("#ts-standard").value, size: $("#ts-size").value,
          rate: $("#ts-rate").value, interlaced: $("#ts-inter").checked,
          audio_channels: Number($("#ts-ach").value), sine_freq: Number($("#ts-freq").value),
        },
        custom: { args: $("#cu-args").value, layout: $("#cu-layout").value },
        alsa: { device: $("#al-device").value, channels: Number($("#al-channels").value), sample_rate: Number($("#al-sr").value) },
      },
      audio_layout: this.layout,
      sink: { type: outType, base: $("#op-base").value, pairs: $$("#op-pairs input:checked").map((i) => Number(i.value)),
              ...(outType.startsWith("mediamtx") ? { rtsp_transport: $("#out-transport").value } : {}) },
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
    if (outType === "url") spec.outputs = $$("#ou-list .url-row").map((r) => r.classList.contains("mc")
      ? this.mcastOutput(r)
      : { type: "url", url: $(".ou-url", r).value, format: $(".ou-fmt", r).value }).filter((o) => o && o.url);
    if (outType === "null") spec.outputs = [{ type: "null" }];
    if (outType === "file") spec.outputs = [{ type: "file", ext: $("#of-ext").value, dir: $("#of-dir").value.trim(), base: this.fileBase(spec) }];
    return spec;
  },

  setSpec(s) {
    const i = s.input || {};
    setRadio("in_type", i.type || "decklink");
    $("#in-nvdec").checked = !!i.nvdec;
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
    const al = i.alsa || {};
    if (al.device) {
      if (![...$("#al-device").options].some((o) => o.value === al.device)) {
        $("#al-device").insertAdjacentHTML("beforeend", `<option value="${esc(al.device)}">${esc(al.device)} ${t("(없음)")}</option>`);
      }
      $("#al-device").value = al.device;
    }
    $("#al-channels").value = String(al.channels || 2);
    $("#al-sr").value = String(al.sample_rate || 48000);
    const ts = i.testsrc || {};
    if (ts.source) $("#ts-source").value = ts.source;
    if (ts.standard) $("#ts-standard").value = ts.standard;
    $("#ts-custom").hidden = ts.standard !== "custom";
    $("#ts-size").value = ts.size || "1920x1080";
    $("#ts-rate").value = ts.rate || "30000/1001";
    $("#ts-inter").checked = !!ts.interlaced;
    $("#ts-ach").value = ts.audio_channels || 8;
    $("#ts-freq").value = ts.sine_freq || 1000;
    const c = i.custom || {};
    $("#cu-args").value = c.args || "";
    $("#cu-layout").value = c.layout || "2";

    const sk = s.sink || {};
    setRadio("out_type", sk.type || "mediamtx_pairs");
    $("#out-transport").value = sk.rtsp_transport || this.meta.rtsp_transport || "udp";
    $("#op-base").value = sk.base || "sdi1";
    $("#ou-list").innerHTML = "";
    (s.outputs || []).forEach((o) => {
      if (o.type === "url" && ["mcast", "udp_mcast"].includes(o.preset)) this.addMcast(o.mc);
      else if (o.type === "url") this.addUrl(o.url, o.format || "auto");
      if (o.type === "mediamtx") $("#om-path").value = o.path;
      if (o.type === "file") {
        $("#of-ext").value = o.ext === "mp4" ? "mp4" : "ts";
        $("#of-dir").value = o.dir || "";
        $("#of-base").value = o.base || "";
      }
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
    // ALSA is audio only: no video encoder
    $("#v-codec").disabled = inType === "alsa";
    if (inType === "alsa") {
      $("#v-codec").value = "none";
      const d = this.alsaDev();
      $("#al-note").textContent = this.alsa?.alsa === false
        ? t("이 ffmpeg 는 ALSA 입력(-f alsa)을 지원하지 않습니다 — libasound2-dev 설치 후 ffmpeg 재빌드가 필요합니다.")
        : d?.busy ? t("이 장치는 다른 프로세스가 사용중입니다 — 인코더가 시작되지 않을 수 있습니다.") : "";
      $("#al-mixer").disabled = !d;
    }
    // NVDEC only for compressed inputs, and only when the video is decoded at all
    const nvd = $("#in-nvdec");
    const nvdecInput = ["mediamtx", "custom"].includes(inType);
    $("#nvdec-row").hidden = !nvdecInput;
    const nvdecOk = this.meta?.caps?.nvdec ?? this.meta?.caps?.cuda;
    nvd.disabled = !nvdecOk || ["copy", "none"].includes($("#v-codec").value);
    $("#nvdec-note").textContent = !nvdecOk ? t("NVDEC 사용 불가")
      : ["copy", "none"].includes($("#v-codec").value) ? t("video copy/none 에서는 디코딩하지 않음")
      : nvd.checked && !isNv($("#v-codec").value) ? t("libx264 → GPU 디코딩 후 hwdownload")
      : "";
    $("#out-transport-row").hidden = !outType.startsWith("mediamtx");
    const codec = $("#v-codec").value;
    $(".v-enc").hidden = codec === "copy" || codec === "none";
    const bf = $("#v-bf");
    const noBf = codec === "hevc_nvenc" && !this.meta?.caps?.hevc_bframes;
    if (noBf) bf.value = "0";
    bf.disabled = noBf;
    bf.title = noBf ? t("이 GPU 의 hevc_nvenc 는 B-frame 을 지원하지 않습니다 (Turing 이상 필요)") : "";
    // NVENC path: CUDA filters replace the CPU ones (CPU row only as a fallback when CUDA is unusable)
    const gpuFilters = isNv(codec) && !!this.meta?.caps?.cuda;
    $$(".cuda-only").forEach((e) => { e.hidden = !isNv(codec); });
    $$(".cpu-only").forEach((e) => { e.hidden = gpuFilters; });
    // DeckLink progressive standard -> nothing to deinterlace
    const progressive = inType === "decklink" && this.dlInterlaced() === false;
    ["#f-cuda-deint", "#f-deint"].forEach((sel) => {
      const el = $(sel);
      if (progressive) el.value = "";
      else if (this.dlProgressive && el.disabled && inType === "decklink" && !el.closest("[hidden]"))
        el.value = sel === "#f-deint" ? "bwdif" : "bwdif_cuda";  // back to interlaced: restore the default
      el.disabled = progressive;
      el.title = progressive ? t("progressive 비디오 표준에서는 deinterlace 를 사용하지 않습니다") : "";
    });
    this.dlProgressive = progressive;
    // deint output rate only means something when a (visible) deinterlacer is chosen
    const cudaDeint = isNv(codec) && $("#f-cuda-deint").value;
    const cpuDeint = !gpuFilters && $("#f-deint").value;
    $("#f-deint-rate").disabled = !(cudaDeint || cpuDeint);
    this.hint();
    this.renderChannels();
    const pairs = outType === "mediamtx_pairs";
    $(".a-lr").hidden = pairs || $("#a-codec").value === "copy";
    $(".a-ln").hidden = ["copy", "none"].includes($("#a-codec").value);
    $$(".a-ln input[type=number]").forEach((i) => { i.disabled = !$("#a-ln").checked; });
    const notes = [];
    if (outType === "url") {
      if ($$("#ou-list .url-row.mc").length && $("#a-codec").value === "opus") notes.push(t("MPEG-TS 수신기(IRD, VLC 외 다수)는 Opus 를 지원하지 않는 경우가 많습니다 — AAC 권장."));
      notes.push(...this.mcastProblems());
    }
    if (outType === "file") {
      this.filePreview();
      $("#of-base").placeholder = this.fileBase(this.getSpec());
      if (!$("#of-dir").value.trim()) notes.push(t("저장 폴더를 선택하세요."));
      else if (this.dirOk === false) notes.push(t("저장 폴더에 쓸 수 없습니다 — 인코더가 시작되지 않습니다."));
    }
    if (pairs) notes.push(t("각 스테레오 쌍이 개별 mediamtx path 로 전송됩니다 (L/R 선택 불필요)."));
    const toMtx = outType.startsWith("mediamtx");
    if (toMtx && ["aac", "mp3"].includes($("#a-codec").value)) notes.push(t("WebRTC 모니터(브라우저)는 AAC/MP3 를 재생하지 못합니다 — 모니터할 path 는 opus 권장."));
    if (toMtx && Number($("#v-bf").value) > 0 && codec !== "copy") notes.push(t("WebRTC 재생에는 B-frame 0 을 권장합니다."));
    if (inType === "decklink" && !$("#f-cuda-deint").value && !$("#f-deint").value && codec !== "copy" && this.dlInterlaced())
      notes.push(t("interlaced 소스입니다 — deinterlace 필터를 고려하세요."));
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
    if (!body.cli.trim()) { toast(t("command line 이 비어있습니다"), true); return; }
    if (radio("out_type") === "file" && !locked && !$("#of-dir").value.trim()) { toast(t("저장 폴더를 선택하세요"), true); return; }
    try {
      if (this.editing) {
        await api("PATCH", `/api/encoders/${this.editing.id}`, body);
        const card = App.encoders.get(this.editing.id);
        if (card) { card.setDirty(false); card.cli.value = body.cli; }
        toast(locked ? t("저장됨 (직접 편집한 CLI)") : t("저장됨 — 다음 시작/재시작부터 적용"));
      } else {
        await api("POST", "/api/encoders", body);
        toast("인코더 추가됨");
      }
      $("#dlg-encoder").close();
    } catch (e) { toast(e.message, true); }
  },

  autoTitle(spec) {
    const i = spec.input;
    const src = i.type === "decklink" ? i.decklink.device : i.type === "mediamtx" ? (i.mediamtx.paths[0] || "mediamtx") : i.type === "testsrc" ? i.testsrc.source : i.type === "alsa" ? (i.alsa.device || "alsa") : "custom";
    const dst = spec.sink.type === "mediamtx_pairs" ? `${spec.sink.base}_*` : spec.sink.type === "mediamtx" ? spec.outputs[0]?.path : spec.sink.type === "url" ? (spec.outputs[0]?.url || "").split("/")[2] || "url"
      : spec.sink.type === "file" ? t("{0} 녹화", spec.outputs[0]?.ext || "file") : "null";
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
    $("#mon-dlg-title").textContent = cfg ? t("Monitor 편집 — {0}", cfg.title) : t("Monitor 추가");
    $("#m-title").value = cfg ? cfg.title : "";
    $("#dlg-monitor").showModal();
    await this.load();
  },

  async load() {
    let d = { paths: [] };
    try { d = await api("GET", "/api/helper/paths"); } catch (_) { /* offline */ }
    this.paths = d.paths || [];
    if (!d.ok) toast(t("mediamtx API 에 연결할 수 없습니다: ") + (d.error || ""), true);
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
    box.innerHTML = all.length ? "" : `<div class="muted small">${t("활성화된 path 가 없습니다. 아래에 직접 입력할 수 있습니다.")}</div>`;
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

/* ------------------------------------------------------------ ALSA recording mixer */
const MixerDialog = {
  card: null,
  timers: {},

  async open(card) {
    const dlg = $("#dlg-mixer");
    if (!this.ready) {
      this.ready = true;
      $("#mix-card").addEventListener("change", () => { this.card = $("#mix-card").value; this.load(); });
      $("#mix-refresh").addEventListener("click", () => this.load());
      $("#mix-list").addEventListener("input", (e) => this.onInput(e, false));
      $("#mix-list").addEventListener("change", (e) => this.onInput(e, true));
    }
    // every card that has a capture device (several ALSA cards are fine)
    const cards = [...new Map((EncoderHelper.alsa?.devices || []).map((d) => [d.card, d.card_name])).entries()];
    opts($("#mix-card"), cards.map(([c]) => c), cards.map(([c, n]) => `${c} — ${n}`));
    this.card = card || cards[0]?.[0] || "";
    $("#mix-card").value = this.card;
    dlg.showModal();
    await this.load();
  },

  async load() {
    const box = $("#mix-list");
    if (!this.card) { box.innerHTML = `<div class="muted small">${t("(ALSA 캡처 장치 없음)")}</div>`; return; }
    box.innerHTML = `<div class="muted small">${t("불러오는 중…")}</div>`;
    let d;
    try { d = await api("GET", `/api/helper/alsa/mixer?card=${encodeURIComponent(this.card)}`); } catch (e) { d = { controls: [], error: e.message }; }
    if (d.error) { box.innerHTML = `<div class="bad small">${esc(d.error)}</div>`; return; }
    if (!d.controls.length) { box.innerHTML = `<div class="muted small">${t("이 카드에는 녹음 관련 믹서 컨트롤이 없습니다.")}</div>`; return; }
    box.innerHTML = d.controls.map((c) => `<div class="mix-row" data-id="${esc(c.id)}"></div>`).join("");
    d.controls.forEach((c) => this.renderRow(c));
  },

  renderRow(c) {
    const row = $(`.mix-row[data-id="${CSS.escape(c.id)}"]`, $("#mix-list"));
    if (!row) return;
    const ch = c.channels[0] || {};
    const sw = c.channels.length ? c.channels.every((x) => x.on !== false) : false;
    const name = c.index ? `${c.name} ${c.index}` : c.name;
    let html = `<span class="mix-name" title="${esc(c.id)}">${esc(name)}</span>`;
    if (c.volume && c.max != null) {
      html += `<input type="range" class="mix-vol" min="${c.min}" max="${c.max}" step="1" value="${ch.value ?? c.min}">` +
        `<span class="mix-val">${this.valText(c)}</span>`;
    } else {
      html += `<span class="mix-fill"></span>`;
    }
    if (c.switch) html += `<label class="mix-sw-l"><input type="checkbox" class="mix-sw"${sw ? " checked" : ""}> ${t("캡처")}</label>`;
    if (c.enum) {
      html += `<select class="mix-enum">${c.items.map((it) => `<option${it === c.item ? " selected" : ""}>${esc(it)}</option>`).join("")}</select>`;
    }
    row.innerHTML = html;
  },

  valText(c) {
    const ch = c.channels[0] || {};
    const parts = [`${ch.value ?? "-"}/${c.max}`];
    if (ch.percent != null) parts.push(`${ch.percent}%`);
    if (ch.db != null) parts.push(`${ch.db > 0 ? "+" : ""}${ch.db.toFixed(1)} dB`);
    return parts.join(" · ");
  },

  onInput(e, final) {
    const row = e.target.closest(".mix-row");
    if (!row) return;
    const id = row.dataset.id;
    let kind, value;
    if (e.target.classList.contains("mix-vol")) { kind = "volume"; value = Number(e.target.value); }
    else if (e.target.classList.contains("mix-sw")) { if (!final) return; kind = "switch"; value = e.target.checked; }
    else if (e.target.classList.contains("mix-enum")) { if (!final) return; kind = "enum"; value = e.target.value; }
    else return;
    // slider: follow the drag at most every 120 ms, always send the final value
    clearTimeout(this.timers[id]);
    this.timers[id] = setTimeout(() => this.send(id, kind, value, final), final ? 0 : 120);
  },

  async send(id, kind, value, final) {
    try {
      const c = await api("POST", "/api/helper/alsa/mixer", { card: this.card, control: id, kind, value });
      const row = $(`.mix-row[data-id="${CSS.escape(id)}"]`, $("#mix-list"));
      // while dragging only update the readout, so the slider under the pointer isn't re-created
      if (kind === "volume" && !final) { const v = row && $(".mix-val", row); if (v) v.textContent = this.valText(c); }
      else this.renderRow(c);
    } catch (e) { toast(e.message, true); }
  },
};

/* language switch: rebuild the helper texts that are generated in JS */
document.addEventListener("langchange", () => {
  if ($("#dlg-encoder").open) {
    const cfg = EncoderHelper.editing;
    $("#helper-title").textContent = cfg ? t("Encoder 편집 — {0}", cfg.title) : t("Encoder 추가");
    EncoderHelper.refresh();
  }
  if ($("#dlg-mixer").open) MixerDialog.load();
  if ($("#dlg-monitor").open) {
    const cfg = MonitorHelper.editing;
    $("#mon-dlg-title").textContent = cfg ? t("Monitor 편집 — {0}", cfg.title) : t("Monitor 추가");
    MonitorHelper.render();
  }
});
