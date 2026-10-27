"use strict";
/* UI language (ko / en). Source strings are Korean; EN maps them to English.
   t("… {0} …", x) for strings built in JS, I18N.apply() for static HTML text/title/placeholder. */

const EN = {
  // ---- index.html
  "오디오 레벨미터를 사용하려면 화면을 한 번 클릭하세요": "Click anywhere to enable the audio level meters",
  "장치": "Device",
  "온도": "Temp",
  "전력": "Power",
  "최근 구간 처리속도가 실시간보다 느림": "Recent processing speed is slower than real time",
  "백엔드 시작시 자동실행": "Start with backend",
  "오류시 자동재시작": "Restart on error",
  "config.json encoder_watchdog.speed_min / speed_window_sec 기준": "Uses config.json encoder_watchdog.speed_min / speed_window_sec",
  "속도저하시 재시작": "Restart when slow",
  "예약 재시작": "Scheduled restart",
  "매일": "Daily",
  "주기(분)": "Every (min)",
  "● 수정됨": "● modified",
  "복사": "Copy",
  "붙여넣기 (클립보드 내용으로 교체)": "Paste (replace with clipboard)",
  "저장": "Save",
  "저장 + 재시작": "Save + restart",
  "되돌리기": "Revert",
  "저장된 CLI 는 다음 시작/재시작부터 사용됩니다. -progress, -nostats 등은 실행시 자동 추가됩니다.":
    "The saved CLI is used from the next start/restart. -progress, -nostats etc. are added automatically.",
  "실행중인 실제 argv": "Actual running argv",
  "로그 삭제": "Clear log",
  "표시된 로그 복사": "Copy shown log",
  "끄면 WebRTC 연결과 레벨미터를 중지합니다": "Off stops the WebRTC connection and level meters",
  "입력 (input)": "Input",
  "카드에서 조회": "Query card",
  "새로고침": "Refresh",
  "추가 오디오 입력 path": "Extra audio input paths",
  "소스가 interlaced": "Source is interlaced",
  "ffprobe 로 스트림 확인": "Probe streams with ffprobe",
  "채널마다 3 dB 씩 낮은 1 kHz 톤 (ch1 -6 dBFS) - 레벨미터로 라우팅 확인용":
    "1 kHz tone, 3 dB lower per channel (ch1 -6 dBFS) - for checking routing on the level meters",
  "오디오 스트림별 채널수 (예: 2,2)": "Channels per audio stream (e.g. 2,2)",
  "입력 비디오(H.264/HEVC 등)를 GPU(NVDEC)로 디코딩. 프레임이 GPU 메모리에 남아 CUDA 필터/NVENC 까지 복사 없이 처리됩니다. CPU 필터나 libx264 를 쓰면 자동으로 hwdownload 합니다.":
    "Decode the input video (H.264/HEVC etc.) on the GPU (NVDEC). Frames stay in GPU memory through CUDA filters/NVENC without copies. With CPU filters or libx264, hwdownload is added automatically.",
  "NVDEC (GPU 디코딩)": "NVDEC (GPU decoding)",
  "출력 (output)": "Output",
  "외부 URL": "External URL",
  "파일 (녹화)": "File (recording)",
  "mediamtx 로 publish 할 때의 RTSP 전송방식. UDP 는 지연이 적지만 손실 가능, TCP 는 손실 없음(재전송) / 지연·버퍼 증가 가능":
    "RTSP transport used to publish to mediamtx. UDP: lower latency but may lose packets. TCP: lossless (retransmit), may add latency/buffering",
  "RTSP 전송": "RTSP transport",
  "video + 첫 스테레오 쌍 → <base>_va12, 나머지 쌍은 오디오 전용 path":
    "video + first stereo pair → <base>_va12, the other pairs go to audio-only paths",
  "프리셋: MPEG-TS 멀티캐스트 — 기본 RTP (rtp_mpegts), UDP 선택 가능, Pro-MPEG FEC":
    "Preset: MPEG-TS multicast — RTP (rtp_mpegts) by default, UDP selectable, Pro-MPEG FEC",
  "+ 멀티캐스트 (MPEG-TS)": "+ Multicast (MPEG-TS)",
  "2개 이상이면 tee muxer (onfail=ignore) 로 한번 인코딩해 여러곳에 전송":
    "With 2 or more, encode once and send to all via the tee muxer (onfail=ignore)",
  "확장자": "Extension",
  "저장 폴더": "Folder",
  "찾아보기…": "Browse…",
  "상위 폴더": "Parent folder",
  "이 폴더 선택": "Select this folder",
  "닫기": "Close",
  "deint 출력": "deint output",
  "I: 목표 통합 라우드니스 (EBU R128 -23, ATSC A/85 -24, 스트리밍 -14~-16). LRA: 허용 라우드니스 범위 (작을수록 압축). TP: true peak 상한. 1-pass 실시간 모드라 약 3초 lookahead 지연이 추가되고, 모든 오디오 출력(pair path 포함)에 적용됩니다.":
    "I: target integrated loudness (EBU R128 -23, ATSC A/85 -24, streaming -14~-16). LRA: allowed loudness range (smaller = more compression). TP: true peak ceiling. Real-time 1-pass mode adds about 3 s lookahead delay and applies to every audio output (pair paths included).",
  "Generated command line (직접 편집 가능)": "Generated command line (editable)",
  "직접 편집한 CLI 유지 (helper 변경을 반영하지 않음)": "Keep the hand-edited CLI (ignore helper changes)",
  "취소": "Cancel",
  "추가": "Add",
  "빠른 선택:": "Quick select:",
  "선택 순서대로 표시됩니다. 비디오 트랙이 있는 첫 path 가 비디오 창으로 쓰입니다.":
    "Shown in selection order. The first path with a video track is used for the video window.",
  "직접 입력 (쉼표 구분)": "Manual entry (comma separated)",

  // ---- meter.js
  "{0} — 클릭하면 이 path 의 오디오를 모니터링(청취)": "{0} — click to listen to this path's audio",
  "path 없음": "no paths",

  // ---- app.js
  "복사 실패": "Copy failed",
  "여기에 놓기 — {0}": "Drop here — {0}",
  "CLI 복사됨": "CLI copied",
  "http 접속에서는 브라우저가 붙여넣기 버튼을 막습니다 — 전체 선택했으니 Ctrl+V 로 붙여넣으세요":
    "Over http the browser blocks the paste button — the text is selected, press Ctrl+V to paste",
  "붙여넣음 — 저장해야 적용됩니다": "Pasted — save to apply",
  "fflog 복사됨": "fflog copied",
  "인코더 \"{0}\" 를 삭제할까요? (실행중이면 정지됩니다)": "Delete encoder \"{0}\"? (it will be stopped if running)",
  "CLI 저장됨": "CLI saved",
  "ffmpeg 가 이 출력(rtsp/tee)의 bitrate 를 제공하지 않아 mediamtx 수신량으로 표시: {0}":
    "ffmpeg reports no bitrate for this output (rtsp/tee); showing what mediamtx receives: {0}",
  "ffmpeg 가 이 출력 형식(rtsp/tee 등)의 bitrate 를 제공하지 않습니다": "ffmpeg reports no bitrate for this output format (rtsp/tee etc.)",
  "최근 구간 처리속도 {0}x (실시간 미달) — {1} 째": "Recent speed {0}x (below real time) — for {1}",
  "{0}  ({1} 전)": "{0}  ({1} ago)",
  "{0} (backup 포함 {1})": "{0} ({1} incl. backups)",
  "모니터 \"{0}\" 를 삭제할까요?": "Delete monitor \"{0}\"?",
  "인코더가 없습니다. + Encoder 로 추가하세요.": "No encoders. Add one with + Encoder.",
  "모니터가 없습니다. + Monitor 로 mediamtx path 를 추가하세요.": "No monitors. Add mediamtx paths with + Monitor.",
  "CPU 사용률": "CPU usage",
  "ffmpeg 프로세스": "ffmpeg processes",
  "RAM 전체": "RAM total",
  "시스템 시간": "System time",
  "mediamtx.yml 의 api: true 및 config.json mediamtx.api_url 을 확인하세요.": "Check api: true in mediamtx.yml and mediamtx.api_url in config.json.",
  "활성화된 path 가 없습니다.": "No active paths.",
  "↳ reader 없음": "↳ no readers",
  "WebRTC 로 새 창에서 보기 (mediamtx player)": "Open in a new window via WebRTC (mediamtx player)",
  "mediamtx → readers (합계)": "mediamtx → readers (total)",
  "확인": "OK",

  // ---- helper.js
  "Encoder 편집 — {0}": "Edit encoder — {0}",
  "Encoder 추가": "Add encoder",
  "NVENC 사용 불가 ({0})": "NVENC unavailable ({0})",
  "HEVC NVENC 사용 불가 ({0})": "HEVC NVENC unavailable ({0})",
  "hevc_nvenc (사용 불가)": "hevc_nvenc (unavailable)",
  "CUDA filter 사용 불가 ({0})": "CUDA filter unavailable ({0})",
  "NVDEC 사용 불가 ({0})": "NVDEC unavailable ({0})",
  "이 시스템에서 {0} — libx264 / CPU filter 를 사용하세요.": "On this system: {0} — use libx264 / CPU filters.",
  "(DeckLink 장치 없음)": "(no DeckLink devices)",
  "(활성 path 없음 — mediamtx API 확인)": "(no active paths — check the mediamtx API)",
  "(활성 path 없음)": "(no active paths)",
  "조회중… (사용중인 장치는 조회 불가)": "Querying… (a device in use can't be queried)",
  "조회 실패: ": "Query failed: ",
  "RTP: -f rtp_mpegts rtp://… (RTP 시퀀스로 손실 확인, Pro-MPEG FEC 가능) / UDP: -f mpegts udp://… (pkt_size 1316)":
    "RTP: -f rtp_mpegts rtp://… (loss visible via RTP sequence, Pro-MPEG FEC possible) / UDP: -f mpegts udp://… (pkt_size 1316)",
  "송출 NIC (localaddr)": "Sending NIC (localaddr)",
  "Pro-MPEG CoP3 (SMPTE 2022-1) FEC. column FEC → port+2, row FEC → port+4. L×D ≤ 100, 각 4–20":
    "Pro-MPEG CoP3 (SMPTE 2022-1) FEC. column FEC → port+2, row FEC → port+4. L×D ≤ 100, each 4–20",
  "{0} 은 멀티캐스트 주소(224.0.0.0 – 239.255.255.255)가 아닙니다.": "{0}: not a multicast address (224.0.0.0 – 239.255.255.255).",
  "FEC L={0} D={1}: L, D 는 4–20, L×D ≤ 100 이어야 합니다 (ffmpeg 가 출력을 열지 못함).":
    "FEC L={0} D={1}: L and D must be 4–20 with L×D ≤ 100 (ffmpeg can't open the output).",
  "{0}:{1} 와 :{2} 포트가 겹칩니다 (RTP 는 port+1 RTCP, FEC 는 port+2/+4 사용).":
    "{0}:{1} and :{2} ports overlap (RTP uses port+1 for RTCP, FEC uses port+2/+4).",
  "가장 빠름 · 화질 최저": "fastest · lowest quality",
  "매우 빠름": "very fast",
  "빠름": "fast",
  "중간 (기본값) · 속도/화질 균형": "medium (default) · speed/quality balance",
  "느림 · 화질 좋음": "slow · good quality",
  "더 느림 · 화질 더 좋음": "slower · better quality",
  "가장 느림 · 화질 최고": "slowest · best quality",
  "지정 안함 — ffmpeg 기본값 hq 와 같음": "not set — same as the ffmpeg default hq",
  "<b>high quality</b> — 화질 우선. lookahead/B-frame 사용 가능 → 지연이 생길 수 있음. 녹화·VOD·외부 송출(지연 무관)용":
    "<b>high quality</b> — quality first. May use lookahead/B-frames → can add latency. For recording, VOD, external delivery (latency not critical)",
  "<b>low latency</b> — 저지연. B-frame/lookahead 없이 즉시 출력, 화질은 hq 보다 약간 낮음. 라이브 전달·모니터용 권장":
    "<b>low latency</b> — immediate output without B-frames/lookahead, quality slightly below hq. Recommended for live contribution and monitoring",
  "<b>ultra low latency</b> — 지연 최소, 화질 가장 낮음. 같은 비트레이트에서 블록이 더 보일 수 있음":
    "<b>ultra low latency</b> — minimum latency, lowest quality. More blocking at the same bitrate",
  "<b>lossless</b> — 무손실. 비트레이트 매우 큼, 스트리밍 부적합": "<b>lossless</b> — very high bitrate, not for streaming",
  "<b>preset {0}</b>: {1}. p1→p7 로 갈수록 화질은 좋아지고 GPU 부하가 커집니다. 프리셋 자체는 프레임 지연을 거의 늘리지 않습니다 (P2200 1080p 기준 p4~p5 권장, 여러 채널이면 p4 이하).<br>":
    "<b>preset {0}</b>: {1}. From p1 to p7 quality improves and GPU load rises. The preset itself adds almost no frame latency (P2200 1080p: p4~p5 recommended, p4 or lower for many channels).<br>",
  "<b>zerolatency</b>: 프레임 재정렬 지연 없이 출력 (B-frame 0 과 함께 사용). 지연을 줄이려면 tune ll/ull + zerolatency + B-frame 0.":
    "<b>zerolatency</b>: output without frame reordering delay (use with B-frames 0). For low latency: tune ll/ull + zerolatency + B-frames 0.",
  "<br><b>HEVC</b>: 같은 화질에 H.264 보다 비트레이트를 약 30~40% 줄일 수 있음. profile <b>main10</b> 은 p010 (10-bit) 으로 변환해 인코딩. 브라우저 WebRTC 모니터·플레이어는 HEVC 재생이 제한적이고, RTMP 는 enhanced RTMP 를 지원하는 수신측만 받을 수 있음.":
    "<br><b>HEVC</b>: about 30~40% less bitrate than H.264 at the same quality. Profile <b>main10</b> converts to p010 (10-bit) before encoding. Browser WebRTC monitors/players have limited HEVC playback, and over RTMP only receivers with enhanced RTMP can take it.",
  " 이 GPU 는 HEVC B-frame 을 지원하지 않아 B-frames 는 0 으로 고정됩니다.": " This GPU has no HEVC B-frame support, so B-frames is fixed at 0.",
  "<b>preset {0}</b>: ultrafast→veryslow 로 갈수록 화질↑ CPU 부하↑. 라이브는 veryfast~fast 권장.<br><b>tune</b>: film/animation/grain 등은 소스 특성별 화질 튜닝. <b>zerolatency</b> 는 B-frame·lookahead·sliced-thread 설정으로 지연을 최소화 (화질 약간 하락).":
    "<b>preset {0}</b>: from ultrafast to veryslow, quality↑ CPU load↑. veryfast~fast recommended for live.<br><b>tune</b>: film/animation/grain etc. tune quality for the source. <b>zerolatency</b> minimizes latency via B-frame/lookahead/sliced-thread settings (slightly lower quality).",
  "(쓰기 불가)": "(not writable)",
  "(하위 폴더 없음)": "(no subfolders)",
  "✓ 쓰기 가능 · 여유 {0}": "✓ writable · {0} free",
  "✗ 쓰기 불가 — {0}": "✗ not writable — {0}",
  "<저장 폴더>": "<folder>",
  "{0}시간": "{0} h",
  "{0}분": "{0} min",
  "{0}초": "{0} s",
  "segment {0} ({1}s, config recording.segment_sec) 마다 새 파일 · 파일명 시각 = 파일 시작 시각":
    "a new file every segment of {0} ({1}s, config recording.segment_sec) · the time in the name = file start time",
  " · mp4 는 +faststart 적용 (segment 종료시 moov 를 앞으로 이동). 비정상 종료시 기록중이던 mp4 는 재생 불가 — 안정성은 ts 가 유리":
    " · mp4 uses +faststart (moov moved to the front when a segment ends). After an abnormal exit the mp4 being written is unplayable — ts is more robust",
  "스테레오 쌍을 만들 채널이 부족합니다": "Not enough channels for a stereo pair",
  "NVDEC 사용 불가": "NVDEC unavailable",
  "video copy/none 에서는 디코딩하지 않음": "no decoding with video copy/none",
  "libx264 → GPU 디코딩 후 hwdownload": "libx264 → GPU decode, then hwdownload",
  "이 GPU 의 hevc_nvenc 는 B-frame 을 지원하지 않습니다 (Turing 이상 필요)": "hevc_nvenc on this GPU has no B-frame support (needs Turing or newer)",
  "progressive 비디오 표준에서는 deinterlace 를 사용하지 않습니다": "No deinterlacing with a progressive video standard",
  "MPEG-TS 수신기(IRD, VLC 외 다수)는 Opus 를 지원하지 않는 경우가 많습니다 — AAC 권장.":
    "Many MPEG-TS receivers (IRDs, and most besides VLC) don't support Opus — AAC recommended.",
  "저장 폴더를 선택하세요.": "Select a folder.",
  "저장 폴더에 쓸 수 없습니다 — 인코더가 시작되지 않습니다.": "The folder is not writable — the encoder won't start.",
  "각 스테레오 쌍이 개별 mediamtx path 로 전송됩니다 (L/R 선택 불필요).": "Each stereo pair is sent to its own mediamtx path (no L/R selection needed).",
  "WebRTC 모니터(브라우저)는 AAC/MP3 를 재생하지 못합니다 — 모니터할 path 는 opus 권장.":
    "WebRTC monitors (browser) can't play AAC/MP3 — use opus for paths you want to monitor.",
  "WebRTC 재생에는 B-frame 0 을 권장합니다.": "B-frames 0 is recommended for WebRTC playback.",
  "interlaced 소스입니다 — deinterlace 필터를 고려하세요.": "Interlaced source — consider a deinterlace filter.",
  "command line 이 비어있습니다": "The command line is empty",
  "저장 폴더를 선택하세요": "Select a folder",
  "저장됨 (직접 편집한 CLI)": "Saved (hand-edited CLI)",
  "저장됨 — 다음 시작/재시작부터 적용": "Saved — applies from the next start/restart",
  "인코더 추가됨": "Encoder added",
  "{0} 녹화": "{0} recording",
  "Monitor 편집 — {0}": "Edit monitor — {0}",
  "Monitor 추가": "Add monitor",
  "mediamtx API 에 연결할 수 없습니다: ": "Can't connect to the mediamtx API: ",
  "활성화된 path 가 없습니다. 아래에 직접 입력할 수 있습니다.": "No active paths. You can type them below.",
};

const I18N = {
  lang: (() => { try { return localStorage.getItem("mr_lang") || "ko"; } catch (_) { return "ko"; } })(),
  // node -> {orig, set}: original Korean text and the value we last wrote (to notice later JS changes)
  texts: new WeakMap(),
  attrs: new WeakMap(),

  set(lang) {
    this.lang = lang;
    try { localStorage.setItem("mr_lang", lang); } catch (_) { /* private mode */ }
    this.apply();
    document.dispatchEvent(new Event("langchange"));
  },

  // translate (or restore) one value; rec remembers the original across switches
  conv(cur, rec) {
    const orig = rec && rec.set === cur ? rec.orig : cur;
    const key = orig.replace(/\s+/g, " ").trim();
    let out = orig;
    if (this.lang === "en" && EN[key] !== undefined) {
      const m = orig.match(/^(\s*)[\s\S]*?(\s*)$/);
      out = m[1] + EN[key] + m[2];
    }
    return { orig, set: out };
  },

  walk(root) {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = tw.currentNode; n; n = tw.nextNode()) {
      if (n.nodeType === Node.TEXT_NODE) {
        if (!n.nodeValue.trim()) continue;
        const r = this.conv(n.nodeValue, this.texts.get(n));
        if (r.set !== n.nodeValue) n.nodeValue = r.set;
        this.texts.set(n, r);
        continue;
      }
      if (n.nodeType !== Node.ELEMENT_NODE) continue;  // the root fragment of a <template>
      if (n.tagName === "SCRIPT" || n.tagName === "STYLE" || n.tagName === "TEXTAREA" || n.tagName === "PRE") { continue; }
      if (n.tagName === "TEMPLATE") this.walk(n.content);
      for (const a of ["title", "placeholder"]) {
        if (!n.hasAttribute(a)) continue;
        const m = this.attrs.get(n) || {};
        const r = this.conv(n.getAttribute(a), m[a]);
        if (r.set !== n.getAttribute(a)) n.setAttribute(a, r.set);
        m[a] = r;
        this.attrs.set(n, m);
      }
    }
  },

  apply() {
    document.documentElement.lang = this.lang;
    this.walk(document.body);
    document.querySelectorAll("#lang-btn [data-l]").forEach((s) => s.classList.toggle("on", s.dataset.l === this.lang));
  },
};

/* translate a JS-built string; {0}, {1}… are replaced by the extra args */
function t(s, ...args) {
  const r = I18N.lang === "en" && EN[s] !== undefined ? EN[s] : s;
  return args.length ? r.replace(/\{(\d+)\}/g, (m, i) => (args[i] !== undefined ? args[i] : m)) : r;
}

document.addEventListener("DOMContentLoaded", () => {
  I18N.apply();
  const b = document.getElementById("lang-btn");
  if (b) b.onclick = () => I18N.set(I18N.lang === "ko" ? "en" : "ko");
});
