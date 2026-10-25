"""Backend logging module.

Root logger -> console + rotating file + in-memory ring buffer.
The ring buffer (skill §6) lets the web UI show the most recent N records
without any separate log shipping; every record gets a monotonically
increasing sequence number so the UI can fetch only what it has not seen.
"""
import logging
import logging.handlers
import os
import threading
from collections import deque

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RingBufferHandler(logging.Handler):
    def __init__(self, maxlen=1000):
        super().__init__()
        self._buf = deque(maxlen=maxlen)
        self._seq = 0
        self._lock2 = threading.Lock()

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self._lock2:
            self._seq += 1
            self._buf.append({
                "seq": self._seq,
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            })

    @property
    def last_seq(self):
        return self._seq

    def since(self, seq=0):
        with self._lock2:
            return [r for r in self._buf if r["seq"] > seq]


_ring = None


def setup_logging(cfg, base_dir):
    """cfg: the "logging" section of config.json."""
    global _ring
    log_dir = os.path.join(base_dir, cfg.get("dir", "logs"))
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "backend.log"),
        maxBytes=int(float(cfg.get("max_size_mb", 10)) * 1024 * 1024),
        backupCount=int(cfg.get("backup_count", 5)),
        encoding="utf-8",
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    _ring = RingBufferHandler(int(cfg.get("ring_size", 1000)))
    # the UI shows timestamp/level in separate columns; keep the message plain
    _ring.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(_ring)

    # uvicorn access log is noisy (SSE + polling); keep it out of the ring buffer
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return _ring


def ring():
    return _ring


def get_logger(name):
    return logging.getLogger(name)
