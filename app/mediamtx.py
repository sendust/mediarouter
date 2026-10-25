"""Local mediamtx API poller: paths, their publisher and the hosts pulling them."""
import json
import threading
import time
import urllib.error
import urllib.request

from .logger import get_logger

log = get_logger("mediamtx")

SESSION_LISTS = {
    "rtspSession": "/v3/rtspsessions/list",
    "rtspsSession": "/v3/rtspssessions/list",
    "rtmpConn": "/v3/rtmpconns/list",
    "rtmpsConn": "/v3/rtmpsconns/list",
    "srtConn": "/v3/srtconns/list",
    "webRTCSession": "/v3/webrtcsessions/list",
}


class MediaMTXPoller(threading.Thread):
    def __init__(self, store):
        super().__init__(name="mediamtx-poller", daemon=True)
        self.store = store
        self._stop = threading.Event()
        self.snapshot = {"ok": False, "error": "not polled yet", "paths": [], "ts": 0}
        self._was_ok = None
        self._prev_bytes = {}  # path -> (ts, inbound bytes) for bitrate

    def _get(self, base, path):
        with urllib.request.urlopen(base.rstrip("/") + path + "?itemsPerPage=1000", timeout=2) as r:
            return json.loads(r.read().decode())

    def poll(self):
        cfg = self.store.section("mediamtx")
        base = cfg["api_url"]
        try:
            paths = self._get(base, "/v3/paths/list").get("items", [])
        except (urllib.error.URLError, OSError, ValueError) as e:
            if self._was_ok is not False:
                log.warning("mediamtx API unreachable (%s): %s", base, e)
            self._was_ok = False
            self.snapshot = {"ok": False, "error": str(e), "paths": [], "ts": time.time()}
            return
        if self._was_ok is not True:
            log.info("mediamtx API connected (%s)", base)
        self._was_ok = True

        sessions = {}
        for kind, url in SESSION_LISTS.items():
            try:
                for it in self._get(base, url).get("items", []):
                    sessions[it.get("id")] = {"type": kind, "remote": it.get("remoteAddr", ""),
                                              "state": it.get("state", ""), "transport": it.get("transport", "")}
            except (urllib.error.URLError, OSError, ValueError):
                pass  # endpoint disabled (e.g. rtmps off)

        out = []
        now = time.monotonic()
        prev_bytes, self._prev_bytes = self._prev_bytes, {}
        for p in paths:
            src = p.get("source") or {}
            readers = []
            for r in p.get("readers") or []:
                s = sessions.get(r.get("id"), {})
                readers.append({"type": r.get("type"), "remote": s.get("remote", ""), "transport": s.get("transport", "")})
            tracks = []
            for t in p.get("tracks2") or [{"codec": c} for c in p.get("tracks") or []]:
                props = t.get("codecProps") or {}
                tracks.append({"codec": t.get("codec"), "channels": props.get("channelCount"),
                               "sample_rate": props.get("sampleRate"),
                               "width": props.get("width"), "height": props.get("height"),
                               "profile": props.get("profile"), "level": props.get("level")})
            bytes_in = p.get("inboundBytes", p.get("bytesReceived", 0)) or 0
            self._prev_bytes[p.get("name")] = (now, bytes_in)
            prev = prev_bytes.get(p.get("name"))
            bps_in = None
            if prev and now > prev[0] and bytes_in >= prev[1]:
                bps_in = (bytes_in - prev[1]) * 8 / (now - prev[0])
            out.append({
                "name": p.get("name"),
                "ready": p.get("ready", False),
                "ready_time": p.get("readyTime"),
                "source_type": src.get("type"),
                "source_remote": sessions.get(src.get("id"), {}).get("remote", ""),
                "tracks": tracks,
                "readers": readers,
                "bytes_in": bytes_in,
                "bps_in": bps_in,
                "bytes_out": p.get("outboundBytes", p.get("bytesSent", 0)),
            })
        out.sort(key=lambda x: x["name"] or "")
        self.snapshot = {"ok": True, "error": "", "paths": out, "ts": time.time()}

    def run(self):
        while True:
            try:
                self.poll()
            except Exception:  # never let the poller die
                log.exception("mediamtx poll failed")
            if self._stop.wait(float(self.store.section("mediamtx").get("poll_interval_sec", 2))):
                break

    def stop(self):
        self._stop.set()
