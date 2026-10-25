"""System status collector (1 s tick)."""
import os
import socket
import threading
import time

import psutil

from .logger import get_logger

log = get_logger("sysinfo")


def _cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return os.uname().machine


class SysInfo(threading.Thread):
    def __init__(self):
        super().__init__(name="sysinfo", daemon=True)
        self._stop = threading.Event()
        self.static = {
            "hostname": socket.gethostname(),
            "cpu_model": _cpu_model(),
            "cpu_cores": psutil.cpu_count(logical=False),
            "cpu_threads": psutil.cpu_count(logical=True),
        }
        self.snapshot = dict(self.static)
        self._prev_net = None
        self._prev_ts = None

    def collect(self):
        now = time.time()
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        counters = psutil.net_io_counters(pernic=True)

        nics = []
        for name, st in sorted(stats.items()):
            if name == "lo" or not st.isup:
                continue
            ips = [a.address for a in addrs.get(name, []) if a.family == socket.AF_INET]
            c = counters.get(name)
            rx = tx = 0.0
            if c and self._prev_net and name in self._prev_net:
                dt = max(1e-3, now - self._prev_ts)
                p = self._prev_net[name]
                rx = (c.bytes_recv - p.bytes_recv) * 8 / dt
                tx = (c.bytes_sent - p.bytes_sent) * 8 / dt
            nics.append({"name": name, "ips": ips, "speed_mbps": st.speed, "rx_bps": rx, "tx_bps": tx})
        self._prev_net, self._prev_ts = counters, now

        ff = 0
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").startswith("ffmpeg"):
                ff += 1
        vm = psutil.virtual_memory()
        snap = dict(self.static)
        snap.update({
            "time": time.strftime("%Y/%m/%d %H:%M.%S"),
            "cpu_percent": psutil.cpu_percent(None),
            "load": os.getloadavg(),
            "ffmpeg_count": ff,
            "mem_total": vm.total,
            "mem_free": vm.free,
            "mem_available": vm.available,
            "nics": nics,
        })
        self.snapshot = snap

    def run(self):
        psutil.cpu_percent(None)
        while not self._stop.wait(1.0):
            try:
                self.collect()
            except Exception:
                log.exception("sysinfo collect failed")

    def stop(self):
        self._stop.set()
