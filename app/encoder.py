"""One ffmpeg encoder = one supervisor OS thread (+ 2 pipe-reader threads).

Skill §1 (watchdog / auto-restart) and §9 (pipe buffer) applied to a
threading model: the supervisor thread owns the subprocess for its whole
life, so start/stop/restart only ever signal it (no two code paths touch
self._proc concurrently).
"""
import datetime as dt
import fcntl
import os
import random
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.parse
from collections import deque

import psutil

from .fflog import FFLog, compile_patterns
from .logger import get_logger

log = get_logger("encoder")

ENV_MARK = "MEDIAROUTER_ENCODER_ID"
# SIGINT grace for mp4 +faststart recordings (the moov move rewrites the whole last segment)
MP4_FINALIZE_SEC = 60.0

# options injected after the ffmpeg binary unless the user already has them
INJECT = [
    (("-hide_banner",), ["-hide_banner"]),
    (("-nostdin",), ["-nostdin"]),
    (("-nostats",), ["-nostats"]),
    (("-progress",), ["-progress", "pipe:1"]),
    (("-stats_period",), ["-stats_period", "0.5"]),
]

# ffmpeg stream report lines (stderr), used to show the codecs actually in use
_RE_SECTION = re.compile(r"^(?:\[\w+\]\s*)?(Input|Output) #\d+")
_RE_STREAM = re.compile(r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?: (Video|Audio): ([\w-]+)(.*)")
_RE_ENCODER = re.compile(r"^\s*(?:\[\w+\]\s*)?encoder\s*: Lavc\S*\s+(\S+)")

PROGRESS_KEYS = ("frame", "fps", "bitrate", "total_size", "out_time", "speed",
                 "drop_frames", "dup_frames")


def split_cli(cli):
    text = cli.replace("\\\r\n", " ").replace("\\\n", " ")
    return shlex.split(text, comments=False, posix=True)


def source_paths(cli, rtsp_url):
    """mediamtx path names read by the CLI: -i rtsp://<mediamtx host:port>/<path>."""
    mtx = urllib.parse.urlsplit(rtsp_url or "")
    local = {mtx.hostname, "127.0.0.1", "localhost"}
    args = split_cli(cli or "")
    out = []
    for i, a in enumerate(args[:-1]):
        if a != "-i":
            continue
        u = urllib.parse.urlsplit(args[i + 1])
        if u.scheme in ("rtsp", "rtsps") and u.hostname in local and (u.port or 554) == (mtx.port or 554):
            name = u.path.strip("/")
            if name and name not in out:
                out.append(name)
    return out


def build_argv(cli, ffmpeg_path, loglevel):
    args = split_cli(cli)
    if not args:
        raise ValueError("empty command line")
    if os.path.basename(args[0]).startswith("ffmpeg"):
        args = args[1:]
    extra = []
    for keys, opt in INJECT:
        if not any(a in keys for a in args):
            extra += opt
    if not any(a in ("-loglevel", "-v") for a in args):
        extra += ["-loglevel", f"level+{loglevel}"]
    return [ffmpeg_path] + extra + args


def next_schedule(sched, from_ts):
    if not sched or not sched.get("enabled"):
        return None
    if sched.get("mode") == "interval":
        minutes = max(1.0, float(sched.get("interval_min") or 1440))
        return from_ts + minutes * 60
    try:
        hh, mm = (int(x) for x in str(sched.get("time", "04:00")).split(":")[:2])
    except ValueError:
        return None
    base = dt.datetime.fromtimestamp(from_ts)
    target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target.timestamp() <= from_ts + 1:
        target += dt.timedelta(days=1)
    return target.timestamp()


def _set_pipe_size(fileobj, size, title):
    try:
        fcntl.fcntl(fileobj.fileno(), fcntl.F_SETPIPE_SZ, int(size))
    except (OSError, AttributeError, ValueError) as e:
        # non fatal (skill §9): keep running with the default 64KB pipe
        log.warning("[%s] could not enlarge pipe buffer to %s: %s", title, size, e)


class Encoder:
    def __init__(self, cfg, app_cfg):
        """cfg: encoder dict from config.json; app_cfg: callable returning sections."""
        self.cfg = cfg
        self.app_cfg = app_cfg
        rep = app_cfg("ffreport_watchdog")
        path = os.path.join(app_cfg("_base_dir"), rep["dir"], f"{cfg['id']}.log")
        self.fflog = FFLog(path, int(rep["backup_count"]), int(rep["tail_lines"]))

        self._lifecycle = threading.Lock()
        self._thread = None
        self._stop_evt = threading.Event()
        self._kick_evt = threading.Event()   # wakes the supervisor for a manual restart
        self._kick_reason = None
        self._proc = None
        self._ps = None

        self.state = "stopped"
        self.pid = None
        self.started_at = None
        self.restarts = 0
        self.last_exit_code = None
        self.last_reason = ""
        self.last_error = ""
        self.last_error_ts = None
        self.progress = {}
        self.out_streams = []
        self._section = None
        self._speed_samples = deque()
        self.speed_now = None
        self.slow = False
        self.slow_since = None
        self.next_sched = None
        self._last_advance = 0.0
        self._first_progress = False
        self._advance_key = None
        self._pattern_hit = None
        self._fail_streak = 0
        self._hold_until = None
        self.argv = []

    # ---------------------------------------------------------------- props
    @property
    def id(self):
        return self.cfg["id"]

    @property
    def title(self):
        return self.cfg.get("title") or self.id

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- control
    def start(self):
        with self._lifecycle:
            if self.running():
                return False
            self._stop_evt.clear()
            self._kick_evt.clear()
            self._fail_streak = 0
            self.state = "starting"
            self._thread = threading.Thread(target=self._supervise, name=f"enc-{self.id}", daemon=True)
            self._thread.start()
            log.info("[%s] start requested", self.title)
            return True

    def stop(self, reason="user stop"):
        with self._lifecycle:
            th = self._thread
            if th is None or not th.is_alive():
                self.state = "stopped"
                return False
            self.state = "stopping"
            self.last_reason = reason
            self._stop_evt.set()
            self._kick_evt.set()
        th.join(timeout=float(self.app_cfg("encoder_watchdog")["stop_timeout_sec"]) + 10)
        if th.is_alive():
            log.error("[%s] supervisor thread did not exit", self.title)
        log.info("[%s] stopped (%s)", self.title, reason)
        return True

    def restart(self, reason="user restart"):
        """Restart the ffmpeg process while keeping the supervisor (session) alive."""
        if not self.running():
            return self.start()
        self._kick_reason = reason
        self._kick_evt.set()
        return True

    def update_cfg(self, cfg):
        self.cfg = cfg
        # schedule changes apply immediately to the running session
        if self.started_at and self.state == "running":
            self.next_sched = next_schedule(cfg.get("scheduled_restart"), time.time())

    # ---------------------------------------------------------- supervisor
    def _supervise(self):
        wd = self.app_cfg("encoder_watchdog")
        while not self._stop_evt.is_set():
            if not self._wait_paths():
                break
            reason, rc, ran_for = self._run_once()
            self.last_exit_code = rc
            if self._stop_evt.is_set():
                break

            if reason.startswith("scheduled") or reason.startswith("manual"):
                hold = 1.0
                self._fail_streak = 0
            elif not self.cfg.get("auto_restart", True):
                self.state = "error" if rc not in (0, None) or reason != "exit" else "finished"
                self.last_reason = reason
                log.warning("[%s] process ended (%s, rc=%s); auto-restart disabled", self.title, reason, rc)
                break
            else:
                wd = self.app_cfg("encoder_watchdog")
                self._fail_streak = 1 if ran_for >= float(wd["stable_run_sec"]) else self._fail_streak + 1
                hold = min(float(wd["restart_hold_sec"]) * (2 ** (self._fail_streak - 1)),
                           float(wd["restart_backoff_max_sec"]))

            self.state = "restarting"
            self.last_reason = reason
            log.warning("[%s] >>> AUTO-RESTART in %.1fs  reason=%s rc=%s", self.title, hold, reason, rc)
            self.fflog.mark(f"restart in {hold:.1f}s ({reason})")
            self._hold_until = time.time() + hold
            # a STOP pressed during the hold cancels the restart
            if self._stop_evt.wait(hold):
                break
            self._hold_until = None
            self.restarts += 1

        self._hold_until = None
        self.pid = None
        self._ps = None
        if self.state not in ("error", "finished"):
            self.state = "stopped"
        self.started_at = None
        self.next_sched = None

    def _wait_paths(self):
        """Hold the (re)start until every mediamtx source path of the CLI is ready.
        Unknown state (mediamtx API down / not polled yet) does not block. False = stopped meanwhile."""
        ready = self.app_cfg("_path_ready")
        paths = source_paths(self.cfg.get("cli"), self.app_cfg("mediamtx").get("rtsp_url")) if ready else []
        t0, shown = None, None
        while paths:
            missing = [p for p in paths if ready(p) is False]
            if not missing:
                break
            if missing != shown:
                shown = missing
                self.state = "waiting"
                self.last_reason = "waiting for mediamtx path " + ", ".join(missing)
                if t0 is None:
                    t0 = time.time()
                    log.warning("[%s] %s (not ready)", self.title, self.last_reason)
                    self.fflog.mark(self.last_reason)
            if self._stop_evt.wait(1.0):
                return False
        if t0 is not None:
            self._kick_evt.clear()  # a manual restart pressed while waiting is already satisfied
            self._fail_streak = 0  # a missing source is not this encoder's failure: no long backoff
            log.info("[%s] mediamtx path %s ready after %.0fs, starting", self.title, ", ".join(paths), time.time() - t0)
            self.fflog.mark("mediamtx path ready, starting")
            self.last_reason = "mediamtx path ready"
        return True

    def _spawn(self):
        ff = self.app_cfg("ffmpeg")
        rep = self.app_cfg("ffreport_watchdog")
        wd = self.app_cfg("encoder_watchdog")
        self.argv = build_argv(self.cfg.get("cli", ""), ff["path"], rep["loglevel"])
        env = dict(os.environ)
        env[ENV_MARK] = self.id
        proc = subprocess.Popen(
            self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, start_new_session=True, bufsize=0,
        )
        _set_pipe_size(proc.stdout, wd["pipe_buffer_bytes"], self.title)
        _set_pipe_size(proc.stderr, wd["pipe_buffer_bytes"], self.title)
        return proc

    def _run_once(self):
        """Run one ffmpeg process until it exits or must be killed. Returns (reason, rc, seconds)."""
        wd = self.app_cfg("encoder_watchdog")
        self._patterns = compile_patterns(self.app_cfg("ffreport_watchdog").get("restart_on_patterns"))
        self.progress = {}
        self.out_streams = []
        self._section = None
        self._speed_samples = deque()
        self.speed_now = None
        self.slow = False
        self.slow_since = None
        self._first_progress = False
        self._advance_key = None
        self._pattern_hit = None
        self.last_error = ""
        t0 = time.time()
        self._last_advance = t0

        try:
            proc = self._spawn()
        except (OSError, ValueError) as e:
            self.last_error = str(e)
            log.error("[%s] spawn failed: %s", self.title, e)
            self.fflog.mark(f"spawn failed: {e}")
            return "spawn failed", None, 0.0

        self._proc = proc
        self.pid = proc.pid
        try:
            self._ps = psutil.Process(proc.pid)
            self._ps.cpu_percent(None)
        except psutil.Error:
            self._ps = None
        self.started_at = t0
        self.state = "running"
        self.next_sched = next_schedule(self.cfg.get("scheduled_restart"), t0)
        self.fflog.mark("start pid=%d: %s" % (proc.pid, shlex.join(self.argv)))
        log.info("[%s] ffmpeg started pid=%d", self.title, proc.pid)

        readers = [
            threading.Thread(target=self._read_progress, args=(proc.stdout,), name=f"enc-{self.id}-out", daemon=True),
            threading.Thread(target=self._read_stderr, args=(proc.stderr,), name=f"enc-{self.id}-err", daemon=True),
        ]
        for r in readers:
            r.start()

        reason = "exit"
        while True:
            # jittered poll so N encoders don't tick in lockstep
            interval = float(wd["poll_interval_sec"])
            if self._kick_evt.wait(random.uniform(interval * 0.5, interval)):
                self._kick_evt.clear()
                reason = "stop" if self._stop_evt.is_set() else f"manual ({self._kick_reason or 'restart'})"
                self._kick_reason = None
                break
            if proc.poll() is not None:
                reason = "exit"
                break
            now = time.time()
            wd = self.app_cfg("encoder_watchdog")
            in_grace = (not self._first_progress) or (now - t0 < float(wd["startup_grace_sec"]))
            timeout = float(wd["progress_timeout_sec"]) * (float(wd["grace_multiplier"]) if in_grace else 1.0)
            if now - self._last_advance > timeout:
                reason = f"watchdog: no progress for {now - self._last_advance:.1f}s"
                log.error("[%s] [WATCHDOG] no progress for %.1fs", self.title, now - self._last_advance)
                break
            if self._pattern_hit:
                reason = f"fflog pattern: {self._pattern_hit}"
                log.error("[%s] [FFREPORT] %s", self.title, reason)
                break
            if self._check_speed(now, t0, wd):
                reason = f"speed watchdog: {self.speed_now:.2f}x < {float(wd['speed_min']):.2f}x"
                log.error("[%s] [SPEED] restarting: %.2fx below %.2fx for %.0fs", self.title,
                          self.speed_now, float(wd["speed_min"]), float(wd["speed_window_sec"]))
                break
            if self.next_sched and now >= self.next_sched:
                reason = "scheduled restart"
                log.info("[%s] [SCHEDULE] scheduled restart", self.title)
                break

        if reason != "exit":
            self.state = "stopping" if reason == "stop" else "restarting"
        rc = self._terminate(proc)
        for r in readers:
            r.join(timeout=2)
        self._proc = None
        self.pid = None
        self._ps = None
        ran = time.time() - t0
        self.fflog.mark(f"exit rc={rc} after {ran:.1f}s ({reason})")
        if reason == "exit":
            log.warning("[%s] ffmpeg exited rc=%s after %.1fs %s", self.title, rc, ran,
                        f"last error: {self.last_error}" if self.last_error else "")
        return reason, rc, ran

    def _terminate(self, proc):
        if proc.poll() is not None:
            return proc.wait()
        timeout = float(self.app_cfg("encoder_watchdog")["stop_timeout_sec"])
        if "faststart" in (self.cfg.get("cli") or ""):
            # mp4 +faststart rewrites the whole file on close: give the last segment time to finish
            timeout = max(timeout, MP4_FINALIZE_SEC)
        for sig, wait in ((signal.SIGINT, timeout), (signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                proc.send_signal(sig)
            try:
                return proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                log.warning("[%s] pid %d ignored %s", self.title, proc.pid, sig.name)
        return proc.wait()

    # -------------------------------------------------------------- readers
    def _read_progress(self, pipe):
        block = {}
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k != "progress":
                block[k.strip()] = v.strip()
                continue
            snap = {key: block.get(key) for key in PROGRESS_KEYS if key in block}
            self.progress = snap
            try:
                us = int(block.get("out_time_us") or "")
                now = time.time()
                self._speed_samples.append((now, us))
                horizon = now - float(self.app_cfg("encoder_watchdog").get("speed_window_sec", 30)) - 5
                while self._speed_samples and self._speed_samples[0][0] < horizon:
                    self._speed_samples.popleft()
            except ValueError:
                pass  # out_time_us=N/A before the first packet
            key = (block.get("out_time_us"), block.get("total_size"), block.get("frame"))
            if key != self._advance_key:
                self._advance_key = key
                self._last_advance = time.time()
                self._first_progress = True
            block = {}
        pipe.close()

    def _check_speed(self, now, t0, wd):
        """Realtime check over the last speed_window_sec (ffmpeg's own speed= is a
        since-start average and reacts far too slowly). Returns True to restart."""
        if not wd.get("speed_check", True):
            self.speed_now, self.slow = None, False
            return False
        window = float(wd.get("speed_window_sec", 30))
        samples = [x for x in list(self._speed_samples) if x[0] >= now - window]
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < min(5.0, window):
            return False
        (ta, ua), (tb, ub) = samples[0], samples[-1]
        self.speed_now = (ub - ua) / 1e6 / max(1e-3, tb - ta)
        full = tb - ta >= window * 0.9 and now - t0 >= float(wd["startup_grace_sec"])
        if not full:
            return False
        if self.speed_now < float(wd.get("speed_min", 0.95)):
            if not self.slow:
                self.slow, self.slow_since = True, now
                log.warning("[%s] [SPEED] running below realtime: %.2fx over the last %.0fs (min %.2fx)",
                            self.title, self.speed_now, window, float(wd["speed_min"]))
            return bool(self.cfg.get("speed_restart"))
        if self.slow:
            log.info("[%s] [SPEED] back to realtime: %.2fx", self.title, self.speed_now)
            self.slow, self.slow_since = False, None
        return False

    def _read_stderr(self, pipe):
        for raw in iter(pipe.readline, b""):
            for line in raw.decode("utf-8", errors="replace").replace("\r", "\n").split("\n"):
                line = line.rstrip()
                if not line:
                    continue
                self.fflog.write(line)
                self._parse_stream_info(line)
                low = line.lower()
                if "[error]" in low or "[fatal]" in low or "[panic]" in low:
                    self.last_error = line[:300]
                    self.last_error_ts = time.time()
                for p in self._patterns:
                    if p.search(line):
                        self._pattern_hit = p.pattern
        pipe.close()

    def _parse_stream_info(self, line):
        body = line.split("] ", 1)[1] if line.startswith("[") and "] " in line else line
        m = _RE_SECTION.match(body.strip())
        if m:
            self._section = m.group(1)
            return
        if body.strip().startswith("Stream mapping"):
            self._section = None
            return
        if self._section != "Output":
            return
        m = _RE_STREAM.search(body)
        if m:
            kind, codec, rest = m.groups()
            info = {"kind": kind.lower(), "codec": codec, "encoder": None}
            res = re.search(r"\b(\d{2,5}x\d{2,5})\b", rest)
            fps = re.search(r"([\d.]+) fps", rest)
            br = re.search(r"(\d+) kb/s", rest)
            hz = re.search(r"(\d+) Hz", rest)
            lay = re.search(r"Hz, ([\w.()]+),", rest)
            info.update(size=res and res.group(1), fps=fps and fps.group(1), kbps=br and br.group(1),
                        hz=hz and hz.group(1), layout=lay and lay.group(1))
            self.out_streams.append(info)
            return
        m = _RE_ENCODER.match(body)
        if m and self.out_streams and self.out_streams[-1]["encoder"] is None:
            self.out_streams[-1]["encoder"] = m.group(1)

    def codecs(self):
        """Distinct output streams, e.g. {'video': [{'name': 'h264_nvenc', ..., 'count': 1}], 'audio': [...]}"""
        out = {"video": [], "audio": []}
        for s in self.out_streams:
            name = s["encoder"] or f"{s['codec']} (copy)"
            key = (name, s.get("size"), s.get("fps"), s.get("kbps"), s.get("layout"), s.get("hz"))
            for e in out[s["kind"]]:
                if e["key"] == key:
                    e["count"] += 1
                    break
            else:
                out[s["kind"]].append({"key": key, "name": name, "size": s.get("size"), "fps": s.get("fps"),
                                       "kbps": s.get("kbps"), "layout": s.get("layout"), "hz": s.get("hz"), "count": 1})
        for lst in out.values():
            for e in lst:
                e.pop("key")
        return out

    # --------------------------------------------------------------- status
    def status(self):
        cpu = rss = None
        if self._ps is not None:
            try:
                cpu = self._ps.cpu_percent(None)
                rss = self._ps.memory_info().rss
            except psutil.Error:
                pass
        now = time.time()
        return {
            "id": self.id,
            "state": self.state,
            "pid": self.pid,
            "uptime": (now - self.started_at) if self.started_at and self.state == "running" else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "last_reason": self.last_reason,
            "last_error": self.last_error,
            "last_error_age": (now - self.last_error_ts) if self.last_error_ts else None,
            "progress": self.progress,
            "codecs": self.codecs(),
            "speed_now": self.speed_now if self.state == "running" else None,
            "slow": self.slow and self.state == "running",
            "slow_for": (now - self.slow_since) if self.slow and self.slow_since else None,
            "progress_age": (now - self._last_advance) if self.state == "running" else None,
            "next_scheduled_restart": self.next_sched,
            "restart_in": (self._hold_until - now) if self._hold_until else None,
            "fflog_size": self.fflog.size(),
            "fflog_total": self.fflog.total_size(),
            "cpu": cpu,
            "rss": rss,
            "argv": shlex.join(self.argv) if self.argv else "",
        }
