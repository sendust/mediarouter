---
name: mediamtx-webrtc-levelmeter
description: mediamtx path 를 브라우저에서 WebRTC(WHEP)로 받아 비디오 프리뷰와 오디오 레벨미터(dBFS, peak hold, 청취 토글)를 구현하는 방법. mediamtx 의 reader.js(MediaMTXWebRTCReader) + Web Audio API 사용. mediarouter 의 web/reader.js, web/meter.js 가 기준 구현.
---

# mediamtx path → 비디오 프리뷰 + 오디오 레벨미터 (reader.js)

브라우저에서 mediamtx path 를 WebRTC(WHEP)로 받아

- `<video>` 에 비디오 프리뷰를 띄우고
- 오디오 트랙을 Web Audio 로 분석해 L/R 레벨미터(dBFS)를 그리고
- 필요하면 그 path 의 소리를 청취(모니터링)하는

방법을 정리한다. 기준 구현은 mediarouter 의 `web/reader.js`(mediamtx 배포본 그대로), `web/meter.js`(레벨미터), `web/app.js`(카드 연결)이다.

---

## 1. 구성

```
mediamtx path ──WHEP(HTTP POST SDP)──▶ MediaMTXWebRTCReader (reader.js)
                                          │ onTrack(evt)
                     ┌────────────────────┴────────────────────┐
                 video track                                audio track
                     │                                          │
            <video muted autoplay>              MediaStreamSource (Web Audio)
                                                   ├─ gain 0 ─▶ destination   (디코더 유지용 무음 경로)
                                                   ├─ listenGain(0/1) ─▶ destination   (청취)
                                                   └─ upmix(2ch) ─▶ ChannelSplitter(2)
                                                                       ├─▶ Analyser L ─▶ RMS → dBFS → 막대
                                                                       └─▶ Analyser R ─▶ RMS → dBFS → 막대
                                          + muted hidden <audio> sink (Chrome 무음 버그 회피)
```

- path 하나당 reader(=RTCPeerConnection) 하나.
- 레벨 계산은 `requestAnimationFrame` 루프 하나에서 모든 미터를 갱신.
- AudioContext 는 페이지 전체에서 하나만 만든다.

---

## 2. 사전 조건 (mediamtx 쪽)

`mediamtx.yml`:

```yaml
webrtc: true
webrtcAddress: :8889            # WHEP HTTP 포트
webrtcEncryption: false         # http 페이지에서 쓸 때
webrtcAllowOrigins: ["*"]       # UI 가 다른 origin(포트)에서 로드되므로 CORS 허용 필요
webrtcLocalUDPAddress: :8189    # 미디어(UDP). 방화벽에서 열어야 함
webrtcIPsFromInterfaces: true
```

- WHEP URL: `http://<host>:8889/<path>/whep`
  (mediarouter 는 `config.json mediamtx.webrtc_url` 이 비어 있으면 `브라우저 host:webrtc_port` 를 씀)
- path 이름은 `encodeURIComponent()` 로 넣는다.

### 코덱 제약 (브라우저 WebRTC)

| 항목 | 권장 | 이유 |
|---|---|---|
| 오디오 | **Opus** (stereo) | 브라우저 WebRTC 는 AAC/MP3 를 못 받음 → 트랙이 아예 안 옴 |
| 비디오 | H.264 (High/Main), **B-frame 0** | B-frame 이 있으면 끊김/지연. H.265 는 브라우저마다 지원이 다름 |
| GOP | 짧게 (예: 1초) | 접속 직후 첫 키프레임까지 화면이 안 나옴 |

ffmpeg 예: `-c:v h264_nvenc -bf 0 -g 30 ... -c:a libopus -b:a 128k -ar 48000`

---

## 3. reader.js API

mediamtx 가 배포하는 `reader.js` 의 `MediaMTXWebRTCReader` 를 그대로 쓴다 (`window.MediaMTXWebRTCReader`).

```js
const reader = new MediaMTXWebRTCReader({
  url: `${whepBase}/${encodeURIComponent(path)}/whep`,
  user: "", pass: "", token: "",          // 인증이 있을 때만
  onError: (err) => { /* 문자열. 연결 후 끊기면 2초 뒤 자동 재시도하며 호출됨 */ },
  onTrack: (evt) => { /* RTCTrackEvent: evt.track.kind === "video" | "audio" */ },
});
// 정리
reader.close();
```

동작 요점:

- 생성 즉시 연결 시작. 끊기면(`failed`/`closed`) **자동으로 2초 후 재연결**하고 `onError("…, retrying in some seconds")` 를 부른다. 재연결되면 `onTrack` 이 **다시** 불린다 → onTrack 은 여러 번 불려도 안전하게 작성(기존 노드 disconnect 후 다시 연결).
- 첫 시도 전 코덱 탐색 단계에서 실패하면 재시도 없이 `onError` 한 번 (state `failed`).
- `video`, `audio` 두 transceiver(recvonly)를 항상 만든다. 오디오만 있는 path 도 그대로 사용 가능 (video 트랙이 안 올 뿐).
- offer 를 고쳐 stereo Opus(`stereo=1;sprop-stereo=1`), multiopus, L16, stereo PCMA/PCMU 를 광고한다 → stereo 가 그대로 전달됨.
- `close()` 는 PeerConnection 과 재시도 타이머를 정리한다. 카드/페이지를 닫을 때 반드시 호출 (안 하면 mediamtx 에 reader 세션이 남음).

---

## 4. 비디오 프리뷰

```html
<video muted playsinline autoplay></video>
```

```js
onTrack: (evt) => {
  const stream = evt.streams[0] || new MediaStream([evt.track]);
  if (evt.track.kind === "video") video.srcObject = stream;
}
```

- `muted` 필수: 소리 있는 autoplay 는 브라우저가 막는다. 소리는 Web Audio 쪽(listenGain)으로만 낸다.
- `playsinline`: iOS 에서 전체화면 강제 방지.
- 여러 path 를 한 카드에 묶을 때는 **비디오는 첫 번째 video path 하나만** 연결하고, 나머지 path 는 오디오만 쓴다. path 의 트랙 정보는 mediamtx API(`/v3/paths/list` 의 `tracks`)로 미리 알 수 있다.
- 오디오 전용 path(비디오 트랙 없음)는 레벨미터만 만든다.

---

## 5. 오디오 레벨미터 — Web Audio 연결

### 5.1 AudioContext 와 autoplay 정책

```js
const AudioMgr = {
  ctx: null,
  get() { return this.ctx ||= new (window.AudioContext || window.webkitAudioContext)(); },
};
// 사용자 제스처 전에는 ctx.state === "suspended" → 미터가 -∞ 로 멈춤
document.addEventListener("click", () => {
  if (AudioMgr.ctx && AudioMgr.ctx.state !== "running") AudioMgr.ctx.resume();
});
```

- 화면을 한 번 클릭하기 전까지 AudioContext 는 `suspended` 다. "화면을 한 번 클릭하세요" 같은 안내를 `ctx.state !== "running"` 일 때 보여준다.
- 대시보드처럼 사람이 안 만지는 화면이면 Chrome 을 `--autoplay-policy=no-user-gesture-required` 로 띄운다.

### 5.2 트랙 → 분석기 연결 (핵심 코드)

```js
function connectMeter(track, meters /* [ChannelMeter L, ChannelMeter R] */) {
  const ctx = AudioMgr.get();
  const stream = new MediaStream([track]);

  // (1) Chrome 버그 회피: 원격 WebRTC 스트림이 media element 에 붙어 있지 않으면
  //     Web Audio 로 무음만 들어온다. muted 숨김 <audio> 에 붙여 디코딩을 강제.
  const sink = document.createElement("audio");
  sink.muted = true;
  sink.autoplay = true;
  sink.srcObject = stream;
  sink.play().catch(() => {});

  const src = ctx.createMediaStreamSource(stream);

  // (2) gain 0 으로 destination 까지 연결: 그래프가 destination 에 닿아야
  //     브라우저가 계속 오디오를 끌어와 디코딩한다 (소리는 안 남)
  const silent = ctx.createGain();
  silent.gain.value = 0;
  src.connect(silent).connect(ctx.destination);

  // (3) 청취용 경로: 평소 0, 청취 시 1
  const listenGain = ctx.createGain();
  listenGain.gain.value = 0;
  src.connect(listenGain).connect(ctx.destination);

  // (4) mono 트랙도 L/R 양쪽에 보이도록 2ch 로 up-mix.
  //     ChannelSplitter 의 channelCount/Mode/Interpretation 은 스펙상 고정이라
  //     직접 설정하면 InvalidStateError 가 난다 → 앞에 unity GainNode 를 둬서 up-mix.
  const upmix = ctx.createGain();
  upmix.channelCount = 2;
  upmix.channelCountMode = "explicit";
  upmix.channelInterpretation = "speakers";
  const split = ctx.createChannelSplitter(2);
  src.connect(upmix).connect(split);

  const nodes = [src, silent, listenGain, upmix, split];
  meters.forEach((m, i) => {
    const an = ctx.createAnalyser();
    an.fftSize = 1024;            // 48 kHz 에서 약 21 ms 창
    split.connect(an, i);         // 출력 i(0=L, 1=R) → analyser
    m.attach(an);
    nodes.push(an);
  });

  return {
    listen(on) { listenGain.gain.value = on ? 1 : 0; },
    disconnect() {
      nodes.forEach((n) => { try { n.disconnect(); } catch (_) {} });
      sink.pause(); sink.srcObject = null;
      meters.forEach((m) => m.attach(null));
    },
  };
}
```

함정 정리:

| 증상 | 원인 | 해결 |
|---|---|---|
| 트랙은 오는데 미터가 계속 -∞ | Chrome: 원격 스트림이 media element 에 안 붙어 있으면 Web Audio 에 무음 | muted 숨김 `<audio>` sink 에 `srcObject` 지정 (1) |
| 잠깐 움직이다 멈춤 | 그래프가 destination 에 연결되지 않아 디코더가 멈춤 | gain 0 경로로 destination 연결 (2) |
| `InvalidStateError` (페이지 에러) | ChannelSplitter 의 channelInterpretation 등을 설정 | 앞단 GainNode 에서 up-mix (4) |
| mono 소스인데 L 만 움직임 | splitter 는 up-mix 를 안 함 | upmix GainNode (`explicit`, 2ch, `speakers`) |
| 첫 클릭 전 전부 -∞ | AudioContext suspended | 클릭 시 `ctx.resume()`, 안내 표시 |
| 재연결 후 미터 두 벌/누수 | onTrack 이 재연결마다 다시 불림 | 연결 전에 기존 노드 `disconnect()` |

### 5.3 레벨 계산 (RMS → dBFS) 과 탄도(ballistics)

```js
const METER_MIN_DB = -60, METER_MAX_DB = 0;
const ATTACK = 0.6;          // 상승 시 EMA 계수 (빠르게 올라감)
const RELEASE = 0.08;        // 하강 시 EMA 계수 (천천히 내려감)
const PEAK_HOLD_MS = 1200;   // peak 표시 유지 시간
const PEAK_DECAY_DB_S = 20;  // 유지 후 초당 하강량

const dbToPct = (db) =>
  ((Math.min(METER_MAX_DB, Math.max(METER_MIN_DB, db)) - METER_MIN_DB) / (METER_MAX_DB - METER_MIN_DB)) * 100;

class ChannelMeter {
  constructor(parent) {
    this.track = Object.assign(document.createElement("div"), { className: "mtrack" });
    this.mask = Object.assign(document.createElement("div"), { className: "mmask" });
    this.peakEl = Object.assign(document.createElement("div"), { className: "mpeak" });
    this.track.append(this.mask, this.peakEl);
    parent.append(this.track);
    this.level = this.peak = METER_MIN_DB;
    this.peakTs = 0;
    this.analyser = null;
  }
  attach(an) { this.analyser = an; this.buf = an ? new Float32Array(an.fftSize) : null; }
  update(now, dt) {
    let db = METER_MIN_DB;
    if (this.analyser) {
      this.analyser.getFloatTimeDomainData(this.buf);
      let sum = 0;
      for (const s of this.buf) sum += s * s;
      const rms = Math.sqrt(sum / this.buf.length);
      db = rms > 0 ? 20 * Math.log10(rms) : METER_MIN_DB;   // full-scale sine = -3.01 dBFS
    }
    this.level += (db - this.level) * (db > this.level ? ATTACK : RELEASE);
    if (this.level >= this.peak) { this.peak = this.level; this.peakTs = now; }
    else if (now - this.peakTs > PEAK_HOLD_MS) this.peak = Math.max(this.level, this.peak - PEAK_DECAY_DB_S * dt);
    this.mask.style.width = (100 - dbToPct(this.level)).toFixed(2) + "%";
    this.peakEl.style.left = `calc(${dbToPct(this.peak).toFixed(2)}% - 2px)`;
    return this.peak;
  }
}
```

- **RMS 기준**이다. full-scale 사인파가 -3.01 dBFS 로 표시된다 (sample peak 미터와 3 dB 차이). 1 kHz -20 dBFS(peak) 톤 → 약 -23 dBFS.
- `getFloatTimeDomainData` 를 쓴다 (Byte 버전은 8bit 양자화로 저레벨이 부정확).
- `dt` 는 초 단위, 탭 전환 후 큰 값이 들어오지 않게 0.2 로 제한.

### 5.4 그리기 — 고정 그라데이션 + 마스크

막대 색을 레벨마다 바꾸지 않고, **트랙에 색 띠를 고정**해 두고 오른쪽에서 덮는 불투명 마스크의 폭만 바꾼다 (레이아웃 재계산 없이 width 하나만 변경).

```css
/* -60..0 dB 선형: 70% = -18 dB, 90% = -6 dB */
.mtrack { position: relative; height: 8px; overflow: hidden;
  background: linear-gradient(to right, #1f9e57 0%, #2fbf71 70%, #e8b931 70%, #e8b931 90%, #e5534b 90%, #e5534b 100%); }
.mmask  { position: absolute; top: 0; right: 0; bottom: 0; width: 100%; background: #0b0e12; }
.mpeak  { position: absolute; top: 0; bottom: 0; width: 2px; left: 0; background: #fff; opacity: .85; }
.scale  { position: relative; height: 14px; font-size: 9px; }
.scale span { position: absolute; transform: translateX(-50%); }
.scale span:first-child { transform: none; }
.scale span:last-child  { transform: translateX(-100%); }
```

눈금은 같은 `dbToPct()` 로 배치해야 막대와 정확히 맞는다:

```js
const TICKS = [-60, -45, -30, -18, -6, 0];   // 좁은 미터에서 -12 는 -18/-6 과 겹쳐 생략
scaleEl.innerHTML = TICKS.map((d) => `<span style="left:${dbToPct(d)}%">${d}</span>`).join("");
```

### 5.5 갱신 루프 (하나만)

```js
const views = new Set();          // update(now, dt) 를 가진 객체들
let last = performance.now();
function loop(now) {
  const dt = Math.min(0.2, (now - last) / 1000);
  last = now;
  views.forEach((v) => v.update(now, dt));
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
```

미터마다 rAF/setInterval 을 따로 돌리지 않는다. 탭이 백그라운드면 rAF 가 멈추므로 CPU 를 쓰지 않는다.

---

## 6. 청취(모니터링) 토글

- path 라벨 클릭 → 그 path 의 `listenGain` 만 1, 나머지는 0 (한 번에 한 path 만 청취).
- 토글 시 `ctx.resume()` 도 같이 호출 (클릭이 곧 사용자 제스처).
- `<video>` 는 항상 muted 로 두고 소리는 이 경로로만 낸다 → 비디오/오디오 path 가 달라도 섞이지 않음.

---

## 7. 전체 최소 예제 (한 파일)

`reader.js` 와 같은 폴더에 두고 열면 된다. `WHEP_BASE`, `PATH` 만 바꾼다.

```html
<!doctype html>
<meta charset="utf-8">
<title>path monitor</title>
<style>
  body { background: #111; color: #ddd; font: 13px sans-serif; }
  video { width: 640px; background: #000; display: block; }
  .meter { width: 640px; margin-top: 6px; }
  .mtrack { position: relative; height: 10px; margin: 3px 0; overflow: hidden;
    background: linear-gradient(to right, #1f9e57 0%, #2fbf71 70%, #e8b931 70%, #e8b931 90%, #e5534b 90%, #e5534b 100%); }
  .mmask { position: absolute; top: 0; right: 0; bottom: 0; width: 100%; background: #0b0e12; }
  .mpeak { position: absolute; top: 0; bottom: 0; width: 2px; left: 0; background: #fff; }
  .scale { position: relative; height: 14px; font-size: 10px; color: #888; }
  .scale span { position: absolute; transform: translateX(-50%); }
  .scale span:first-child { transform: none; } .scale span:last-child { transform: translateX(-100%); }
</style>
<video muted playsinline autoplay></video>
<div class="meter"><div id="bars"></div><div class="scale" id="scale"></div></div>
<button id="listen">청취</button> <span id="msg"></span> <span id="unlock">화면을 한 번 클릭하세요</span>
<script src="reader.js"></script>
<script>
const WHEP_BASE = `${location.protocol}//${location.hostname}:8889`;
const PATH = "sdi1_va12";

/* --- meter (5.3) --- */
const MIN = -60, MAX = 0, ATTACK = 0.6, RELEASE = 0.08, HOLD = 1200, DECAY = 20;
const pct = (db) => ((Math.min(MAX, Math.max(MIN, db)) - MIN) / (MAX - MIN)) * 100;
class ChannelMeter {
  constructor(parent) {
    this.track = Object.assign(document.createElement("div"), { className: "mtrack" });
    this.mask = Object.assign(document.createElement("div"), { className: "mmask" });
    this.peakEl = Object.assign(document.createElement("div"), { className: "mpeak" });
    this.track.append(this.mask, this.peakEl); parent.append(this.track);
    this.level = this.peak = MIN; this.peakTs = 0; this.analyser = null;
  }
  attach(an) { this.analyser = an; this.buf = an ? new Float32Array(an.fftSize) : null; }
  update(now, dt) {
    let db = MIN;
    if (this.analyser) {
      this.analyser.getFloatTimeDomainData(this.buf);
      let s = 0; for (const v of this.buf) s += v * v;
      const rms = Math.sqrt(s / this.buf.length);
      db = rms > 0 ? 20 * Math.log10(rms) : MIN;
    }
    this.level += (db - this.level) * (db > this.level ? ATTACK : RELEASE);
    if (this.level >= this.peak) { this.peak = this.level; this.peakTs = now; }
    else if (now - this.peakTs > HOLD) this.peak = Math.max(this.level, this.peak - DECAY * dt);
    this.mask.style.width = (100 - pct(this.level)).toFixed(2) + "%";
    this.peakEl.style.left = `calc(${pct(this.peak).toFixed(2)}% - 2px)`;
  }
}
const meters = [new ChannelMeter(bars), new ChannelMeter(bars)];
scale.innerHTML = [-60, -45, -30, -18, -6, 0].map((d) => `<span style="left:${pct(d)}%">${d}</span>`).join("");

/* --- audio graph (5.1, 5.2) --- */
let ctx = null, conn = null, listening = false;
const audioCtx = () => (ctx ||= new AudioContext());
const showUnlock = () => { unlock.hidden = !ctx || ctx.state === "running"; };
document.addEventListener("click", () => { if (ctx && ctx.state !== "running") ctx.resume().then(showUnlock); });

function connectMeter(track) {
  if (conn) conn.disconnect();
  const c = audioCtx(); showUnlock();
  const stream = new MediaStream([track]);
  const sink = Object.assign(document.createElement("audio"), { muted: true, autoplay: true });
  sink.srcObject = stream; sink.play().catch(() => {});
  const src = c.createMediaStreamSource(stream);
  const silent = c.createGain(); silent.gain.value = 0; src.connect(silent).connect(c.destination);
  const listenGain = c.createGain(); listenGain.gain.value = listening ? 1 : 0; src.connect(listenGain).connect(c.destination);
  const upmix = c.createGain();
  upmix.channelCount = 2; upmix.channelCountMode = "explicit"; upmix.channelInterpretation = "speakers";
  const split = c.createChannelSplitter(2); src.connect(upmix).connect(split);
  const nodes = [src, silent, listenGain, upmix, split];
  meters.forEach((m, i) => { const an = c.createAnalyser(); an.fftSize = 1024; split.connect(an, i); m.attach(an); nodes.push(an); });
  conn = {
    listen: (on) => { listenGain.gain.value = on ? 1 : 0; },
    disconnect: () => { nodes.forEach((n) => { try { n.disconnect(); } catch (_) {} }); sink.srcObject = null; meters.forEach((m) => m.attach(null)); },
  };
}
listen.onclick = () => { listening = !listening; listen.textContent = listening ? "청취 중지" : "청취"; if (conn) conn.listen(listening); };

/* --- reader (3, 4) --- */
const video = document.querySelector("video");
const reader = new MediaMTXWebRTCReader({
  url: `${WHEP_BASE}/${encodeURIComponent(PATH)}/whep`,
  onError: (err) => { msg.textContent = err; if (conn) { conn.disconnect(); conn = null; } },
  onTrack: (evt) => {
    msg.textContent = "";
    if (evt.track.kind === "video") video.srcObject = evt.streams[0] || new MediaStream([evt.track]);
    else if (evt.track.kind === "audio") connectMeter(evt.track);
  },
});
window.addEventListener("beforeunload", () => reader.close());

/* --- one rAF loop (5.5) --- */
let last = performance.now();
(function loop(now) {
  const dt = Math.min(0.2, (now - last) / 1000); last = now;
  meters.forEach((m) => m.update(now, dt));
  requestAnimationFrame(loop);
})(last);
</script>
```

---

## 8. 여러 path / 카드 구성 (mediarouter 방식)

- 모니터 카드 하나 = path 목록. path 마다 `MediaMTXWebRTCReader` 하나, 오디오가 있는 path 마다 `PathMeter`(L/R 두 막대 + 라벨 + peak 수치) 하나.
- 비디오 창은 **video 트랙이 있는 첫 path** 하나만 (`H264/H265/VP8/VP9/AV1` 코덱 여부로 판단).
- 비디오 전용 path 가 비디오 창 path 가 아니면 reader 를 만들지 않는다 (불필요한 세션 방지).
- 카드 비활성화/삭제/설정 변경 시: `reader.close()` → 미터 `disconnect()` → `video.srcObject = null` → rAF 대상 Set 에서 제거.
- 라벨 색: 연결 전/끊김 = 빨강(`off`), 청취 중 = 강조색(`listen`).

## 9. 수신 통계 (선택)

`onTrack` 의 `evt.receiver` 를 모아 1초마다 `getStats()` 의 `inbound-rtp` 를 읽는다.

- bitrate = Δ`bytesReceived` × 8 / Δt, fps = Δ`framesDecoded` / Δt (5초 슬라이딩 창으로 평활)
- `packetsLost`, 오디오 `jitter`, 비디오 `frameWidth`×`frameHeight`
- `framesPerSecond` 는 정수라 59.94 같은 값이 안 나오므로 Δframes 로 계산.

## 10. 검증

Web Audio / WebRTC 문제는 추측하지 말고 실제 브라우저에서 재현한다.

- headless Chrome(`chrome-headless-shell`) + puppeteer-core 로 페이지를 열고
  `--autoplay-policy=no-user-gesture-required` 옵션으로 AudioContext 를 바로 running 상태로 만든다.
- `page.on("pageerror")` 로 예외(예: InvalidStateError) 수집.
- 미터 상태(각 ChannelMeter 의 `level`) 를 `page.evaluate` 로 읽어 -60 이 아닌지 확인.
- 기준 신호: 1 kHz 사인 full-scale → 약 -3 dBFS, -20 dBFS(peak) → 약 -23 dBFS 로 표시되면 정상.
- mediamtx 쪽 확인: `curl http://127.0.0.1:9997/v3/paths/get/<path>` 의 `ready`, `tracks`, `readers`.

## 11. 문제 해결 체크리스트

| 증상 | 확인 |
|---|---|
| `stream not found` | path 이름/인코딩, path 가 ready 인지 (퍼블리셔 동작 중인지) |
| 연결은 되는데 화면 검정 | 첫 키프레임 대기 (GOP 길이), H.265 를 브라우저가 지원하는지 |
| 오디오 트랙이 안 옴 | 오디오 코덱이 Opus 인지 (AAC/MP3 는 WebRTC 불가) |
| 다른 PC 에서만 안 됨 | UDP 8189 방화벽, `webrtcIPsFromInterfaces`/`webrtcAdditionalHosts`, CORS(`webrtcAllowOrigins`) |
| 미터 -∞ 고정 | AudioContext suspended(클릭), muted `<audio>` sink, gain 0 → destination 경로 |
| 한쪽 막대만 움직임 | mono 소스 → upmix GainNode 누락 |
| 탭 오래 열어두면 느려짐 | 재연결마다 노드/sink 를 disconnect 하는지, reader 를 close 하는지 |
