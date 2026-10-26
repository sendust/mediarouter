"""System status collector (1 s tick)."""
import os
import shutil
import socket
import subprocess
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


_GPU_FIELDS = ("index", "name", "util_gpu", "util_enc", "util_dec", "temp_c", "power_w",
               "power_limit_w", "mem_used_mib", "mem_total_mib")
_GPU_QUERY = ("index,name,utilization.gpu,utilization.encoder,utilization.decoder,temperature.gpu,"
              "power.draw,power.limit,memory.used,memory.total")


def _num(v):
    try:
        return float(v)
    except ValueError:
        return None  # "[N/A]" / "[Not Supported]"


def _gpus(smi):
    """NVIDIA GPUs via nvidia-smi: utilization (gpu / NVENC / NVDEC), temperature, power, VRAM."""
    r = subprocess.run([smi, f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=3)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"rc={r.returncode}")
    out = []
    for line in r.stdout.strip().splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) != len(_GPU_FIELDS):
            continue
        g = dict(zip(_GPU_FIELDS, vals))
        for k in _GPU_FIELDS[2:]:
            g[k] = _num(g[k])
        g["index"] = int(g["index"])
        out.append(g)
    return out


class SysInfo(threading.Thread):
    GPU_PERIOD = 2.0   # nvidia-smi is a process spawn: every other tick is plenty
    GPU_RETRY = 60.0   # after a failure (driver reload etc.)

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
        self._smi = shutil.which("nvidia-smi")  # nvidia systems only; otherwise no GPU section
        self._gpus = []
        self._gpu_next = 0.0
        self._gpu_err = None

    def _collect_gpus(self, now):
        if not self._smi or now < self._gpu_next:
            return
        try:
            self._gpus = _gpus(self._smi)
            self._gpu_next = now + self.GPU_PERIOD
            if self._gpu_err:
                log.info("nvidia-smi OK again")
            self._gpu_err = None
        except Exception as e:
            self._gpus = []
            self._gpu_next = now + self.GPU_RETRY
            if str(e) != self._gpu_err:
                log.warning("nvidia-smi failed: %s (retry in %.0fs)", e, self.GPU_RETRY)
            self._gpu_err = str(e)

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
        self._collect_gpus(now)
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
            "gpus": self._gpus,
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
