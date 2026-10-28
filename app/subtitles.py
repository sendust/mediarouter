"""Timestamp subtitles for segmented recordings.

For every encoder whose CLI writes <dir>/<base>_%Y-%m-%d-%H-%M-%S.<ext> (helper file
output), each new segment file gets <same name>.srt next to it: one cue per second
showing the wall-clock time "YYYY/MM/DD  HH:MM.SS", starting at the time in the file
name and covering -segment_time seconds. An existing non-empty .srt is left alone.
"""
import os
import re
import threading
import time
from datetime import datetime, timedelta

from .encoder import split_cli
from .logger import get_logger

log = get_logger("subtitles")

PATTERN_RE = re.compile(r"^(?:(.*)/)?([^/]*)_%Y-%m-%d-%H-%M-%S\.(\w+)$")
STAMP = "%Y/%m/%d  %H:%M.%S"
CHECK_SEC = 2.0


def recording_target(cli):
    """(dir, base, ext, segment_sec) of a segment-muxer file output in cli, or None."""
    args = split_cli(cli or "")
    for a in args:
        m = PATTERN_RE.match(a)
        if m:
            seg = 3600
            if "-segment_time" in args:
                try:
                    seg = int(float(args[args.index("-segment_time") + 1]))
                except (IndexError, ValueError):
                    pass
            return (m.group(1) or "."), m.group(2), m.group(3), max(1, seg)
    return None


def _ts(sec):
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d},000"


def srt_text(start, seconds):
    cues = []
    for i in range(seconds):
        cues.append(f"{i + 1}\n{_ts(i)} --> {_ts(i + 1)}\n{(start + timedelta(seconds=i)).strftime(STAMP)}\n")
    return "\n".join(cues)


class RecordingSubtitler(threading.Thread):
    def __init__(self, store):
        super().__init__(name="rec-srt", daemon=True)
        self.store = store
        self._stop = threading.Event()
        # only segments written since (backend start - one segment): no backfill of old archives
        self.started = time.time()
        self._failed = set()  # write errors are logged once per file

    def run(self):
        while not self._stop.wait(CHECK_SEC):
            if not self.store.section("recording").get("srt", True):
                continue
            for enc in self.store.items("encoders"):
                t = recording_target(enc.get("cli"))
                if t:
                    try:
                        self.scan(*t)
                    except Exception as e:  # noqa: BLE001 - keep the thread alive
                        log.warning("srt scan %s failed: %s", t[0], e)

    def scan(self, folder, base, ext, seg):
        name_re = re.compile(re.escape(base) + r"_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\." + re.escape(ext) + "$")
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for n in names:
            m = name_re.match(n)
            if not m:
                continue
            path = os.path.join(folder, n)
            srt = os.path.splitext(path)[0] + ".srt"
            if srt in self._failed:
                continue
            try:
                if os.path.getsize(srt) > 0:
                    continue
            except OSError:
                pass  # no srt yet
            try:
                if os.path.getmtime(path) < self.started - seg:
                    continue
            except OSError:
                continue
            start = datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S")
            tmp = os.path.join(folder, "." + os.path.basename(srt) + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(srt_text(start, seg))
                os.replace(tmp, srt)
                log.info("srt created %s (%d s)", srt, seg)
            except OSError as e:
                log.warning("srt write %s failed: %s", srt, e)
                self._failed.add(srt)
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
