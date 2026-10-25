"""Encoder registry: owns Encoder objects, keeps them in sync with config.json."""
import os
import signal
import threading

import psutil

from .encoder import ENV_MARK, Encoder
from .logger import get_logger

log = get_logger("manager")


class EncoderManager:
    def __init__(self, store, base_dir):
        self.store = store
        self.base_dir = base_dir
        self._lock = threading.RLock()
        self._enc = {}
        for cfg in store.items("encoders"):
            self._enc[cfg["id"]] = Encoder(cfg, self.app_cfg)

    def app_cfg(self, name):
        if name == "_base_dir":
            return self.base_dir
        return self.store.section(name)

    # --------------------------------------------------------------- lookup
    def all(self):
        order = [e["id"] for e in self.store.items("encoders")]
        with self._lock:
            return [self._enc[i] for i in order if i in self._enc]

    def get(self, enc_id):
        with self._lock:
            return self._enc.get(enc_id)

    # ----------------------------------------------------------------- CRUD
    def add(self, data):
        cfg = self.store.add_item("encoders", data)
        with self._lock:
            self._enc[cfg["id"]] = Encoder(cfg, self.app_cfg)
        log.info("encoder added: %s (%s)", cfg["title"], cfg["id"])
        return cfg

    def update(self, enc_id, patch):
        cfg = self.store.update_item("encoders", enc_id, patch)
        if cfg is None:
            return None
        enc = self.get(enc_id)
        if enc:
            enc.update_cfg(cfg)
        changed = ", ".join(sorted(patch.keys()))
        log.info("encoder updated: %s (%s)", cfg["title"], changed)
        return cfg

    def remove(self, enc_id):
        enc = self.get(enc_id)
        if enc is None:
            return False
        enc.stop("encoder removed")
        enc.fflog.close()
        with self._lock:
            self._enc.pop(enc_id, None)
        self.store.remove_item("encoders", enc_id)
        log.info("encoder removed: %s (%s)", enc.title, enc_id)
        return True

    # ------------------------------------------------------------ lifecycle
    def kill_orphans(self):
        """Kill ffmpeg processes left over from a previous backend instance (env-tagged)."""
        me = os.getpid()
        for p in psutil.process_iter(["pid", "name"]):
            if p.info["pid"] == me or not (p.info["name"] or "").startswith("ffmpeg"):
                continue
            try:
                tag = p.environ().get(ENV_MARK)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            if tag:
                log.warning("killing orphan ffmpeg pid=%d (encoder %s)", p.pid, tag)
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()

    def autostart(self):
        for enc in self.all():
            if enc.cfg.get("autostart"):
                log.info("[%s] autostart", enc.title)
                enc.start()

    def shutdown(self):
        threads = [threading.Thread(target=e.stop, args=("backend shutdown",)) for e in self.all() if e.running()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for e in self.all():
            e.fflog.close()
