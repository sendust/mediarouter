"""mediarouter backend: FastAPI app, REST API and one multiplexed SSE stream."""
import asyncio
import json
import os
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import helpers, status
from .cli import Cli, CliError, client_script, html_page
from .cli_builder import DECKLINK_FORMATS, LAVFI_VIDEO_SOURCES, VIDEO_STANDARDS, build_cli, suggest_pair_paths
from .config import ConfigStore
from .fflog import FFReportWatchdog
from .logger import ClientIPMiddleware, get_logger, ring, setup_logging
from .manager import EncoderManager
from .mediamtx import MediaMTXPoller
from .subtitles import RecordingSubtitler
from .sysinfo import SysInfo

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
ALSA_STATE_DIR = os.path.join(BASE_DIR, "alsa")  # saved recording mixer state per card
CONFIG_PATH = os.environ.get("MEDIAROUTER_CONFIG", os.path.join(BASE_DIR, "config.json"))

store = ConfigStore(CONFIG_PATH)
setup_logging(store.section("logging"), BASE_DIR)
log = get_logger("main")
for _lvl, _msg in store.load_messages:
    getattr(log, _lvl)(_msg)

manager = EncoderManager(store, BASE_DIR)
mtx = MediaMTXPoller(store)
manager.path_ready = mtx.path_ready
sysinfo = SysInfo()
STARTED_AT = time.time()
ffreport = FFReportWatchdog(manager, store.section("ffreport_watchdog"))
subtitler = RecordingSubtitler(store)


@asynccontextmanager
async def lifespan(_app):
    log.info("mediarouter backend starting (config %s)", CONFIG_PATH)
    manager.kill_orphans()
    # GPU capability probe (~2 s) in the background; the helper waits on it if opened early
    threading.Thread(target=helpers.capabilities, args=(store.section("ffmpeg")["path"],),
                     name="caps-probe", daemon=True).start()
    sysinfo.start()
    mtx.start()
    ffreport.start()
    subtitler.start()
    threading.Thread(target=helpers.alsa_restore_all, args=(ALSA_STATE_DIR,), name="alsa-restore", daemon=True).start()
    manager.autostart()
    yield
    log.info("mediarouter backend shutting down")
    await asyncio.to_thread(manager.shutdown)
    for t in (subtitler, ffreport, mtx, sysinfo):
        t.stop()


app = FastAPI(title="mediarouter", lifespan=lifespan)
app.add_middleware(ClientIPMiddleware)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def _app_ctx():
    m = store.section("mediamtx")
    return {"rtsp_url": m["rtsp_url"], "rtsp_transport": m.get("rtsp_transport", "udp"),
            "udp_buffer_size": m.get("udp_buffer_size", 4194304),
            "segment_sec": store.section("recording").get("segment_sec", 3600),
            "alsa_low_latency": bool(store.section("alsa").get("low_latency", True))}


def _enc_or_404(enc_id):
    enc = manager.get(enc_id)
    if not enc:
        raise HTTPException(404, "encoder not found")
    return enc


_payload_cache = {"ts": 0.0, "data": []}


def encoders_payload(cached=True):
    # shared by every SSE client: psutil cpu_percent() is "since last call", so
    # computing it per client would make clients steal each other's samples
    now = time.monotonic()
    if not cached or now - _payload_cache["ts"] > 0.4:
        _payload_cache["data"] = [{"config": e.cfg, "status": e.status()} for e in manager.all()]
        _payload_cache["ts"] = now
    return _payload_cache["data"]


# ------------------------------------------------------------------ status
@app.get("/api/status")
def api_status(format: str = "json"):
    """Backend health + system + encoder summary for external monitoring.
    ?format=text returns a short plain-text version (e.g. for a Telegram message)."""
    doc = status_doc()
    if format == "text":
        return PlainTextResponse(status.to_text(doc))
    return doc


def status_doc():
    return status.build(sysinfo.snapshot, mtx.snapshot, encoders_payload(), store.items("monitors"),
                        BASE_DIR, STARTED_AT)


# --------------------------------------------------------------------- cli
cli = Cli(manager, store, mtx, sysinfo, status_doc)


def _base_url(request):
    return str(request.base_url).rstrip("/")


@app.get("/cli")
def cli_client(request: Request):
    """`curl http://host:8080/cli | bash` gets the bash client; a browser gets usage instructions."""
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(html_page(_base_url(request)))
    return PlainTextResponse(client_script(_base_url(request)), media_type="text/x-shellscript")


@app.post("/cli/run")
async def cli_run(request: Request):
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    line = (form.get("cmd") or [""])[0]
    value = form["value"][0] if "value" in form else None
    words = line.split()
    if words and words[0].lower() in ("set", "add", "delete", "del", "rm", "move", "mv"):
        log.info("cli: %s", line if value is None else f"{line} <{len(value)} chars>")
    try:
        out = await asyncio.to_thread(cli.run, line, value)
    except CliError as e:
        return PlainTextResponse(f"error: {e}\n", status_code=400)
    return PlainTextResponse(out.rstrip("\n") + "\n" if out else "")


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/settings")
def settings():
    m = store.section("mediamtx")
    return {"webrtc_url": m.get("webrtc_url") or "", "webrtc_port": m.get("webrtc_port", 8889),
            "rtsp_url": m["rtsp_url"], "rtsp_transport": m.get("rtsp_transport", "udp")}


# --------------------------------------------------------------- encoders
@app.get("/api/encoders")
def list_encoders():
    return encoders_payload(cached=False)


@app.post("/api/encoders")
def add_encoder(data: dict = Body(...)):
    return manager.add(data)


@app.post("/api/encoders/order")
def order_encoders(data: dict = Body(...)):
    store.reorder("encoders", data.get("ids", []))
    log.info("encoder order changed")
    return {"ok": True}


@app.patch("/api/encoders/{enc_id}")
def patch_encoder(enc_id: str, data: dict = Body(...)):
    cfg = manager.update(enc_id, data)
    if cfg is None:
        raise HTTPException(404, "encoder not found")
    return cfg


@app.delete("/api/encoders/{enc_id}")
def delete_encoder(enc_id: str):
    if not manager.remove(enc_id):
        raise HTTPException(404, "encoder not found")
    return {"ok": True}


@app.post("/api/encoders/{enc_id}/{action}")
def encoder_action(enc_id: str, action: str):
    enc = _enc_or_404(enc_id)
    if action == "start":
        if not enc.cfg.get("cli", "").strip():
            raise HTTPException(400, "command line is empty")
        enc.start()
    elif action == "stop":
        enc.stop()
    elif action == "restart":
        enc.restart()
    else:
        raise HTTPException(400, "unknown action")
    return enc.status()


@app.get("/api/encoders/{enc_id}/fflog")
def encoder_fflog(enc_id: str, lines: int = 300):
    enc = _enc_or_404(enc_id)
    return {"size": enc.fflog.size(), "total": enc.fflog.total_size(), "path": enc.fflog.path,
            "lines": enc.fflog.read_tail(max(10, min(lines, 5000)))}


@app.delete("/api/encoders/{enc_id}/fflog")
def encoder_fflog_clear(enc_id: str):
    enc = _enc_or_404(enc_id)
    enc.fflog.clear()
    log.info("[%s] fflog cleared", enc.title)
    return {"ok": True}


# --------------------------------------------------------------- monitors
@app.get("/api/monitors")
def list_monitors():
    return store.items("monitors")


@app.post("/api/monitors")
def add_monitor(data: dict = Body(...)):
    m = store.add_item("monitors", data)
    log.info("monitor added: %s %s", m["title"], m["paths"])
    return m


@app.post("/api/monitors/order")
def order_monitors(data: dict = Body(...)):
    store.reorder("monitors", data.get("ids", []))
    log.info("monitor order changed")
    return {"ok": True}


@app.patch("/api/monitors/{mon_id}")
def patch_monitor(mon_id: str, data: dict = Body(...)):
    m = store.update_item("monitors", mon_id, data)
    if m is None:
        raise HTTPException(404, "monitor not found")
    log.info("monitor updated: %s (%s)", m.get("title"), ", ".join(sorted(data)))
    return m


@app.delete("/api/monitors/{mon_id}")
def delete_monitor(mon_id: str):
    if not store.remove_item("monitors", mon_id):
        raise HTTPException(404, "monitor not found")
    log.info("monitor removed: %s", mon_id)
    return {"ok": True}


# ----------------------------------------------------------------- helper
@app.get("/api/helper/meta")
def helper_meta():
    return {
        "video_standards": list(VIDEO_STANDARDS.keys()),
        "decklink_formats": [{"code": c, "label": l} for c, l in DECKLINK_FORMATS],
        "lavfi_sources": helpers.lavfi_sources(store.section("ffmpeg")["path"]) or LAVFI_VIDEO_SOURCES,
        "caps": helpers.capabilities(store.section("ffmpeg")["path"]),
        "rtsp_transport": store.section("mediamtx").get("rtsp_transport", "udp"),
        "alsa_low_latency": bool(store.section("alsa").get("low_latency", True)),
        "recording": store.section("recording"),
    }


@app.get("/api/helper/decklink")
def helper_decklink():
    devs = helpers.decklink_devices(store.section("ffmpeg")["path"])
    for i, d in enumerate(devs):
        d["suggest_base"] = f"sdi{i + 1}"
    return devs


@app.get("/api/helper/decklink/formats")
def helper_decklink_formats(device: str):
    return helpers.decklink_formats(store.section("ffmpeg")["path"], device)


@app.get("/api/helper/alsa")
def helper_alsa():
    return {"devices": helpers.alsa_devices(), "alsa": helpers.capabilities(store.section("ffmpeg")["path"]).get("alsa")}


@app.get("/api/helper/alsa/mixer")
def helper_alsa_mixer(card: str):
    return {**helpers.alsa_mixer(card), "state": helper_alsa_state(card)}


@app.get("/api/helper/alsa/mixer/state")
def helper_alsa_state(card: str):
    try:
        return helpers.alsa_saved_state(ALSA_STATE_DIR, card)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/helper/alsa/mixer/{action}")
def helper_alsa_mixer_store(action: str, data: dict = Body(...)):
    fn = {"save": helpers.alsa_mixer_save, "revert": helpers.alsa_mixer_restore}.get(action)
    if fn is None:
        raise HTTPException(404, "unknown action")
    try:
        return fn(ALSA_STATE_DIR, data.get("card", ""))
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/helper/alsa/mixer")
def helper_alsa_mixer_set(data: dict = Body(...)):
    try:
        return helpers.alsa_mixer_set(data.get("card", ""), data.get("control", ""), data.get("kind", ""), data.get("value"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/helper/paths")
def helper_paths():
    snap = mtx.snapshot
    return {"ok": snap["ok"], "error": snap["error"], "paths": snap["paths"]}


@app.post("/api/helper/probe")
def helper_probe(data: dict = Body(...)):
    url = data.get("url") or ""
    if data.get("path"):
        url = f"{store.section('mediamtx')['rtsp_url'].rstrip('/')}/{data['path']}"
    transport = data.get("transport") or store.section("mediamtx").get("rtsp_transport", "udp")
    return helpers.probe(store.section("ffmpeg")["ffprobe"], url, transport)


@app.post("/api/helper/build")
def helper_build(spec: dict = Body(...)):
    try:
        return {"cli": build_cli(spec, _app_ctx())}
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"cannot build command: {e}")


@app.get("/api/helper/browse")
def helper_browse(path: str = ""):
    return helpers.browse_dirs(path or store.section("recording").get("default_dir") or "~")


@app.post("/api/helper/check_dir")
def helper_check_dir(data: dict = Body(...)):
    return helpers.check_dir_writable(data.get("path") or "")


@app.get("/api/helper/pair_paths")
def helper_pair_paths(base: str, channels: int = 8):
    return suggest_pair_paths(base, channels)


# ----------------------------------------------------------------- system
@app.get("/api/system")
def system():
    return {"system": sysinfo.snapshot, "mediamtx": mtx.snapshot}


@app.get("/api/logs")
def logs(since: int = 0):
    return ring().since(since)


# -------------------------------------------------------------------- SSE
def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


@app.get("/events")
async def events(request: Request):
    async def stream():
        last_seq = 0
        next_sys = next_mtx = 0.0
        yield _sse("logs", ring().since(0))
        last_seq = ring().last_seq
        while not await request.is_disconnected():
            now = time.monotonic()
            yield _sse("encoders", encoders_payload())
            if now >= next_sys:
                next_sys = now + 1.0
                yield _sse("system", sysinfo.snapshot)
                new = ring().since(last_seq)
                if new:
                    last_seq = new[-1]["seq"]
                    yield _sse("logs", new)
            if now >= next_mtx:
                next_mtx = now + 2.0
                yield _sse("mediamtx", mtx.snapshot)
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def run():
    srv = store.section("server")
    uvicorn.run(app, host=srv.get("host", "0.0.0.0"), port=int(srv.get("port", 8080)),
                log_config=None, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    run()
