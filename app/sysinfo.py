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


def _parse_gpu_line(line):
    vals = [v.strip() for v in line.split(",")]
    if len(vals) != len(_GPU_FIELDS):
        return None
    g = dict(zip(_GPU_FIELDS, vals))
    for k in _GPU_FIELDS[2:]:
        g[k] = _num(g[k])
    try:
        g["index"] = int(g["index"])
    except ValueError:
        return None
    return g


class _GpuLoop:
    """One resident `nvidia-smi --query-gpu ... -lms N` process instead of spawning nvidia-smi
    every tick (each spawn re-initializes NVML: ~18 ms CPU; the loop costs ~1 ms per sample).
    A reader thread parses its CSV lines; GPUs not reported for a while are dropped."""

    def __init__(self, smi, period):
        self.smi = smi
        self.period = period
        self.proc = None
        self._lock = threading.Lock()
        self._gpus = {}        # index -> (ts, dict)
        self._last_text = ""   # last non-CSV line (error message)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.stop()
        with self._lock:
            self._gpus.clear()
            self._last_text = ""
        self.proc = subprocess.Popen(
            [self.smi, f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits",
             f"-lms={int(self.period * 1000)}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        threading.Thread(target=self._read, args=(self.proc,), name="nvidia-smi", daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            g = _parse_gpu_line(line)
            with self._lock:
                if g is None:
                    self._last_text = line
                else:
                    self._gpus[g["index"]] = (time.monotonic(), g)

    def snapshot(self):
        cutoff = time.monotonic() - 3 * self.period - 2
        with self._lock:
            return [g for ts, g in sorted(self._gpus.values(), key=lambda x: x[1]["index"]) if ts >= cutoff]

    def error(self):
        rc = self.proc.poll() if self.proc else None
        with self._lock:
            return self._last_text or f"rc={rc}"

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()


def _count_ffmpeg():
    """Number of processes whose name starts with "ffmpeg". Reads only /proc/<pid>/comm
    (psutil.process_iter parses /proc/<pid>/stat and, for long names, cmdline: ~8x slower)."""
    n = 0
    try:
        it = os.scandir("/proc")
    except OSError:
        return 0
    with it:
        for e in it:
            if not e.name.isdigit():
                continue
            try:
                with open(f"/proc/{e.name}/comm", "rb") as f:
                    if f.read(6) == b"ffmpeg":
                        n += 1
            except OSError:
                pass  # process exited
    return n


class SysInfo(threading.Thread):
    GPU_PERIOD = 2.0   # nvidia-smi loop interval (seconds)
    GPU_RETRY = 60.0   # restart delay after the loop process dies (driver reload etc.)
    FFMPEG_PERIOD = 5.0  # ffmpeg process count interval: a full /proc scan is ~85% of a collect()

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
        smi = shutil.which("nvidia-smi")  # nvidia systems only; otherwise no GPU section
        self._gpu = _GpuLoop(smi, self.GPU_PERIOD) if smi else None
        self._gpus = []
        self._gpu_next = 0.0
        self._gpu_err = None
        self._gpu_started = False
        self._ffmpeg_count = None
        self._ffmpeg_next = 0.0

    def _collect_gpus(self, now):
        loop = self._gpu
        if loop is None:
            return
        if self._gpu_started and not loop.alive():
            err = loop.error()
            loop.stop()
            self._gpu_started = False
            self._gpu_next = now + self.GPU_RETRY
            if err != self._gpu_err:
                log.warning("nvidia-smi failed: %s (retry in %.0fs)", err, self.GPU_RETRY)
            self._gpu_err = err
        if not self._gpu_started and now >= self._gpu_next:
            try:
                loop.start()
                self._gpu_started = True
            except OSError as e:
                self._gpu_next = now + self.GPU_RETRY
                if str(e) != self._gpu_err:
                    log.warning("nvidia-smi failed: %s (retry in %.0fs)", e, self.GPU_RETRY)
                self._gpu_err = str(e)
        self._gpus = loop.snapshot() if self._gpu_started else []
        if self._gpus and self._gpu_err:
            log.info("nvidia-smi OK again")
            self._gpu_err = None

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

        if now >= self._ffmpeg_next:
            self._ffmpeg_count = _count_ffmpeg()
            self._ffmpeg_next = now + self.FFMPEG_PERIOD
        self._collect_gpus(now)
        vm = psutil.virtual_memory()
        snap = dict(self.static)
        snap.update({
            "time": time.strftime("%Y/%m/%d %H:%M.%S"),
            "cpu_percent": psutil.cpu_percent(None),
            "load": os.getloadavg(),
            "ffmpeg_count": self._ffmpeg_count,
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
        if self._gpu:
            self._gpu.stop()
