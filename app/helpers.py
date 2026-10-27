"""Helper-dialog backends: DeckLink enumeration, lavfi sources, probing, capabilities."""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
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


def _std_name(width_height, rate, interlaced):
    """'1920x1080', '30000/1001', True -> '1080i59.94' (interlaced shows the field rate)."""
    h = width_height.split("x")[1]
    try:
        num, den = (int(x) for x in rate.split("/"))
        fps = num / den * (2 if interlaced else 1)
    except (ValueError, ZeroDivisionError):
        return width_height
    return f"{h}{'i' if interlaced else 'p'}{round(fps, 2):g}"


def decklink_formats(ffmpeg, device):
    """Formats the card reports (device must not be in use).

    ffmpeg prints them as tab separated rows under the log line, e.g.
    '\tpal \t\t720x576 at 25000/1000 fps (interlaced, upper field first)'
    (older builds prefix each row with '[decklink @ 0x..]'). The code is 4 chars and may end in a space.
    """
    _, out, err = _run([ffmpeg, "-hide_banner", "-f", "decklink", "-list_formats", "1", "-i", device], timeout=15)
    fmts = []
    for line in (out + err).splitlines():
        m = re.search(r"(?:^|\]\s*)\t?([^\t\]]{3,4}?)\s*\t+((\d+x\d+) at ([\d/]+) fps(.*))$", line)
        if m:
            code = m.group(1).ljust(4) if len(m.group(1)) == 3 else m.group(1)
            inter = "interlaced" in m.group(5)
            extra = m.group(5).strip(" ()")
            label = f"{_std_name(m.group(3), m.group(4), inter)} — {m.group(3)}" + (f", {extra}" if extra else "")
            fmts.append({"code": code, "label": label})
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


def _nvdec_probe(ffmpeg):
    """Encode a few H.264 frames on the CPU, decode them with NVDEC into CUDA frames.
    hwdownload only accepts hardware frames, so success proves the GPU decoder was used."""
    try:
        sample = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30",
                                 "-frames:v", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "h264", "-"],
                                capture_output=True, timeout=20)
        if sample.returncode != 0 or not sample.stdout:
            return False, "sample encode failed: " + _last_line(sample.stderr.decode(errors="replace"))
        p = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                            "-f", "h264", "-i", "-", "-vf", "hwdownload,format=nv12", "-f", "null", "-"],
                           input=sample.stdout, capture_output=True, timeout=20)
        if p.returncode != 0:
            return False, _last_line(p.stderr.decode(errors="replace"))
        return True, ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def capabilities(ffmpeg):
    """Probe NVENC / NVDEC / CUDA filters with real tiny jobs (a codec or filter that ffmpeg
    lists may still be unusable, e.g. driver/API version mismatch). Logged once, cached."""
    global _caps
    with _caps_lock:
        if _caps is not None:
            return _caps
        rc, out, _ = _run([ffmpeg, "-hide_banner", "-version"])
        version = out.splitlines()[0].split(" Copyright")[0] if rc == 0 and out else "ffmpeg not runnable"
        rc, out, _ = _run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
        gpu = out.strip().replace("\n", " / ") if rc == 0 and out.strip() else "nvidia-smi not available"

        base = [ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=s=1280x720:d=0.2"]
        rc, _, err = _run(base + ["-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=20)
        nvenc, nvenc_err = rc == 0, "" if rc == 0 else _last_line(err)
        rc, _, err = _run(base + ["-frames:v", "1", "-c:v", "hevc_nvenc", "-f", "null", "-"], timeout=20)
        hevc, hevc_err = rc == 0, "" if rc == 0 else _last_line(err)
        # HEVC B-frames need Turing or newer (Pascal e.g. P2200: "No capable devices found")
        hevc_bf = hevc and _run(base + ["-frames:v", "3", "-c:v", "hevc_nvenc", "-bf", "1", "-f", "null", "-"],
                                timeout=20)[0] == 0
        nvdec, nvdec_err = _nvdec_probe(ffmpeg)
        filters = {}
        for name, chain in (("hwupload_cuda+scale_cuda", "format=nv12,hwupload_cuda,scale_cuda=640:360"),
                            ("bwdif_cuda", "format=nv12,hwupload_cuda,bwdif_cuda"),
                            ("yadif_cuda", "format=nv12,hwupload_cuda,yadif_cuda")):
            rc, _, err = _run(base + ["-frames:v", "2", "-vf", chain, "-f", "null", "-"], timeout=20)
            filters[name] = "" if rc == 0 else _last_line(err)
        cuda = filters["hwupload_cuda+scale_cuda"] == ""
        _caps = {"nvenc": nvenc, "nvdec": nvdec, "cuda": cuda,
                 "hevc_nvenc": hevc, "hevc_bframes": hevc_bf,
                 "nvenc_error": nvenc_err, "hevc_nvenc_error": hevc_err, "nvdec_error": nvdec_err,
                 "cuda_error": filters["hwupload_cuda+scale_cuda"],
                 "cuda_filters": {k: v == "" for k, v in filters.items()},
                 "ffmpeg": version, "gpu": gpu}

        ok = lambda b: "OK" if b else "UNAVAILABLE"
        log.info("capabilities: %s", version)
        log.info("capabilities: GPU %s", gpu)
        lvl = lambda b: logging.INFO if b else logging.WARNING
        log.log(lvl(nvenc), "capabilities: NVENC (h264_nvenc) %s%s", ok(nvenc), f" — {nvenc_err}" if nvenc_err else "")
        log.log(lvl(hevc), "capabilities: NVENC (hevc_nvenc) %s%s", ok(hevc),
                f" — {hevc_err}" if hevc_err else "" if hevc_bf else " (no B-frames on this GPU)")
        log.log(lvl(nvdec), "capabilities: NVDEC (hwaccel cuda) %s%s", ok(nvdec), f" — {nvdec_err}" if nvdec_err else "")
        for k, e in filters.items():
            log.log(lvl(not e), "capabilities: CUDA filter %s %s%s", k, ok(not e), f" — {e}" if e else "")
        if not (nvenc and nvdec and cuda):
            log.warning("capabilities: GPU features missing (nvenc=%s nvdec=%s cuda=%s) — helper falls back to "
                        "libx264 / CPU filters", nvenc, nvdec, cuda)
        return _caps


# ------------------------------------------------------------ file output folders
def browse_dirs(path):
    """Sub-directories of `path` for the helper's folder picker (directories only, no files)."""
    p = os.path.abspath(os.path.expanduser(path or "~"))
    while not os.path.isdir(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)  # deleted / mistyped folder -> nearest existing parent
    dirs, error = [], ""
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith("."):
                        dirs.append(e.name)
                except OSError:
                    pass
    except OSError as e:
        error = e.strerror or str(e)
    parent = os.path.dirname(p)
    return {"path": p, "parent": parent if parent != p else None, "dirs": sorted(dirs, key=str.lower),
            "writable": os.access(p, os.W_OK | os.X_OK), "error": error}


def check_dir_writable(path):
    """Really create (and remove) a file in `path`: os.access alone misses read-only mounts / ACLs."""
    p = os.path.abspath(os.path.expanduser(path or ""))
    if not path or not os.path.isdir(p):
        ok, reason = False, "폴더가 없습니다"
    else:
        try:
            fd, tmp = tempfile.mkstemp(prefix=".mediarouter-write-test-", dir=p)
            os.close(fd)
            os.unlink(tmp)
            ok, reason = True, ""
        except OSError as e:
            ok, reason = False, e.strerror or str(e)
    free = shutil.disk_usage(p).free if ok else None
    if ok:
        log.info("file output folder %s: writable (free %.1f GB)", p, free / 2 ** 30)
    else:
        log.warning("file output folder %s: NOT writable — %s", p, reason)
    return {"path": p, "ok": ok, "error": reason, "free_bytes": free}
