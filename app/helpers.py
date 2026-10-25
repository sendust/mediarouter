"""Helper-dialog backends: DeckLink enumeration, lavfi sources, probing, capabilities."""
import json
import re
import subprocess
import threading

from .cli_builder import LAVFI_VIDEO_SOURCES
from .logger import get_logger

log = get_logger("helper")

_caps = None
_caps_lock = threading.Lock()


def _run(argv, timeout=10):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def _last_line(text):
    """Most telling line of an ffmpeg failure (the last line is usually a generic summary)."""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    for l in lines:
        if re.search(r"cuda|nvenc|libcuda|device|driver", l, re.IGNORECASE):
            return l.strip()
    return lines[-1] if lines else "failed"


def decklink_devices(ffmpeg):
    """Parse `ffmpeg -sources decklink`:  '  81:ed12d690:00000000 [DeckLink Duo (1)] (none)'"""
    _, out, err = _run([ffmpeg, "-hide_banner", "-sources", "decklink"])
    devices = []
    for line in (out + err).splitlines():
        m = re.match(r"\s*\*?\s*(\S+)\s+\[(.+)\]", line)
        if m:
            devices.append({"id": m.group(1), "name": m.group(2)})
    return devices


def decklink_formats(ffmpeg, device):
    """Formats the card reports (device must not be in use)."""
    _, out, err = _run([ffmpeg, "-hide_banner", "-f", "decklink", "-list_formats", "1", "-i", device], timeout=15)
    fmts = []
    for line in (out + err).splitlines():
        m = re.search(r"\]\s+(\S{3,4})\s+(\d+x\d+ at [\d/]+ fps.*)$", line)
        if m:
            fmts.append({"code": m.group(1), "label": m.group(2).strip()})
    return {"formats": fmts, "raw": (out + err)[-3000:] if not fmts else ""}


def lavfi_sources(ffmpeg):
    _, out, _ = _run([ffmpeg, "-hide_banner", "-filters"])
    have = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "|->V":
            have.add(parts[1])
    return [s for s in LAVFI_VIDEO_SOURCES if not have or s in have]


def probe(ffprobe, url, transport="udp"):
    argv = [ffprobe, "-hide_banner", "-v", "error", "-print_format", "json", "-show_streams",
            "-show_format", "-rw_timeout", "5000000"]
    if url.startswith("rtsp"):
        argv += ["-rtsp_transport", transport]
    rc, out, err = _run(argv + [url], timeout=15)
    if rc != 0:
        return {"ok": False, "error": err.strip()[-500:] or f"ffprobe rc={rc}"}
    data = json.loads(out or "{}")
    streams = []
    a_idx = v_idx = 0
    for s in data.get("streams", []):
        t = s.get("codec_type")
        item = {"type": t, "codec": s.get("codec_name")}
        if t == "video":
            item.update(index=v_idx, width=s.get("width"), height=s.get("height"),
                        rate=s.get("r_frame_rate"), field_order=s.get("field_order"))
            v_idx += 1
        elif t == "audio":
            item.update(index=a_idx, channels=s.get("channels"), sample_rate=s.get("sample_rate"))
            a_idx += 1
        streams.append(item)
    return {"ok": True, "streams": streams}


def capabilities(ffmpeg):
    """Probe NVENC/CUDA with a real tiny encode (an encoder listed in -encoders may still be unusable)."""
    global _caps
    with _caps_lock:
        if _caps is not None:
            return _caps
        base = [ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=s=1280x720:d=0.2"]
        rc, _, err = _run(base + ["-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=20)
        nvenc = rc == 0
        rc2, _, err2 = _run(base + ["-frames:v", "1", "-vf", "format=nv12,hwupload_cuda,scale_cuda=640:360",
                                    "-f", "null", "-"], timeout=20)
        cuda = rc2 == 0
        _caps = {"nvenc": nvenc, "cuda": cuda,
                 "nvenc_error": "" if nvenc else _last_line(err),
                 "cuda_error": "" if cuda else _last_line(err2)}
        log.info("capabilities: nvenc=%s cuda=%s", nvenc, cuda)
        return _caps
