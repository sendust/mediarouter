"""Per-encoder ffmpeg log file (our own FFREPORT equivalent) + ffreport watchdog.

ffmpeg's FFREPORT opens its file with fopen("w") and keeps the offset, so it
cannot be truncated/rotated from outside while ffmpeg runs. Instead the
encoder's stderr reader writes every line here, and the ffreport watchdog
thread rotates the file when it grows beyond max_size_mb.
"""
import os
import re
import threading
import time
from collections import deque

from .logger import get_logger

log = get_logger("ffreport")


class FFLog:
    def __init__(self, path, backup_count=3, tail_lines=300):
        self.path = path
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self._fh = None
        self.tail = deque(maxlen=tail_lines)
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _open(self):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")

    def write(self, line):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} {line}"
        self.tail.append(text)
        with self._lock:
            try:
                self._open()
                self._fh.write(text + "\n")
                self._fh.flush()
            except OSError as e:
                log.warning("fflog write failed %s: %s", self.path, e)

    def mark(self, text):
        self.write(f"===== {text} =====")

    def close(self):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None

    def size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def total_size(self):
        total = self.size()
        for i in range(1, self.backup_count + 1):
            try:
                total += os.path.getsize(f"{self.path}.{i}")
            except OSError:
                pass
        return total

    def rotate_if_needed(self, max_bytes):
        if self.size() <= max_bytes:
            return False
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None
            for i in range(self.backup_count, 0, -1):
                src = self.path if i == 1 else f"{self.path}.{i - 1}"
                dst = f"{self.path}.{i}"
                if os.path.exists(src):
                    os.replace(src, dst)
            if self.backup_count <= 0 and os.path.exists(self.path):
                os.remove(self.path)
        return True

    def read_tail(self, lines=300):
        """Tail of the file on disk (survives backend restarts, unlike self.tail)."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                block = min(end, max(64 * 1024, lines * 200))
                f.seek(end - block)
                data = f.read().decode("utf-8", errors="replace")
            return data.splitlines()[-lines:]
        except OSError:
            return list(self.tail)[-lines:]

    def clear(self):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None
            for p in [self.path] + [f"{self.path}.{i}" for i in range(1, self.backup_count + 1)]:
                if os.path.exists(p):
                    os.remove(p)
        self.tail.clear()


class FFReportWatchdog(threading.Thread):
    """Rotates every encoder's fflog when it exceeds the configured size."""

    def __init__(self, manager, cfg):
        super().__init__(name="ffreport-watchdog", daemon=True)
        self.manager = manager
        self.cfg = cfg
        self._stop = threading.Event()

    def run(self):
        log.info("ffreport watchdog started (max %.1f MB, keep %d)",
                 float(self.cfg["max_size_mb"]), int(self.cfg["backup_count"]))
        while not self._stop.wait(float(self.cfg["check_interval_sec"])):
            max_bytes = int(float(self.cfg["max_size_mb"]) * 1024 * 1024)
            for enc in self.manager.all():
                try:
                    if enc.fflog.rotate_if_needed(max_bytes):
                        log.info("[%s] fflog rotated (> %.1f MB)", enc.title, float(self.cfg["max_size_mb"]))
                except OSError as e:
                    log.warning("[%s] fflog rotate failed: %s", enc.title, e)

    def stop(self):
        self._stop.set()


def compile_patterns(patterns):
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning("invalid restart_on_patterns entry %r: %s", p, e)
    return out
