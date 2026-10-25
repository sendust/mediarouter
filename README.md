# mediarouter

DeckLink SDI 입력을 local mediamtx 로 밀어넣고, mediamtx 스트림을 외부 여러곳으로 보내는
ffmpeg 인코더들을 감시/제어하는 웹 UI + Python 백엔드.
(AWS Elemental Live 대체용 사내 도구 — 특히 오디오 채널 라우팅(L/R 선택)을 지원)

```
DeckLink ─ ffmpeg ─▶ mediamtx (sdiN_va12, sdiN_a34 …) ─ ffmpeg ─▶ 외부 RTMP / SRT / RTSP / UDP …
                          │
                          └─ WebRTC (WHEP) ─▶ 브라우저 모니터 카드 (영상 + 오디오 레벨미터)
```

## 주요 기능

- **인코더 카드** — 인코더 1개 = ffmpeg 프로세스 1개 (OS 스레드 1개가 감시)
  - 실행/정지/재시작, 오류시 자동재시작 (backoff), 예약 재시작 (매일 / 주기), 백엔드 시작시 자동실행
  - progress 정지(hang) 감지, 속도저하(speed < 기준) 감지/재시작, fflog 패턴 매칭 재시작
  - ffmpeg CLI 직접 편집 (복사/붙여넣기), fflog 보기, 실제 사용중인 video/audio 코덱, CPU/RAM, fps, bitrate, speed
  - 실행중인 카드는 붉은 테두리, 속도저하는 노란 테두리
- **Encoder helper** — 입력/출력을 고르면 CLI 를 생성
  - 입력: DeckLink (장치/포맷/채널수), mediamtx path (여러 path 의 오디오를 합쳐서 사용 가능), testsrc (lavfi), custom
  - 오디오: 스테레오 출력마다 L, R 채널을 임의의 입력/채널에서 선택, loudnorm (I / LRA / TP)
  - 비디오: h264_nvenc / libx264 / stream copy / none, CPU 또는 CUDA deinterlace(bwdif/yadif/estdif)·scale, GOP, B-frame
  - 출력: mediamtx (N audio stream — 스테레오 쌍마다 별도 path), mediamtx path, 외부 URL 여러개 (tee), null
  - 출력 프리셋: UDP 멀티캐스트 (MPEG-TS) — group / port / TTL / 송출 NIC(localaddr) 입력, `pkt_size=1316`
- **모니터 카드** — mediamtx path 들을 WebRTC 로 받아 영상 + 수평 오디오 레벨미터 (dBFS, peak hold)
  - path 라벨 클릭시 해당 오디오 청취
  - path 정보: codec/profile/해상도/채널, path 입력 bitrate, reader 수, 트랙별 fps·bitrate·패킷손실
  - enable 체크 — 끄면 WebRTC 연결/미터 중지 (켜짐 하늘색 테두리, 꺼짐 회색 테두리)
- 카드 drag & drop 순서변경, 시스템 상태 (CPU/RAM/NIC TX·RX/ffmpeg 수), mediamtx path/reader 목록,
  백엔드 로그 최근 1000줄 (레벨 필터/검색)
- 모바일 브라우저 대응

## 요구사항

- Linux, Python 3.10+
- ffmpeg — `--enable-decklink`, NVENC/CUDA 사용시 `--enable-nvenc --enable-cuda-nvcc` 등
- [mediamtx](https://github.com/bluenviron/mediamtx) — `mediamtx.yml` 에서 `api: true` (API :9997), RTSP :8554, WebRTC :8889
- (선택) Blackmagic Desktop Video 드라이버, NVIDIA 드라이버

> **Quadro P2200 (Pascal) 주의**: NVIDIA 드라이버는 580 branch 까지만 Pascal 지원 (590+ 미지원, `-open` 모듈 불가).
> ffmpeg 은 nv-codec-headers `sdk/13.0` 으로 빌드해야 함 (master / 13.1 은 드라이버 610+ 필요 →
> `Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0`).

## 실행

```
cp config.example.json config.json   # 처음 한번 (없으면 기본값으로 자동 생성)
./run.sh                             # 첫 실행시 .venv 생성 + 패키지 설치, 이후 백엔드 실행
```

브라우저로 `http://<host>:8080` (config.json `server.port`).
백그라운드 실행 예: `nohup ./run.sh >> logs/console.log 2>&1 &`

백엔드를 종료(SIGINT/SIGTERM)하면 실행중인 인코더도 모두 정지합니다.
다음 시작시 이전 실행에서 남은 ffmpeg (env `MEDIAROUTER_ENCODER_ID`) 를 정리하고 autostart 인코더를 실행합니다.

## systemd 서비스로 실행

예시 unit 파일: [`deploy/mediarouter.service`](deploy/mediarouter.service)

```ini
# /etc/systemd/system/mediarouter.service
[Unit]
Description=mediarouter backend (ffmpeg encoder supervisor + web UI)
Wants=network-online.target
After=network-online.target mediamtx.service

[Service]
Type=simple
User=sendust
Group=sendust
WorkingDirectory=/home/sendust/mediarouter
ExecStart=/home/sendust/mediarouter/.venv/bin/python -m app.main
Environment=PYTHONUNBUFFERED=1
# Environment=MEDIAROUTER_CONFIG=/home/sendust/mediarouter/config.json

# SIGTERM goes to the backend only; it stops every ffmpeg itself (SIGINT -> SIGTERM -> SIGKILL).
# Anything still alive after TimeoutStopSec is SIGKILLed by systemd.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30

Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

설치:

```
./run.sh                                  # 한번 실행해서 .venv 생성 확인 후 Ctrl+C
sudo cp deploy/mediarouter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediarouter
```

운영:

```
systemctl status mediarouter
sudo systemctl restart mediarouter        # 실행중인 인코더는 정지 → autostart 인코더만 다시 시작
journalctl -u mediarouter -f              # 콘솔 출력 (logs/backend.log 에도 동일하게 기록)
```

- `User`, `Group`, 경로는 설치 위치에 맞게 수정하세요. 이 사용자는 DeckLink(`/dev/blackmagic/*`), NVIDIA 장치에 접근 가능해야 합니다.
- `KillMode=mixed` — systemd 가 ffmpeg 를 직접 죽이지 않고 백엔드가 인코더를 순서대로 정지하게 합니다.
  `TimeoutStopSec` 는 `encoder_watchdog.stop_timeout_sec` × 2 보다 넉넉하게 두세요.
- mediamtx 가 systemd 서비스가 아니면 `After=` 의 `mediamtx.service` 는 지워도 됩니다 (없어도 오류는 아님).
- 서비스 등록 전에 `nohup ./run.sh` 등으로 띄운 백엔드가 있으면 먼저 종료하세요 (포트 충돌).
- 서비스 재시작/서버 재부팅 후에도 계속 송출해야 하는 인코더는 카드에서 "백엔드 시작시 자동실행" 을 켜두세요.

## 구조

| 파일 | 역할 |
|---|---|
| `app/main.py` | FastAPI, REST API, `/events` SSE (encoders 0.5s / system 1s / logs 1s / mediamtx 2s) |
| `app/encoder.py` | 인코더 1개 = supervisor OS 스레드 1개 + stdout(progress)/stderr 리더 스레드, watchdog |
| `app/manager.py` | 인코더 목록, autostart, 종료시 정지, 이전 실행의 고아 ffmpeg 정리 |
| `app/fflog.py` | 인코더별 ffmpeg 로그 파일 + ffreport watchdog (크기 초과시 rotate) |
| `app/cli_builder.py` | helper 입력 → ffmpeg CLI 생성 (오디오 라우팅 filter_complex) |
| `app/helpers.py` | DeckLink enumerate / format 조회, lavfi 소스, ffprobe, NVENC/CUDA 실측 probe |
| `app/mediamtx.py` | mediamtx API poller (path, codec 정보, 입력 bitrate, publisher/reader 의 remote 주소) |
| `app/sysinfo.py` | hostname, IP, CPU, RAM, NIC TX/RX, ffmpeg 프로세스 수 |
| `app/logger.py` | 콘솔 + `logs/backend.log` (rotate) + 최근 1000건 ring buffer |
| `app/config.py` | config.json 로드/저장 (atomic write, 누락 키는 기본값으로 채워 기록) |
| `web/index.html`, `app.js` | UI (vanilla JS), 인코더/모니터 카드, 시스템, 로그 |
| `web/helper.js` | Encoder / Monitor helper 대화창 |
| `web/meter.js` | WebRTC 모니터, Web Audio 레벨미터, WebRTC 수신 통계 |
| `web/reader.js` | mediamtx WHEP reader |
| `deploy/mediarouter.service` | systemd unit 예시 |

## config.json

모든 키는 백엔드 시작시 기본값으로 채워져 파일에 기록됩니다. 인코더/모니터는 UI 에서 편집하면 저장됩니다.
`config.json` 은 호스트별 운영 상태라 git 에서 제외되어 있고, `config.example.json` 이 예시입니다
(환경변수 `MEDIAROUTER_CONFIG` 로 경로 변경 가능).

- `server` — `host`, `port`
- `ffmpeg` — `path`, `ffprobe`
- `mediamtx` — `api_url`, `rtsp_url`, `rtsp_transport` (기본 `udp`), `udp_buffer_size` (UDP RTSP 소켓 버퍼, 기본 4MB),
  `webrtc_port`, `webrtc_url` (비우면 `브라우저 host:webrtc_port`), `poll_interval_sec`
- `encoder_watchdog`
  - `poll_interval_sec` — 감시 주기 (인코더마다 jitter 적용)
  - `progress_timeout_sec` — `-progress` 의 out_time/size/frame 이 이 시간동안 변하지 않으면 hang 으로 판단해 재시작
  - `startup_grace_sec`, `grace_multiplier` — 시작 직후에는 timeout × multiplier
  - `restart_hold_sec`, `restart_backoff_max_sec`, `stable_run_sec` — 연속 실패시 hold 를 2배씩 (최대값까지), stable_run_sec 이상 돌았으면 초기화
  - `stop_timeout_sec` — 정지시 SIGINT 후 대기, 이후 SIGTERM → SIGKILL
  - `pipe_buffer_bytes` — ffmpeg stdout/stderr 파이프 크기 (F_SETPIPE_SZ)
  - `speed_check`, `speed_min`, `speed_window_sec` — 최근 `speed_window_sec` 동안의 실제 처리속도(out_time 증가량 / 경과시간)가
    `speed_min` 미만이면 카드에 SLOW 표시 + 경고 로그. 인코더 카드의 "속도저하시 재시작" 을 켜면 재시작까지 함
- `ffreport_watchdog` — `dir`, `loglevel`, `max_size_mb`, `backup_count`, `check_interval_sec`, `tail_lines`,
  `restart_on_patterns` (정규식 목록; fflog 에 매칭되면 재시작. 예: `"Connection refused"`).
  fflog 크기는 rotate 만 하고 재시작 사유가 되지 않습니다.
- `logging` — `dir`, `level`, `max_size_mb`, `backup_count`, `ring_size`
- `encoders[]` — `title`, `comment`, `cli` (실행되는 텍스트 그대로), `autostart`, `auto_restart`, `speed_restart`,
  `scheduled_restart {enabled, mode: daily|interval, time, interval_min}`, `helper` (helper 대화창 상태)
- `monitors[]` — `title`, `paths[]`, `enabled` (기본 true; false 면 WebRTC 연결·레벨미터를 하지 않음)

## REST API

| | |
|---|---|
| `GET/POST /api/encoders`, `PATCH/DELETE /api/encoders/{id}` | 인코더 목록/추가/수정/삭제 |
| `POST /api/encoders/{id}/start\|stop\|restart` | 인코더 제어 |
| `GET/DELETE /api/encoders/{id}/fflog` | fflog 보기/지우기 |
| `POST /api/encoders/order`, `POST /api/monitors/order` | 카드 순서 (`{"ids": [...]}`) |
| `GET/POST /api/monitors`, `PATCH/DELETE /api/monitors/{id}` | 모니터 |
| `GET /api/helper/*` | helper 용 (DeckLink 장치/포맷, mediamtx path, probe, CLI build) |
| `GET /api/system`, `GET /api/logs`, `GET /events` (SSE) | 상태 |

인증은 없습니다. 사내망에서만 사용하세요.

## 동작 메모

- 실행시 `-hide_banner -nostdin -nostats -progress pipe:1 -stats_period 0.5 -loglevel level+info` 가
  (CLI 에 없으면) 자동으로 추가됩니다. 카드의 "실행중인 실제 argv" 에서 확인할 수 있습니다.
- DeckLink → mediamtx 는 `sdiN_va12` (video + ch1/2), `sdiN_a34`, `sdiN_a56`, `sdiN_a78` 로 나눕니다.
  WebRTC 모니터용 오디오는 opus 여야 브라우저에서 재생/미터가 동작합니다 (AAC 는 WebRTC 불가).
- 외부 URL 이 2개 이상이면 tee muxer (`onfail=ignore`) 로 한번 인코딩해 보냅니다.
- loudnorm 은 single-pass 이며 출력이 192kHz 이므로 뒤에 `aresample` 이 자동으로 붙습니다.
- 인코더 카드의 CPU% 는 코어 기준입니다 (100% = 1 스레드 분량).
- 모니터 카드의 path 라벨을 클릭하면 그 path 오디오를 청취합니다 (기본은 무음, 미터만 동작).
  브라우저 정책상 화면을 한번 클릭해야 오디오 미터가 시작됩니다.
- 모니터 카드 하단 path 정보: codec/profile/해상도/채널, path 입력 bitrate, reader 수는 mediamtx API,
  트랙별 fps·bitrate·패킷손실은 브라우저 WebRTC 수신 통계(5초 평균)입니다. 꺼진(disable) 카드에는 mediamtx 정보만 표시됩니다.
- mediamtx 쪽 UDP 수신 손실이 보이면 mediamtx.yml `udpReadBufferSize: 4194304` 도 고려하세요.
