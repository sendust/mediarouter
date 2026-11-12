"""Text command shell for headless use: `curl http://host:8080/cli | bash`.

GET /cli serves a small bash REPL (CLIENT_SCRIPT) that posts every line to
POST /cli/run and prints the plain-text answer, so all parsing lives here.
Encoders and monitors are addressed by their 1-based position in the list
(as on the dashboard) or by id. The GUI ffmpeg helper is not exposed.
"""
import copy
import html
import re
import threading
import time
import unicodedata

from . import status
from .logger import get_logger, ring

log = get_logger("cli")


class CliError(Exception):
    pass


HELP = """\
mediarouter CLI — 명령 목록  (N = 목록 번호 또는 id, 'all' = 전체)

  help                         이 도움말
  list encoders                인코더 목록 (번호와 함께)          별칭: ls, list
  list monitors                모니터 목록
  list paths                   mediamtx path 목록
  detail N                     인코더 상세 (key : value)
  detail monitor N             모니터 상세

  get status [all]             전체 상태 (health, 인코더 표, 이슈)
  get status N                 인코더 N 상태
  get N <key>                  인코더 설정/상태 값 하나 (detail 의 key)
  get log N [lines]            인코더 N 의 ffmpeg 로그 (기본 40줄)
  get logs [lines]             backend 로그 (기본 40줄)
  get system                   CPU / 메모리 / GPU / NIC
  get config [section]         전역 설정 (config.json 섹션)
  get monitor N <key>          모니터 설정 값 하나

  set N <key> <value>          인코더 설정 변경 (아래 key 참고)
  set N start|stop|restart     인코더 시작 / 정지 / 재시작      (N 에 all 가능)
  set N clearlog               인코더 ffmpeg 로그 비우기
  set monitor N <key> <value>  모니터 설정 변경 (title, enabled, muted, paths)
  set config <sec.key> <value> 전역 설정 변경 (일부는 backend 재시작 후 적용)

  add encoder [title]          빈 인코더 추가 (cli 는 set N cli 또는 edit N 으로)
  delete N yes                 인코더 삭제 (실행 중이면 정지 후 삭제)
  move N M                     인코더 N 을 M 번 위치로 이동
  edit N                       $EDITOR 로 ffmpeg 명령줄 편집 (대화형 모드 전용)
  quit                         종료 (Ctrl-D)

인코더 key: title, comment, cli, autostart, auto_restart, speed_restart,
  scheduled_restart.enabled, scheduled_restart.mode (daily|interval),
  scheduled_restart.time (HH:MM), scheduled_restart.interval_min
bool 값: on/off, true/false, yes/no, 1/0.  값에 공백이 있어도 따옴표 없이 쓸 수 있습니다.
예) set 1 title DeckLink 1 → 송출  ·  set 2 autostart on  ·  set 1 restart  ·  get status 1
"""

ENC_KEYS = ("title", "comment", "cli", "autostart", "auto_restart", "speed_restart",
            "scheduled_restart.enabled", "scheduled_restart.mode", "scheduled_restart.time",
            "scheduled_restart.interval_min")
MON_KEYS = ("title", "enabled", "muted", "paths")
CONFIG_SECTIONS_RO = ("encoders", "monitors")
_TRUE = {"1", "on", "true", "yes", "y"}
_FALSE = {"0", "off", "false", "no", "n"}


# ------------------------------------------------------------------ format
def _width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, w):
    s = str(s)
    return s + " " * max(0, w - _width(s))


def _table(head, rows):
    rows = [[("-" if c is None else str(c)) for c in r] for r in rows]
    ws = [max([_width(h)] + [_width(r[i]) for r in rows]) for i, h in enumerate(head)]
    out = ["  ".join(_pad(h, ws[i]) for i, h in enumerate(head)).rstrip(),
           "  ".join("-" * w for w in ws)]
    out += ["  ".join(_pad(c, ws[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    return "\n".join(out)


def _kv(pairs):
    w = max((_width(k) for k, _ in pairs), default=0)
    out = []
    for k, v in pairs:
        lines = _fmt(v).split("\n")
        out.append(f"{_pad(k, w)} : {lines[0]}")
        out += [" " * (w + 3) + ln for ln in lines[1:]]
    return "\n".join(out)


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".")
    if isinstance(v, (list, tuple)):
        return ", ".join(_fmt(x) for x in v) if v else "-"
    return str(v)


def _dur(sec):
    if sec is None:
        return None
    sec = int(sec)
    d, h, m, s = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60, sec % 60
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def _bps(v):
    return f"{v / 1e6:.2f} Mb/s" if v else None


def _bytes(v):
    return f"{v / 2**30:.1f} GiB" if v else None


def _ts(v):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if v else None


def _flatten(d, prefix=""):
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out += _flatten(v, f"{prefix}{k}.")
        else:
            out.append((f"{prefix}{k}", v))
    return out


def _parse_value(raw, current, key):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    if isinstance(current, bool):
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise CliError(f"{key}: on/off 값이 필요합니다 (입력: {raw!r})")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            raise CliError(f"{key}: 정수가 필요합니다 (입력: {raw!r})") from None
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            raise CliError(f"{key}: 숫자가 필요합니다 (입력: {raw!r})") from None
    if isinstance(current, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(current, dict):
        raise CliError(f"{key} 는 섹션입니다. 하위 key 를 지정하세요 (예: {key}.<name>)")
    return raw


def _nest(key, value):
    out = value
    for part in reversed(key.split(".")):
        out = {part: out}
    return out


def _lookup(d, key):
    cur = d
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(key)
        cur = cur[part]
    return cur


# ------------------------------------------------------------------- shell
class Cli:
    """ctx: manager, store, mtx, sysinfo, status_doc() -> /api/status document."""

    def __init__(self, manager, store, mtx, sysinfo, status_doc):
        self.manager = manager
        self.store = store
        self.mtx = mtx
        self.sysinfo = sysinfo
        self.status_doc = status_doc

    def run(self, line, value=None):
        """Execute one command line. `value` (multi-line text, e.g. from `edit`) replaces
        the trailing value argument of a `set`. Raises CliError for user errors."""
        line = (line or "").strip()
        if not line or line.startswith("#"):
            return ""
        words = line.split()
        cmd, args = words[0].lower(), words[1:]
        fn = {
            "help": self._help, "?": self._help, "h": self._help,
            "list": self._list, "ls": self._list,
            "detail": self._detail, "show": self._detail,
            "get": self._get, "set": self._set,
            "add": self._add, "delete": self._delete, "del": self._delete, "rm": self._delete,
            "move": self._move, "mv": self._move,
        }.get(cmd)
        if fn is None:
            raise CliError(f"알 수 없는 명령: {cmd}  ('help' 로 명령 목록 확인)")
        if cmd == "set":
            return fn(line, value)
        return fn(args)

    # -------------------------------------------------------------- lookup
    def _enc(self, ref):
        encs = self.manager.all()
        if ref.isdigit():
            i = int(ref)
            if 1 <= i <= len(encs):
                return i, encs[i - 1]
            raise CliError(f"인코더 번호 {i} 없음 (1~{len(encs)})")
        for i, e in enumerate(encs, 1):
            if e.id == ref:
                return i, e
        raise CliError(f"인코더 없음: {ref}  ('list encoders' 로 번호 확인)")

    def _mon(self, ref):
        mons = self.store.items("monitors")
        if ref.isdigit():
            i = int(ref)
            if 1 <= i <= len(mons):
                return i, mons[i - 1]
            raise CliError(f"모니터 번호 {i} 없음 (1~{len(mons)})")
        for i, m in enumerate(mons, 1):
            if m["id"] == ref:
                return i, m
        raise CliError(f"모니터 없음: {ref}")

    def _enc_status(self, enc_id):
        for e in self.status_doc()["encoders"]:
            if e["id"] == enc_id:
                return e
        return {}

    # ---------------------------------------------------------------- help
    def _help(self, _args):
        return HELP

    # ---------------------------------------------------------------- list
    def _list(self, args):
        what = (args[0].lower() if args else "encoders").rstrip("s")
        if what in ("encoder", "enc", "e"):
            doc = {e["id"]: e for e in self.status_doc()["encoders"]}
            rows = []
            for i, enc in enumerate(self.manager.all(), 1):
                st = doc.get(enc.id, {})
                rows.append([i, enc.title, st.get("state"), _fmt(enc.cfg.get("autostart")),
                             _dur(st.get("uptime_sec")), st.get("restarts"),
                             _fmt(st.get("publish_paths")), enc.id])
            if not rows:
                return "인코더 없음 ('add encoder <title>' 로 추가)"
            return _table(["#", "title", "state", "autostart", "uptime", "restarts", "publish", "id"], rows)
        if what in ("monitor", "mon", "m"):
            rows = [[i, m.get("title"), _fmt(m.get("enabled")), _fmt(m.get("muted")), _fmt(m.get("paths")), m["id"]]
                    for i, m in enumerate(self.store.items("monitors"), 1)]
            return _table(["#", "title", "enabled", "muted", "paths", "id"], rows) if rows else "모니터 없음"
        if what in ("path", "p"):
            return self._paths()
        raise CliError("list encoders | list monitors | list paths")

    def _paths(self):
        snap = self.mtx.snapshot or {}
        if not snap.get("ok"):
            raise CliError(f"mediamtx API 접속 불가: {snap.get('error') or '-'}")
        rows = []
        for p in snap.get("paths") or []:
            tracks = []
            for t in p.get("tracks") or []:
                desc = t.get("codec") or "?"
                if t.get("width"):
                    desc += f" {t['width']}x{t['height']}"
                if t.get("channels"):
                    desc += f" {t['channels']}ch"
                tracks.append(desc)
            rows.append([p["name"], "ready" if p.get("ready") else "-", _bps(p.get("bps_in")),
                         len(p.get("readers") or []), ", ".join(tracks) or "-"])
        return _table(["path", "state", "in", "readers", "tracks"], rows) if rows else "path 없음"

    # -------------------------------------------------------------- detail
    def _detail_pairs(self, enc):
        cfg = copy.deepcopy(enc.cfg)
        st = self._enc_status(enc.id)
        pairs = [("id", cfg.get("id"))]
        pairs += [(k, v) for k, v in _flatten({k: v for k, v in cfg.items() if k not in ("id", "cli", "helper")})]
        pairs.append(("helper", "GUI helper 설정 있음" if cfg.get("helper") else "없음"))
        pairs += [
            ("state", st.get("state")),
            ("health", st.get("health")),
            ("issues", st.get("issues")),
            ("pid", st.get("pid")),
            ("uptime", _dur(st.get("uptime_sec"))),
            ("restarts", st.get("restarts")),
            ("fps", st.get("fps")),
            ("speed", st.get("speed")),
            ("bitrate", _bps(st.get("bitrate_bps"))),
            ("drop_frames", st.get("drop_frames")),
            ("dup_frames", st.get("dup_frames")),
            ("cpu_percent", st.get("cpu_percent")),
            ("rss", f"{st['rss_bytes'] / 2**20:.0f} MiB" if st.get("rss_bytes") else None),
            ("video", st.get("video")),
            ("audio", st.get("audio")),
            ("publish_paths", st.get("publish_paths")),
            ("last_exit_code", st.get("last_exit_code")),
            ("last_reason", st.get("last_reason")),
            ("last_error", st.get("last_error")),
            ("next_scheduled_restart", _ts(st.get("next_scheduled_restart"))),
            ("cli", cfg.get("cli") or ""),
        ]
        return pairs

    def _detail(self, args):
        if args and args[0].lower() in ("monitor", "mon"):
            if len(args) < 2:
                raise CliError("detail monitor N")
            i, m = self._mon(args[1])
            return f"[모니터 {i}]\n" + _kv([("id", m["id"])] + [(k, m.get(k)) for k in MON_KEYS])
        if not args:
            raise CliError("detail N  (N = 'list encoders' 의 번호)")
        i, enc = self._enc(args[0])
        return f"[인코더 {i}]\n" + _kv(self._detail_pairs(enc))

    # ----------------------------------------------------------------- get
    def _get(self, args):
        if not args:
            raise CliError("get status|log|logs|system|config|paths|monitor ... 또는 get N <key>")
        what = args[0].lower()
        if what == "status":
            return self._get_status(args[1:])
        if what == "log":
            if len(args) < 2:
                raise CliError("get log N [lines]")
            _, enc = self._enc(args[1])
            n = self._int(args[2] if len(args) > 2 else "40", "lines", 1, 5000)
            lines = enc.fflog.read_tail(n)
            return "\n".join(lines[-n:]) if lines else "(로그 없음)"
        if what == "logs":
            n = self._int(args[1] if len(args) > 1 else "40", "lines", 1, 1000)
            recs = ring().since(0)[-n:]
            return "\n".join(f"{_ts(r['ts'])} {r['level']:<7} [{r['name']}] {r['msg']}" for r in recs) or "(로그 없음)"
        if what == "system":
            return self._system()
        if what in ("paths", "path"):
            return self._paths()
        if what == "config":
            return self._get_config(args[1:])
        if what in ("monitor", "mon"):
            if len(args) < 3:
                raise CliError("get monitor N <key>")
            _, m = self._mon(args[1])
            if args[2] not in m:
                raise CliError(f"모니터 key 없음: {args[2]}  ({', '.join(MON_KEYS)})")
            return _fmt(m[args[2]])
        if what in ("encoders", "encoder", "monitors"):
            return self._list([what])
        # get N <key>
        _, enc = self._enc(args[0])
        if len(args) < 2:
            return self._detail([args[0]])
        key = args[1]
        if key == "cli":
            return enc.cfg.get("cli") or ""
        pairs = dict(self._detail_pairs(enc))
        if key not in pairs:
            raise CliError(f"key 없음: {key}  ('detail {args[0]}' 로 key 확인)")
        return _fmt(pairs[key])

    def _get_status(self, args):
        doc = self.status_doc()
        if args and args[0].lower() != "all":
            i, enc = self._enc(args[0])
            st = self._enc_status(enc.id)
            keys = ("state", "health", "issues", "pid", "uptime", "restarts", "fps", "speed", "bitrate",
                    "drop_frames", "dup_frames", "cpu_percent", "video", "audio", "publish_paths",
                    "last_reason", "last_error", "next_scheduled_restart")
            pairs = dict(self._detail_pairs(enc))
            return f"[인코더 {i}] {enc.title}\n" + _kv([(k, pairs[k]) for k in keys]) if st else "상태 없음"
        s, c = doc["system"], doc["summary"]["encoders"]
        head = [
            f"health  : {doc['health'].upper()}",
            f"host    : {s['hostname']}  ·  {s['time']}  ·  backend up {_dur(s['backend_uptime_sec'])}",
            f"system  : CPU {_fmt(s['cpu_percent'])}%  ·  RAM avail {_fmt(s['mem_available_percent'])}%"
            f"  ·  disk {_fmt(s['disk_used_percent'])}%  ·  ffmpeg {_fmt(s['ffmpeg_processes'])}",
            f"mediamtx: {'OK' if doc['mediamtx']['ok'] else 'DOWN'}  ·  paths {doc['mediamtx']['paths_ready']}"
            f"/{doc['mediamtx']['paths_total']}  ·  readers {doc['mediamtx']['readers_total']}",
            "encoders: " + ", ".join(f"{k} {v}" for k, v in c.items()),
            "",
        ]
        rows = [[i, e["title"], e["state"], e["health"], e["fps"], e["speed"], _bps(e["bitrate_bps"]),
                 e["restarts"], _dur(e["uptime_sec"])] for i, e in enumerate(doc["encoders"], 1)]
        out = "\n".join(head) + _table(["#", "title", "state", "health", "fps", "speed", "bitrate", "restarts", "uptime"], rows)
        if doc["issues"]:
            out += "\n\nissues:\n" + "\n".join(f"  [{i['level']}] {i['message']}" for i in doc["issues"])
        return out

    def _system(self):
        s = self.sysinfo.snapshot or {}
        pairs = [
            ("hostname", s.get("hostname")),
            ("cpu", f"{s.get('cpu_model')} ({s.get('cpu_cores')}C/{s.get('cpu_threads')}T)"),
            ("cpu_percent", s.get("cpu_percent")),
            ("load", [round(x, 2) for x in s.get("load") or []]),
            ("memory", f"{_bytes(s.get('mem_available'))} available / {_bytes(s.get('mem_total'))}"),
            ("ffmpeg_processes", s.get("ffmpeg_count")),
        ]
        for g in s.get("gpus") or []:
            pairs.append((f"gpu{g.get('index')}", f"{g.get('name')}  gpu {_fmt(g.get('util_gpu'))}%  enc {_fmt(g.get('util_enc'))}%"
                                                f"  dec {_fmt(g.get('util_dec'))}%  {_fmt(g.get('temp_c'))}°C  {_fmt(g.get('power_w'))}W"))
        for n in s.get("nics") or []:
            pairs.append((f"nic {n.get('name')}", f"{_fmt(n.get('ips'))}  {_fmt(n.get('speed_mbps'))} Mb/s"
                                                  f"  rx {_bps(n.get('rx_bps')) or '0'}  tx {_bps(n.get('tx_bps')) or '0'}"))
        return _kv(pairs)

    def _get_config(self, args):
        sections = [k for k in self.store.data if k not in CONFIG_SECTIONS_RO]
        if not args:
            return "섹션: " + ", ".join(sections) + "\n'get config <section>' 또는 'get config <section.key>'"
        key = args[0]
        sec = key.split(".")[0]
        if sec not in sections:
            raise CliError(f"설정 섹션 없음: {sec}  ({', '.join(sections)})")
        try:
            v = _lookup({sec: self.store.section(sec)}, key)
        except KeyError:
            raise CliError(f"설정 key 없음: {key}") from None
        if isinstance(v, dict):
            return _kv([(f"{key}.{k}", x) for k, x in _flatten(v)])
        return _fmt(v)

    # ----------------------------------------------------------------- set
    def _set(self, line, value):
        parts = line.split(None, 3)   # set, target, key, value (rest of the line, spaces kept)
        if len(parts) < 3:
            raise CliError("set N <key> <value> | set N start|stop|restart|clearlog | set monitor N <key> <value>"
                           " | set config <sec.key> <value>")
        target = parts[1].lower()
        if target in ("config", "monitor", "mon"):
            parts = line.split(None, 4) if target != "config" else parts
            if target == "config":
                raw = value if value is not None else (parts[3] if len(parts) > 3 else None)
                return self._set_config(parts[2], raw)
            raw = value if value is not None else (parts[4] if len(parts) > 4 else None)
            if len(parts) < 4:
                raise CliError("set monitor N <key> <value>")
            return self._set_monitor(parts[2], parts[3], raw)
        key = parts[2]
        if key.lower() in ("start", "stop", "restart"):
            return self._action(parts[1], key.lower())
        if key.lower() == "clearlog":
            _, enc = self._enc(parts[1])
            enc.fflog.clear()
            log.info("[%s] fflog cleared (cli)", enc.title)
            return "ffmpeg 로그를 비웠습니다"
        raw = value if value is not None else (parts[3] if len(parts) > 3 else None)
        if raw is None:
            raise CliError(f"값이 없습니다: set {parts[1]} {key} <value>")
        i, enc = self._enc(parts[1])
        if key not in ENC_KEYS:
            raise CliError(f"변경할 수 없는 key: {key}\n가능한 key: {', '.join(ENC_KEYS)}")
        new = _parse_value(raw, _lookup(enc.cfg, key), key) if key != "cli" else raw.strip()
        if key == "scheduled_restart.mode" and new not in ("daily", "interval"):
            raise CliError("scheduled_restart.mode: daily 또는 interval")
        if key == "scheduled_restart.time" and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", new):
            raise CliError("scheduled_restart.time: HH:MM (예: 04:00)")
        if key == "scheduled_restart.interval_min" and new < 1:
            raise CliError("scheduled_restart.interval_min: 1 이상")
        if key == "title" and not new:
            raise CliError("title 은 비울 수 없습니다")
        old = _lookup(enc.cfg, key)
        self.manager.update(enc.id, _nest(key, new))
        msg = f"[인코더 {i}] {key}: {_fmt(old) if key != 'cli' else '(이전 명령줄)'} → {_fmt(new) if key != 'cli' else '(새 명령줄)'}"
        if key == "cli" and enc.running():
            msg += f"\n실행 중입니다. 적용하려면: set {i} restart"
        return msg

    def _action(self, ref, action):
        targets = list(enumerate(self.manager.all(), 1)) if ref.lower() == "all" else [self._enc(ref)]
        if not targets:
            raise CliError("인코더 없음")
        results = {}

        def do(i, enc):
            if action == "start":
                if not enc.cfg.get("cli", "").strip():
                    results[i] = "명령줄이 비어 있어 시작할 수 없음"
                    return
                results[i] = "시작" if enc.start() else "이미 실행 중"
            elif action == "stop":
                results[i] = "정지" if enc.stop() else "이미 정지 상태"
            else:
                enc.restart()
                results[i] = "재시작 요청"

        threads = [threading.Thread(target=do, args=t) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return "\n".join(f"[인코더 {i}] {enc.title}: {results.get(i, '?')}" for i, enc in targets)

    def _set_monitor(self, ref, key, raw):
        if raw is None:
            raise CliError(f"값이 없습니다: set monitor {ref} {key} <value>")
        i, m = self._mon(ref)
        if key not in MON_KEYS:
            raise CliError(f"변경할 수 없는 key: {key}  ({', '.join(MON_KEYS)})")
        new = _parse_value(raw, m.get(key), key)
        self.store.update_item("monitors", m["id"], {key: new})
        log.info("monitor updated: %s (%s, cli)", m.get("title"), key)
        return f"[모니터 {i}] {key}: {_fmt(m.get(key))} → {_fmt(new)}  (브라우저 새로고침 후 반영)"

    def _set_config(self, key, raw):
        sec = key.split(".")[0]
        if sec in CONFIG_SECTIONS_RO or sec not in self.store.data or "." not in key:
            raise CliError("set config <section.key> <value>  ('get config' 로 섹션 확인)")
        if raw is None:
            raise CliError(f"값이 없습니다: set config {key} <value>")
        try:
            old = _lookup({sec: self.store.section(sec)}, key)
        except KeyError:
            raise CliError(f"설정 key 없음: {key}") from None
        new = _parse_value(raw, old, key)
        self.store.set_value(key, new)
        log.info("config changed (cli): %s = %r", key, new)
        note = "  (backend 재시작 후 적용)" if sec in ("server", "logging") else ""
        return f"{key}: {_fmt(old)} → {_fmt(new)}{note}"

    # ------------------------------------------------------ add/delete/move
    def _add(self, args):
        if not args or args[0].lower() not in ("encoder", "enc"):
            raise CliError("add encoder [title]")
        title = " ".join(args[1:]) or "New encoder"
        cfg = self.manager.add({"title": title})
        i = len(self.manager.all())
        return f"[인코더 {i}] 추가됨: {cfg['title']} ({cfg['id']})\n다음: set {i} cli <ffmpeg ...>  또는  edit {i}"

    def _delete(self, args):
        if args and args[0].lower() in ("encoder", "enc"):
            args = args[1:]
        if not args:
            raise CliError("delete N yes")
        i, enc = self._enc(args[0])
        if len(args) < 2 or args[1].lower() != "yes":
            return f"[인코더 {i}] {enc.title} 을(를) 삭제하려면: delete {args[0]} yes"
        self.manager.remove(enc.id)
        return f"[인코더 {i}] {enc.title} 삭제됨"

    def _move(self, args):
        if len(args) < 2:
            raise CliError("move N M")
        i, enc = self._enc(args[0])
        ids = [e.id for e in self.manager.all()]
        j = self._int(args[1], "M", 1, len(ids))
        ids.remove(enc.id)
        ids.insert(j - 1, enc.id)
        self.store.reorder("encoders", ids)
        log.info("encoder order changed (cli)")
        return f"{enc.title}: {i} → {j}\n" + self._list(["encoders"])

    @staticmethod
    def _int(raw, name, lo, hi):
        try:
            v = int(raw)
        except ValueError:
            raise CliError(f"{name}: 정수가 필요합니다") from None
        if not lo <= v <= hi:
            raise CliError(f"{name}: {lo}~{hi}")
        return v


# ----------------------------------------------------------------- client
CLIENT_SCRIPT = r'''#!/usr/bin/env bash
# mediarouter CLI client (served by GET /cli). Usage:
#   curl -s __BASE__/cli | bash                       interactive
#   curl -s __BASE__/cli | bash -s -- get status all  one command, then exit
MR_URL="${MR_URL:-__BASE__}"

mr_run() {   # $1 command line, $2 optional value (multi-line text for set)
  local args=(-sS --max-time 120 --data-urlencode "cmd=$1")
  [ $# -ge 2 ] && args+=(--data-urlencode "value=$2")
  curl "${args[@]}" "$MR_URL/cli/run" || echo "backend 에 접속할 수 없습니다: $MR_URL"
}

mr_edit() {  # edit an encoder's ffmpeg command line in $EDITOR
  local tmp code ed
  [ -n "$1" ] || { echo "edit N"; return; }
  tmp=$(mktemp "${TMPDIR:-/tmp}/mediarouter-cli.XXXXXX") || return
  code=$(curl -sS --max-time 30 --data-urlencode "cmd=get $1 cli" -o "$tmp" -w '%{http_code}' "$MR_URL/cli/run")
  if [ "$code" != 200 ]; then cat "$tmp"; rm -f "$tmp"; return; fi
  cp "$tmp" "$tmp.orig"
  ed="${VISUAL:-${EDITOR:-}}"
  [ -n "$ed" ] || { command -v nano >/dev/null && ed=nano || ed=vi; }
  $ed "$tmp" </dev/tty >/dev/tty
  if cmp -s "$tmp" "$tmp.orig"; then echo "변경 없음"
  else mr_run "set $1 cli" "$(cat "$tmp")"; fi
  rm -f "$tmp" "$tmp.orig"
}

mr_main() {
  local line
  if [ $# -gt 0 ]; then mr_run "$*"; return; fi
  echo "mediarouter CLI — $MR_URL"
  mr_run help
  HISTFILE="$HOME/.mediarouter_cli_history"; HISTSIZE=1000
  set -o history; history -r "$HISTFILE" 2>/dev/null
  while IFS= read -r -e -p "mediarouter> " line; do
    [ -n "${line// }" ] || continue
    history -s -- "$line"; history -a "$HISTFILE" 2>/dev/null
    case "$line" in
      quit|exit|q) break ;;
      edit\ *) mr_edit "${line#edit }" ;;
      clear) clear ;;
      *) mr_run "$line" ;;
    esac
  done
  echo
}

if [ $# -gt 0 ]; then mr_main "$@"
elif [ -r /dev/tty ]; then mr_main </dev/tty
else echo "터미널이 없습니다. 한 명령만 실행: curl -s $MR_URL/cli | bash -s -- get status all"; fi
'''

HTML_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>mediarouter CLI</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--muted:#5b6475;--card:#fff;--line:#d9dde5;--code:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e4e7ec;--muted:#9aa3b2;--card:#1c2027;--line:#2c323c;--code:#242a33}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
p{color:var(--muted);margin:6px 0}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:12px;overflow-x:auto;
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre}
a{color:inherit}
</style></head><body><main>
<h1>mediarouter CLI</h1>
<p>GUI 를 쓸 수 없는 환경(ssh 터미널 등)에서 인코더 상태 확인과 설정 변경을 하는 텍스트 명령 셸입니다.
이 주소는 브라우저용이 아니라 터미널에서 실행합니다.</p>

<h2>대화형 실행</h2>
<pre>curl -s __BASE__/cli | bash</pre>

<h2>명령 하나만 실행 (스크립트 · cron)</h2>
<pre>curl -s __BASE__/cli | bash -s -- get status all
curl -s __BASE__/cli | bash -s -- set 1 restart
curl -s --data-urlencode 'cmd=list encoders' __BASE__/cli/run</pre>

<h2>명령 목록</h2>
<pre>__HELP__</pre>
<p>ffmpeg helper(인코더 마법사)는 CLI 로 제공하지 않습니다. 명령줄은 <code>set N cli ...</code> 또는 <code>edit N</code>($EDITOR) 로 직접 편집합니다.
<a href="/">대시보드로 돌아가기</a></p>
</main></body></html>
"""


def client_script(base):
    return CLIENT_SCRIPT.replace("__BASE__", base)


def html_page(base):
    return HTML_PAGE.replace("__BASE__", html.escape(base)).replace("__HELP__", html.escape(HELP.rstrip()))
