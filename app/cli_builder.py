"""Builds an ffmpeg command line from the helper-dialog spec.

The result is plain text the user can still edit; the encoder only ever
runs the stored text (the spec is kept so the helper can be reopened).
"""
import shlex

# (label, size, rate, interlaced) -- interlaced sources are generated at field
# rate and woven with tinterlace=4 (skill §11)
VIDEO_STANDARDS = {
    "1080i5994": ("1920x1080", "60000/1001", True),
    "1080i50": ("1920x1080", "50", True),
    "1080p2997": ("1920x1080", "30000/1001", False),
    "1080p5994": ("1920x1080", "60000/1001", False),
    "1080p25": ("1920x1080", "25", False),
    "1080p50": ("1920x1080", "50", False),
    "1080p30": ("1920x1080", "30", False),
    "720p5994": ("1280x720", "60000/1001", False),
    "720p50": ("1280x720", "50", False),
    "2160p2997": ("3840x2160", "30000/1001", False),
    "2160p5994": ("3840x2160", "60000/1001", False),
}

# decklink -format_code values
DECKLINK_FORMATS = [
    ("Hi59", "1080i59.94"), ("Hi50", "1080i50"), ("Hi60", "1080i60"),
    ("Hp29", "1080p29.97"), ("Hp30", "1080p30"), ("Hp25", "1080p25"),
    ("Hp59", "1080p59.94"), ("Hp50", "1080p50"), ("Hp60", "1080p60"),
    ("23ps", "1080p23.98"), ("24ps", "1080p24"),
    ("hp59", "720p59.94"), ("hp50", "720p50"), ("hp60", "720p60"),
    ("4k29", "2160p29.97"), ("4k25", "2160p25"), ("4k59", "2160p59.94"), ("4k50", "2160p50"),
    ("ntsc", "NTSC 480i59.94"), ("pal ", "PAL 576i50"),
]

LAVFI_VIDEO_SOURCES = [
    "testsrc2", "testsrc", "smptehdbars", "smptebars", "pal75bars", "pal100bars",
    "rgbtestsrc", "yuvtestsrc", "colorspectrum", "colorchart", "zoneplate",
    "gradients", "mandelbrot", "life", "cellauto", "sierpinski", "color",
]
# sources that don't accept size= / rate= in the usual way
_NO_SIZE = {"colorchart"}

AUDIO_CODECS = {
    "aac": ["-c:a", "aac"],
    "opus": ["-c:a", "libopus"],
    "mp3": ["-c:a", "libmp3lame"],
}


def _q(arg):
    return shlex.quote(str(arg))


def _fmt_for_url(url):
    u = url.lower()
    if u.startswith(("rtmp://", "rtmps://")):
        return "flv"
    if u.startswith(("srt://", "udp://", "rtp://")) or u.endswith(".ts"):
        return "mpegts"
    if u.startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    return None


class Builder:
    def __init__(self, spec, app):
        """app: dict with 'rtsp_url', 'rtsp_transport'."""
        self.spec = spec
        self.app = app
        self.groups = []         # list of arg-lists, one printed line each
        self.inputs = 0
        self.fc = []             # filter_complex chains
        self.audio_streams = []  # [(stream_spec, channels)] in flat channel order
        self._uses = {}          # stream_spec -> times referenced by audio filters

    # ------------------------------------------------------------- inputs
    def _add_input(self, args):
        self.groups.append(args)
        idx = self.inputs
        self.inputs += 1
        return idx

    def _udp_buf(self, transport):
        size = int(self.app.get("udp_buffer_size") or 0)
        return ["-buffer_size", size] if transport == "udp" and size > 0 else []

    def _rtsp_url(self, path):
        return f"{self.app['rtsp_url'].rstrip('/')}/{path.lstrip('/')}"

    def build_inputs(self):
        inp = self.spec.get("input", {})
        kind = inp.get("type", "testsrc")
        custom_in = shlex.split(self.spec.get("custom", {}).get("input", "") or "")
        self.video_label = None
        self.interlaced = False
        # NVDEC: decode the (compressed) video input on the GPU and keep frames in CUDA memory
        v = self.spec.get("video") or {}
        self.nvdec = bool(inp.get("nvdec")) and kind in ("mediamtx", "custom") and v.get("codec") not in ("copy", "none")
        hw = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if self.nvdec else []

        if kind == "decklink":
            d = inp.get("decklink", {})
            args = ["-f", "decklink", "-format_code", (d.get("format_code") or "Hi59")]
            ch = int(d.get("channels") or 8)
            args += ["-channels", ch]
            if d.get("raw_format") and d["raw_format"] != "auto":
                args += ["-raw_format", d["raw_format"]]
            if d.get("video_input"):
                args += ["-video_input", d["video_input"]]
            args += custom_in + ["-i", d.get("device") or "DeckLink"]
            i = self._add_input(args)
            self.video_label = f"{i}:v:0"
            self.audio_streams = [(f"{i}:a:0", ch)]
            self.interlaced = (d.get("format_code") or "Hi59").startswith(("Hi", "ntsc", "pal"))

        elif kind == "mediamtx":
            m = inp.get("mediamtx", {})
            transport = m.get("transport") or self.app.get("rtsp_transport", "udp")
            paths = m.get("paths") or ([m["path"]] if m.get("path") else [])
            layout = self.spec.get("audio_layout") or []
            for n, path in enumerate(paths):
                args = ["-rtsp_transport", transport, "-timeout", "5000000"] + self._udp_buf(transport)
                if n == 0:
                    args += hw + custom_in
                args += ["-i", self._rtsp_url(path)]
                i = self._add_input(args)
                if n == 0:
                    self.video_label = f"{i}:v:0"
            # audio layout comes from probing: [{"input": 0, "index": 0, "channels": 2}, ...]
            self.audio_streams = [(f"{a.get('input', 0)}:a:{a.get('index', 0)}", int(a.get("channels", 2)))
                                  for a in layout]
            self.interlaced = bool(m.get("interlaced"))

        elif kind == "testsrc":
            t = inp.get("testsrc", {})
            src = t.get("source") or "testsrc2"
            std = t.get("standard") or "1080i5994"
            if std == "custom":
                size, rate, inter = t.get("size") or "1920x1080", t.get("rate") or "30000/1001", bool(t.get("interlaced"))
            else:
                size, rate, inter = VIDEO_STANDARDS.get(std, VIDEO_STANDARDS["1080i5994"])
            opts = [] if src in _NO_SIZE else [f"size={size}"]
            opts.append(f"rate={rate}")
            vsrc = f"{src}=" + ":".join(opts)
            i = self._add_input(["-re", "-f", "lavfi"] + custom_in + ["-i", vsrc])
            self.test_video = (i, inter)
            ch = max(1, int(t.get("audio_channels") or 2))
            freq = float(t.get("sine_freq") or 1000)
            # every channel a 1 kHz tone, each 3 dB lower than the previous one so
            # the routing is visible on the level meters (ch1 -6 dBFS peak, ch2 -9, ...)
            exprs = "|".join(f"{10 ** ((-6 - 3 * c) / 20):.4f}*sin(2*PI*{freq:g}*t)" for c in range(ch))
            a = self._add_input(["-re", "-f", "lavfi", "-i",
                                 f"aevalsrc=exprs={exprs}:sample_rate=48000:channel_layout={ch}c"])
            self.audio_streams = [(f"{a}:a:0", ch)]
            self.video_label = f"{i}:v:0"
            if inter:
                self.fc.append(f"[{i}:v:0]tinterlace=4,setfield=tff[vsrc]")
                self.video_label = "vsrc"
                self.interlaced = True

        else:  # custom
            c = inp.get("custom", {})
            args = shlex.split(c.get("args") or "-i input")
            if hw:  # applies to the first -i only
                k = args.index("-i") if "-i" in args else 0
                args = args[:k] + hw + args[k:]
            self.inputs += max(1, args.count("-i"))
            self.groups.append(args)
            self.video_label = "0:v:0"
            self.audio_streams = [(f"{a.get('input', 0)}:a:{a.get('index', 0)}", int(a.get("channels", 2)))
                                  for a in (self.spec.get("audio_layout") or [{"channels": 2}])]

    # -------------------------------------------------------------- video
    def video_chain(self):
        """Returns (map_label, codec_args) or (None, []) if video disabled."""
        v = self.spec.get("video", {})
        codec = v.get("codec", "h264_nvenc")
        if codec == "none" or not self.video_label:
            return None, []
        src = self.video_label
        if codec == "copy":
            return (src if ":" in src else f"[{src}]"), ["-c:v", "copy"]

        f = v.get("filters", {})
        cpu, cuda = [], []
        if f.get("deint"):                       # cpu bwdif / yadif / estdif
            cpu.append(f"{f['deint']}=mode={1 if f.get('deint_rate') == 'field' else 0}")
        if f.get("scale"):
            cpu.append(f"scale={f['scale'].replace('x', ':')}")
        if f.get("cuda_deint"):
            cuda.append(f"{f['cuda_deint']}=mode={1 if f.get('deint_rate') == 'field' else 0}")
        if f.get("cuda_resize"):
            cuda.append(f"scale_cuda={f['cuda_resize'].replace('x', ':')}")
        fps = f.get("fps")

        nvenc = codec == "h264_nvenc"
        on_gpu = self.nvdec      # NVDEC frames arrive as CUDA surfaces (nv12)
        chain = []
        if on_gpu and (cpu or (not cuda and not nvenc)):
            chain += ["hwdownload", "format=nv12"]   # CPU filters / CPU encoder need system memory
            on_gpu = False
        chain += cpu
        if cuda:
            if not on_gpu:
                # hwupload_cuda doesn't take 4:2:2 (decklink uyvy422): convert, interlace aware
                chain.append("scale=interl=1,format=nv12" if f.get("cuda_deint") else "format=nv12")
                chain.append("hwupload_cuda")
            chain += cuda
            if fps:
                chain.append(f"fps={fps}")
            if not nvenc:
                chain += ["hwdownload", "format=nv12"]
        else:
            if fps:
                chain.append(f"fps={fps}")
            if codec in ("libx264", "h264_nvenc") and not on_gpu:
                chain.append("format=yuv420p")

        self.fc.append(f"[{src}]{','.join(chain) or 'null'}[vout]")

        br = v.get("bitrate") or "6M"
        args = ["-c:v", codec]
        if codec == "h264_nvenc":
            args += ["-preset", v.get("preset") or "p4"]
            if v.get("tune"):
                args += ["-tune", v["tune"]]
            args += ["-rc", v.get("rc") or "cbr"]
            if v.get("zerolatency"):
                args += ["-zerolatency", "1"]
        else:
            args += ["-preset", v.get("preset") or "veryfast"]
            tunes = [t for t in [v.get("tune")] if t]
            if v.get("zerolatency"):
                tunes.append("zerolatency")
            if tunes:
                args += ["-tune", ",".join(tunes)]
        args += ["-b:v", br, "-maxrate", v.get("maxrate") or br, "-bufsize", v.get("bufsize") or br]
        if v.get("gop") not in (None, ""):
            args += ["-g", int(v["gop"])]
        if v.get("bframes") not in (None, ""):
            args += ["-bf", int(v["bframes"])]
        if v.get("profile"):
            args += ["-profile:v", v["profile"]]
        return "[vout]", args

    # -------------------------------------------------------------- audio
    def _flat(self, idx):
        """flat channel index -> (stream_spec, channel in stream)"""
        n = 0
        for spec, ch in self.audio_streams:
            if idx < n + ch:
                return spec, idx - n
            n += ch
        raise ValueError(f"audio channel {idx + 1} does not exist")

    def _codec_args(self, a, opus_default=False):
        codec = a.get("codec") or ("opus" if opus_default else "aac")
        args = list(AUDIO_CODECS.get(codec, ["-c:a", codec]))
        args += ["-b:a", a.get("bitrate") or "128k"]
        args += ["-ar", str(self._sample_rate(a))]
        return args

    def audio_stereo(self, left, right, label):
        """Build one stereo pair from two flat channel indices -> filter label."""
        ls, lc = self._flat(left)
        rs, rc = self._flat(right)
        post = self._audio_post()
        if ls == rs:
            self.fc.append(f"{self._ref(ls)}pan=stereo|c0=c{lc}|c1=c{rc}{post}[{label}]")
        else:
            lch = dict(self.audio_streams)[ls]
            self.fc.append(f"{self._ref(ls)}{self._ref(rs)}amerge=inputs=2,pan=stereo|c0=c{lc}|c1=c{lch + rc}{post}[{label}]")
        return f"[{label}]"

    def _sample_rate(self, a):
        return int(a.get("sample_rate") or (44100 if (a.get("codec") or "aac") == "mp3" else 48000))

    def _audio_post(self):
        """Filters applied to every stereo audio output after the channel pick (currently loudnorm)."""
        a = self.spec.get("audio", {})
        ln = a.get("loudnorm") or {}
        if not ln.get("enabled"):
            return ""
        def num(key, default, lo, hi):
            try:
                v = float(ln.get(key, default))
            except (TypeError, ValueError):
                v = default
            return f"{min(hi, max(lo, v)):g}"
        i = num("I", -23, -70, -5)
        lra = num("LRA", 7, 1, 50)
        tp = num("TP", -2, -9, 0)
        # single-pass (dynamic) loudnorm outputs 192 kHz: resample back for the encoder
        return f",loudnorm=I={i}:LRA={lra}:TP={tp},aresample={self._sample_rate(a)}"

    def _ref(self, spec):
        k = self._uses.get(spec, 0)
        self._uses[spec] = k + 1
        return f"<<{spec}#{k}>>"

    def _resolve_refs(self):
        """An input audio stream used by several pan chains is fanned out with asplit."""
        splits = []
        for spec, n in self._uses.items():
            if n == 1:
                self.fc = [c.replace(f"<<{spec}#0>>", f"[{spec}]") for c in self.fc]
                continue
            tag = spec.replace(":", "_")
            splits.append(f"[{spec}]asplit={n}" + "".join(f"[s{tag}_{k}]" for k in range(n)))
            for k in range(n):
                self.fc = [c.replace(f"<<{spec}#{k}>>", f"[s{tag}_{k}]") for c in self.fc]
        self.fc = splits + self.fc

    def audio_chain(self):
        a = self.spec.get("audio", {})
        codec = a.get("codec") or "aac"
        if codec == "none" or not self.audio_streams:
            return None, []
        if codec == "copy":
            spec, _ = self._flat(int(a.get("left", 0)))
            return spec, ["-c:a", "copy"]
        lbl = self.audio_stereo(int(a.get("left", 0)), int(a.get("right", 1)), "aout")
        return lbl, self._codec_args(a)

    # ------------------------------------------------------------ outputs
    def _out_transport(self):
        """RTSP transport for publishing: per encoder (helper sink) or the config default."""
        t = (self.spec.get("sink") or {}).get("rtsp_transport")
        return t if t in ("udp", "tcp") else self.app.get("rtsp_transport", "udp")

    def _out_args(self, url, fmt=None):
        custom_out = shlex.split(self.spec.get("custom", {}).get("output", "") or "")
        fmt = fmt or _fmt_for_url(url)
        args = list(custom_out)
        if fmt == "rtsp":
            transport = self._out_transport()
            args += ["-f", "rtsp", "-rtsp_transport", transport] + self._udp_buf(transport)
        elif fmt:
            args += ["-f", fmt]
        return args + [url]

    def build(self):
        custom_global = shlex.split(self.spec.get("custom", {}).get("global", "") or "")
        head = ["ffmpeg"] + custom_global
        self.build_inputs()
        sink = self.spec.get("sink", {})
        outs = []

        if sink.get("type") == "mediamtx_pairs":
            # skill §2: one input, video + first stereo pair on <base>_va12, the
            # remaining pairs audio-only on <base>_a34, _a56, ...
            base = sink.get("base") or "sdi1"
            total = sum(ch for _, ch in self.audio_streams)
            pairs = sink.get("pairs") or list(range(total // 2))
            vmap, vargs = self.video_chain()
            a = self.spec.get("audio", {})
            aargs = self._codec_args(a, opus_default=True)
            for n, p in enumerate(pairs):
                p = int(p)
                name = f"{base}_{'va' if n == 0 else 'a'}{2 * p + 1}{2 * p + 2}"
                lbl = self.audio_stereo(2 * p, 2 * p + 1, f"a{2 * p + 1}{2 * p + 2}")
                o = []
                if n == 0 and vmap:
                    o += ["-map", vmap] + vargs
                o += ["-map", lbl] + aargs
                outs.append(o + self._out_args(self._rtsp_url(name), "rtsp"))
            if not pairs and vmap:
                outs.append(["-map", vmap] + vargs + self._out_args(self._rtsp_url(f"{base}_v"), "rtsp"))
        else:
            vmap, vargs = self.video_chain()
            amap, aargs = self.audio_chain()
            maps = []
            if vmap:
                maps += ["-map", vmap] + vargs
            if amap:
                maps += ["-map", amap] + aargs
            targets = []
            for o in self.spec.get("outputs") or []:
                if o.get("type") == "mediamtx" and o.get("path"):
                    targets.append((self._rtsp_url(o["path"]), "rtsp"))
                elif o.get("type") == "null":
                    targets.append(("-", "null"))
                elif o.get("url"):
                    targets.append((o["url"], o.get("format") if o.get("format") not in (None, "", "auto") else None))
            if not targets:
                targets = [("-", "null")]
            if len(targets) == 1:
                url, fmt = targets[0]
                outs.append(maps + self._out_args(url, fmt))
            else:
                # one encode, several destinations; a failing destination doesn't kill the others
                slaves = []
                for url, fmt in targets:
                    fmt = fmt or _fmt_for_url(url)
                    opts = ["onfail=ignore"]
                    if fmt:
                        opts.insert(0, f"f={fmt}")
                    if fmt == "rtsp":
                        transport = self._out_transport()
                        opts.append(f"rtsp_transport={transport}")
                        if self._udp_buf(transport):
                            opts.append(f"buffer_size={int(self.app['udp_buffer_size'])}")
                    slaves.append(f"[{':'.join(opts)}]{url}")
                custom_out = shlex.split(self.spec.get("custom", {}).get("output", "") or "")
                outs.append(maps + ["-flags:v", "+global_header", "-flags:a", "+global_header"]
                            + custom_out + ["-f", "tee", "|".join(slaves)])

        self._resolve_refs()
        lines = [head] + self.groups
        if self.fc:
            lines.append(["-filter_complex", ";".join(self.fc)])
        lines += outs
        return " \\\n  ".join(" ".join(_q(x) for x in g) for g in lines if g)


def build_cli(spec, app):
    return Builder(spec, app).build()


def suggest_pair_paths(base, channels):
    return [f"{base}_va12"] + [f"{base}_a{2 * p + 1}{2 * p + 2}" for p in range(1, channels // 2)]
