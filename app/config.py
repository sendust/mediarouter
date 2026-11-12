"""JSON configuration store (thread-safe, atomic writes)."""
import copy
import json
import os
import shutil
import threading
import uuid

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "ffmpeg": {"path": "ffmpeg", "ffprobe": "ffprobe"},
    "mediamtx": {
        "api_url": "http://127.0.0.1:9997",
        "rtsp_url": "rtsp://127.0.0.1:8554",
        "rtsp_transport": "udp",
        "udp_buffer_size": 4194304,
        "webrtc_port": 8889,
        "webrtc_url": "",
        "poll_interval_sec": 2.0,
    },
    "encoder_watchdog": {
        "poll_interval_sec": 1.0,
        "progress_timeout_sec": 10.0,
        "startup_grace_sec": 15.0,
        "grace_multiplier": 3.0,
        "restart_hold_sec": 3.0,
        "restart_backoff_max_sec": 60.0,
        "stable_run_sec": 60.0,
        "stop_timeout_sec": 5.0,
        "speed_check": True,
        "speed_min": 0.95,
        "speed_window_sec": 30.0,
        "pipe_buffer_bytes": 524288,
        "video_stall_sec": 10.0,   # restart when the video frame count stops while output time advances (0 = off)
    },
    "ffreport_watchdog": {
        "dir": "logs/fflog",
        "loglevel": "info",
        "max_size_mb": 10.0,
        "backup_count": 3,
        "check_interval_sec": 5.0,
        "tail_lines": 300,
        "restart_on_patterns": [],
    },
    "recording": {
        "segment_sec": 3600,   # file output: one file per segment (seconds)
        "default_dir": "",     # folder browser start (empty = backend user's home)
        "srt": True,           # write a 1 s timestamp .srt next to every new segment file
    },
    "alsa": {
        "low_latency": True,   # ALSA inputs via the shared low-latency dsnoop device (app/alsa_lowlat.py)
        "period_frames": 240,  # capture packet size (240 @ 48 kHz = 5 ms; ffmpeg's own default is 85 ms)
        "buffer_frames": 1920, # ALSA ring buffer (40 ms): headroom before an overrun if ffmpeg stalls
    },
    "logging": {
        "dir": "logs",
        "level": "INFO",
        "max_size_mb": 10,
        "backup_count": 5,
        "ring_size": 1000,
    },
    "encoders": [],
    "monitors": [],
}

ENCODER_DEFAULTS = {
    "title": "New encoder",
    "comment": "",
    "cli": "",
    "autostart": False,
    "auto_restart": True,
    "speed_restart": False,
    "scheduled_restart": {"enabled": False, "mode": "daily", "time": "04:00", "interval_min": 1440},
    "helper": None,
}

MONITOR_DEFAULTS = {
    "title": "New monitor",
    "paths": [],
    "muted": True,
    "enabled": True,
}


def _merge(defaults, data):
    out = copy.deepcopy(defaults)
    for k, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class ConfigStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.data = {}
        self.load_messages = []  # (level, text) from load(); logged by main once logging is set up
        self.load()

    def load(self):
        with self._lock:
            raw = {}
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
            self.data = _merge(DEFAULTS, raw)
            self.data["encoders"] = [_merge(ENCODER_DEFAULTS, e) for e in self.data["encoders"]]
            self.data["monitors"] = [_merge(MONITOR_DEFAULTS, m) for m in self.data["monitors"]]
            for e in self.data["encoders"]:
                e.setdefault("id", new_id("enc"))
            for m in self.data["monitors"]:
                m.setdefault("id", new_id("mon"))
            if raw != self.data:
                self._write_back(os.path.exists(self.path))

    def _write_back(self, existed):
        """Write new default keys back so the file documents every option (after an upgrade).
        The previous file is kept as <config>.bak. Failure only warns: defaults still apply in memory."""
        bak = self.path + ".bak"
        if existed:
            try:
                shutil.copy2(self.path, bak)
            except OSError as e:
                self.load_messages.append(("warning", f"config backup {bak} failed: {e}"))
                bak = None
        try:
            self.save()
        except OSError as e:
            self.load_messages.append(("warning", f"config write-back {self.path} failed: {e} "
                                                  "(running with defaults for the missing keys)"))
            return
        self.load_messages.append(("info", f"config {self.path} updated with new default keys"
                                           + (f" (previous file: {bak})" if existed and bak else "")))

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    def section(self, name):
        with self._lock:
            return copy.deepcopy(self.data[name])

    def set_value(self, key, value):
        """Set one dotted key inside a settings section, e.g. "encoder_watchdog.video_stall_sec"."""
        parts = key.split(".")
        with self._lock:
            cur = self.data
            for part in parts[:-1]:
                cur = cur[part]
            cur[parts[-1]] = value
            self.save()

    # ---- list helpers (encoders / monitors) ----
    def items(self, kind):
        with self._lock:
            return copy.deepcopy(self.data[kind])

    def get_item(self, kind, item_id):
        with self._lock:
            for it in self.data[kind]:
                if it["id"] == item_id:
                    return copy.deepcopy(it)
        return None

    def add_item(self, kind, item):
        defaults = ENCODER_DEFAULTS if kind == "encoders" else MONITOR_DEFAULTS
        item = _merge(defaults, item)
        item["id"] = new_id("enc" if kind == "encoders" else "mon")
        with self._lock:
            self.data[kind].append(item)
            self.save()
        return copy.deepcopy(item)

    def update_item(self, kind, item_id, patch):
        with self._lock:
            for i, it in enumerate(self.data[kind]):
                if it["id"] == item_id:
                    patch = {k: v for k, v in patch.items() if k != "id"}
                    self.data[kind][i] = _merge(it, patch)
                    self.save()
                    return copy.deepcopy(self.data[kind][i])
        return None

    def remove_item(self, kind, item_id):
        with self._lock:
            before = len(self.data[kind])
            self.data[kind] = [it for it in self.data[kind] if it["id"] != item_id]
            if len(self.data[kind]) != before:
                self.save()
                return True
        return False

    def reorder(self, kind, ids):
        with self._lock:
            by_id = {it["id"]: it for it in self.data[kind]}
            ordered = [by_id.pop(i) for i in ids if i in by_id]
            ordered.extend(by_id.values())  # anything the client didn't mention keeps its place at the end
            self.data[kind] = ordered
            self.save()
