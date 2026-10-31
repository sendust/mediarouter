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
        rc, out, _ = _run([ffmpeg, "-hide_banner", "-devices"])
        alsa = rc == 0 and re.search(r"^\s*D\S*\s+alsa\b", out, re.MULTILINE) is not None
        _caps = {"nvenc": nvenc, "nvdec": nvdec, "cuda": cuda,
                 "hevc_nvenc": hevc, "hevc_bframes": hevc_bf, "alsa": alsa,
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
        log.log(lvl(alsa), "capabilities: ALSA input (-f alsa) %s%s", ok(alsa),
                "" if alsa else " — ffmpeg built without ALSA (needs libasound2-dev at configure time)")
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


# ------------------------------------------------------------ ALSA capture devices / mixer
def alsa_devices(probe=True):
    """Capture PCMs of every sound card, from /proc/asound (no alsa-utils needed).
    `device` is the ffmpeg -i argument: plughw by card id (stable across reboots, converts format/rate)."""
    cards = {}
    try:
        with open("/proc/asound/cards", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*?)\s+-\s+(.*)$", line)
                if m:
                    cards[int(m.group(1))] = {"id": m.group(2), "driver": m.group(3), "name": m.group(4).strip()}
    except OSError:
        return []
    out = []
    try:
        with open("/proc/asound/pcm", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        m = re.match(r"(\d+)-(\d+):\s*(.*?)\s*:\s*(.*?)\s*:(.*)$", line)
        if not m or "capture" not in m.group(5):
            continue
        c, d = int(m.group(1)), int(m.group(2))
        card = cards.get(c, {"id": str(c), "name": "", "driver": ""})
        busy = _pcm_busy(c, d)
        out.append({
            "card": card["id"], "card_index": c, "card_name": card["name"], "pcm": d, "pcm_name": m.group(3),
            "device": f"plughw:CARD={card['id']},DEV={d}",
            "busy": busy, "hw_channels": _hw_channels(card["id"], d, busy) if probe else None,
        })
    return out


_hw_ch_cache = {}


def _hw_channels(card, pcm, busy):
    """[min, max] capture channels the hardware supports (arecord --dump-hw-params), cached.
    A busy device cannot be probed: last known value, or None."""
    key = (card, pcm)
    if key in _hw_ch_cache or busy or not shutil.which("arecord"):
        return _hw_ch_cache.get(key)
    _, _, err = _run(["timeout", "3", "arecord", "-D", f"hw:CARD={card},DEV={pcm}", "--dump-hw-params",
                      "-d", "1", "-f", "S16_LE", "-r", "48000", "/dev/null"], timeout=6)
    m = re.search(r"^CHANNELS:\s*(?:\[(\d+)\s+(\d+)\]|(\d+))", err, re.MULTILINE)
    if m:
        lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)),) * 2
        _hw_ch_cache[key] = [lo, hi]
    return _hw_ch_cache.get(key)


def _pcm_busy(card, pcm):
    """True when some process has the capture PCM open (sub0/status is 'closed' otherwise)."""
    try:
        with open(f"/proc/asound/card{card}/pcm{pcm}c/sub0/status", encoding="utf-8") as f:
            return f.read().strip() != "closed"
    except OSError:
        return False


# controls shown in the recording mixer: capture volume/switch/source, plus common (non-directional)
# controls that clearly belong to the input side (Mic Boost, Line Boost, ...)
_CAPTURE_NAME = re.compile(r"capture|mic|boost|input|line|adc|digital|record|source", re.IGNORECASE)
_SEG = r"(?:(Playback|Capture) )?(\d+) \[(\d+)%\](?: \[(-?[\d.]+)dB\])?(?: \[(on|off)\])?"


def _amixer(card, *args):
    if not shutil.which("amixer"):
        return -1, "", "amixer not found (sudo apt install alsa-utils)"
    return _run(["amixer", "-c", str(card), *args], timeout=5)


def _parse_scontents(text):
    controls, cur = [], None
    for line in text.splitlines():
        m = re.match(r"Simple mixer control '(.*)',(\d+)$", line)
        if m:
            cur = {"name": m.group(1), "index": int(m.group(2)), "caps": [], "items": [], "channels": []}
            controls.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Capabilities:"):
            cur["caps"] = s.split(":", 1)[1].split()
        elif s.startswith("Limits:"):
            nums = re.findall(r"(?:(Playback|Capture) )?(-?\d+) - (-?\d+)", s)
            for direction, lo, hi in nums:
                if direction != "Playback":
                    cur["min"], cur["max"] = int(lo), int(hi)
        elif s.startswith("Items:"):
            cur["items"] = re.findall(r"'([^']*)'", s)
        elif re.match(r"Item\d+:", s):
            cur.setdefault("item", (re.findall(r"'([^']*)'", s) or [""])[0])
        elif ":" in s and not s.startswith(("Playback channels", "Capture channels", "Capabilities")):
            ch, rest = s.split(":", 1)
            vol = None
            for direction, val, pct, db, sw in re.findall(_SEG, rest):
                if direction != "Playback":
                    vol = {"name": ch.strip(), "value": int(val), "percent": int(pct),
                           "db": float(db) if db else None, "on": (sw == "on") if sw else None}
            if vol is None:
                sw = re.search(r"Capture \[(on|off)\]", rest)
                if sw:
                    vol = {"name": ch.strip(), "value": None, "percent": None, "db": None, "on": sw.group(1) == "on"}
            if vol:
                cur["channels"].append(vol)
    out = []
    for c in controls:
        caps = set(c["caps"])
        cvol = bool(caps & {"cvolume", "cvolume-joined"})
        csw = bool(caps & {"cswitch", "cswitch-joined", "cswitch-exclusive"})
        cenum = "cenum" in caps
        common_vol = bool(caps & {"volume", "volume-joined"}) and _CAPTURE_NAME.search(c["name"])
        common_enum = "enum" in caps and _CAPTURE_NAME.search(c["name"])
        if not (cvol or csw or cenum or common_vol or common_enum):
            continue
        out.append({
            "name": c["name"], "index": c["index"], "id": f"{c['name']},{c['index']}",
            "volume": cvol or bool(common_vol), "capture_dir": cvol,
            "switch": csw, "enum": cenum or bool(common_enum),
            "min": c.get("min"), "max": c.get("max"), "items": c["items"], "item": c.get("item"),
            "channels": c["channels"],
        })
    return out


def alsa_mixer(card):
    rc, out, err = _amixer(card, "scontents")
    if rc != 0:
        return {"card": card, "controls": [], "error": (err or out).strip().splitlines()[-1] if (err or out).strip() else f"amixer rc={rc}"}
    return {"card": card, "controls": _parse_scontents(out), "error": ""}


def alsa_mixer_set(card, control, kind, value):
    """kind: volume (raw value, all channels) | switch (bool) | enum (item name)."""
    ctl = next((c for c in alsa_mixer(card)["controls"] if c["id"] == control), None)
    if ctl is None:
        raise ValueError(f"no capture control {control!r} on card {card}")
    if kind == "volume" and ctl["volume"]:
        v = max(ctl["min"] or 0, min(ctl["max"] or 0, int(value)))
        args = ["sset", control] + (["capture"] if ctl["capture_dir"] else []) + [str(v)]
    elif kind == "switch" and ctl["switch"]:
        args = ["sset", control, "cap" if value else "nocap"]
    elif kind == "enum" and ctl["enum"] and value in ctl["items"]:
        args = ["sset", control, value]
    else:
        raise ValueError(f"{control}: cannot set {kind}")
    rc, out, err = _amixer(card, "--", *args)
    if rc != 0:
        raise ValueError((err or out).strip() or f"amixer rc={rc}")
    log.info("alsa mixer %s '%s' %s = %s", card, control, kind, value)
    return next((c for c in alsa_mixer(card)["controls"] if c["id"] == control), ctl)


# saved mixer state, one alsactl state file per card (alsactl -f: no root needed, unlike
# /var/lib/alsa/asound.state); restored when the backend starts
def _state_file(state_dir, card):
    if not re.fullmatch(r"[\w-]+", card or ""):
        raise ValueError(f"invalid card id {card!r}")
    return os.path.join(state_dir, f"{card}.state")


def _alsactl(*args):
    if not shutil.which("alsactl"):
        return -1, "", "alsactl not found (sudo apt install alsa-utils)"
    return _run(["alsactl", *args], timeout=10)


def alsa_saved_state(state_dir, card):
    """{saved, saved_at, dirty}: dirty = current mixer differs from the saved file."""
    path = _state_file(state_dir, card)
    if not os.path.exists(path):
        return {"saved": False, "saved_at": None, "dirty": True}
    fd, tmp = tempfile.mkstemp(prefix=".cur-", suffix=".state", dir=state_dir)
    os.close(fd)
    try:
        rc, _, _ = _alsactl("-f", tmp, "store", card)
        with open(path, encoding="utf-8", errors="replace") as a, open(tmp, encoding="utf-8", errors="replace") as b:
            dirty = rc != 0 or a.read() != b.read()
    finally:
        os.unlink(tmp)
    return {"saved": True, "saved_at": os.path.getmtime(path), "dirty": dirty}


def alsa_mixer_save(state_dir, card):
    path = _state_file(state_dir, card)
    os.makedirs(state_dir, exist_ok=True)
    rc, out, err = _alsactl("-f", path, "store", card)
    if rc != 0:
        raise ValueError((err or out).strip() or f"alsactl rc={rc}")
    log.info("alsa mixer %s saved to %s", card, path)
    return alsa_saved_state(state_dir, card)


def alsa_mixer_restore(state_dir, card, reason="revert"):
    path = _state_file(state_dir, card)
    if not os.path.exists(path):
        raise ValueError(f"no saved mixer state for {card}")
    rc, out, err = _alsactl("-f", path, "restore", card)
    if rc != 0:
        raise ValueError((err or out).strip() or f"alsactl rc={rc}")
    log.info("alsa mixer %s restored from %s (%s)", card, path, reason)
    return alsa_saved_state(state_dir, card)


def alsa_restore_all(state_dir):
    """Backend start: put every card with a saved state back to it (cards not present are skipped)."""
    try:
        files = sorted(f for f in os.listdir(state_dir) if f.endswith(".state") and not f.startswith("."))
    except OSError:
        return
    present = {d["card"] for d in alsa_devices(probe=False)}   # no arecord probe: it would hold the device while ALSA encoders autostart
    for f in files:
        card = f[:-len(".state")]
        if card not in present:
            log.warning("alsa mixer %s: saved state but no such card now — skipped", card)
            continue
        try:
            alsa_mixer_restore(state_dir, card, "backend start")
        except ValueError as e:
            log.warning("alsa mixer %s restore failed: %s", card, e)
