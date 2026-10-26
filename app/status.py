"""Backend status summary for external monitoring (GET /api/status).

One JSON document with an overall health verdict, the reasons for it, a
system summary, mediamtx reachability and a compact line per encoder, so a
poller (e.g. a Telegram bot) only has to look at `health` and `issues`.
"""
import re
import shutil
import time

# health levels, worst wins
OK, WARN, ERROR = "ok", "warn", "error"
_RANK = {OK: 0, WARN: 1, ERROR: 2}

CPU_WARN_PCT = 90.0
MEM_WARN_AVAIL_PCT = 10.0
DISK_WARN_PCT = 90.0
RECENT_ERROR_SEC = 60.0

_RTSP_OUT = re.compile(r"(-i\s+['\"]?)?rtsps?://[^\s'\"|/]+/([^\s'\"|?]+)")


def _publish_paths(cli):
    """mediamtx paths an encoder publishes to (rtsp URLs that are not inputs)."""
    return sorted({m.group(2) for m in _RTSP_OUT.finditer(cli or "") if not m.group(1)})


def _bitrate_bps(progress, cli, path_bps):
    """ffmpeg's -progress bitrate, or the mediamtx inbound rate of the published
    paths when ffmpeg can't count bytes (rtsp / tee outputs report N/A)."""
    raw = (progress or {}).get("bitrate") or ""
    m = re.match(r"\s*([\d.]+)\s*kbits/s", raw)
    if m:
        return float(m.group(1)) * 1000, "ffmpeg"
    rates = [path_bps[p] for p in _publish_paths(cli) if path_bps.get(p) is not None]
    if rates:
        return sum(rates), "mediamtx"
    return None, None


def _encoder(cfg, st, path_bps, now):
    prog = st.get("progress") or {}
    bps, bps_src = _bitrate_bps(prog, cfg.get("cli"), path_bps)
    codecs = st.get("codecs") or {}
    level, issues = OK, []
    state = st.get("state")
    if state in ("error", "restarting"):
        level = ERROR
        issues.append(f"{state}: {st.get('last_reason') or st.get('last_error') or '-'}")
    elif state in ("stopped", "finished") and cfg.get("autostart"):
        level = WARN
        issues.append("autostart 인코더가 정지 상태")
    if st.get("slow"):
        level = max(level, WARN, key=_RANK.get)
        issues.append(f"speed {st.get('speed_now') or 0:.2f}x (실시간 미달)")
    age = st.get("last_error_age")
    if state == "running" and st.get("last_error") and age is not None and age < RECENT_ERROR_SEC:
        level = max(level, WARN, key=_RANK.get)
        issues.append(f"최근 오류: {st['last_error']}")
    return {
        "id": cfg["id"],
        "title": cfg.get("title") or cfg["id"],
        "state": state,
        "health": level,
        "issues": issues,
        "autostart": bool(cfg.get("autostart")),
        "auto_restart": bool(cfg.get("auto_restart")),
        "pid": st.get("pid"),
        "uptime_sec": round(st["uptime"], 1) if st.get("uptime") is not None else None,
        "restarts": st.get("restarts", 0),
        "fps": prog.get("fps"),
        "speed": round(st["speed_now"], 3) if st.get("speed_now") is not None else None,
        "bitrate_bps": round(bps) if bps is not None else None,
        "bitrate_source": bps_src,
        "drop_frames": prog.get("drop_frames"),
        "dup_frames": prog.get("dup_frames"),
        "cpu_percent": st.get("cpu"),
        "rss_bytes": st.get("rss"),
        "video": [v["name"] + (f" {v['size']}" if v.get("size") else "") for v in codecs.get("video") or []],
        "audio": [a["name"] + (f" x{a['count']}" if a.get("count", 1) > 1 else "") for a in codecs.get("audio") or []],
        "publish_paths": _publish_paths(cfg.get("cli")),
        "last_exit_code": st.get("last_exit_code"),
        "last_reason": st.get("last_reason") or None,
        "last_error": st.get("last_error") or None,
        "last_error_age_sec": round(age, 1) if age is not None else None,
        "next_scheduled_restart": st.get("next_scheduled_restart"),
    }


def build(sysinfo, mtx_snapshot, encoders, monitors, base_dir, started_at):
    """encoders: [{"config": cfg, "status": status}] (the shared cached payload)."""
    now = time.time()
    sysd = sysinfo or {}
    issues = []
    level = OK

    def flag(lvl, msg):
        nonlocal level
        issues.append({"level": lvl, "message": msg})
        if _RANK[lvl] > _RANK[level]:
            level = lvl

    # ---- system
    mem_total = sysd.get("mem_total") or 0
    mem_avail = sysd.get("mem_available") or 0
    mem_avail_pct = (mem_avail / mem_total * 100) if mem_total else None
    disk = shutil.disk_usage(base_dir)
    disk_pct = disk.used / disk.total * 100
    cpu = sysd.get("cpu_percent")
    if cpu is not None and cpu >= CPU_WARN_PCT:
        flag(WARN, f"CPU {cpu:.0f}%")
    if mem_avail_pct is not None and mem_avail_pct < MEM_WARN_AVAIL_PCT:
        flag(WARN, f"가용 메모리 {mem_avail_pct:.0f}%")
    if disk_pct >= DISK_WARN_PCT:
        flag(WARN, f"디스크 사용 {disk_pct:.0f}%")
    system = {
        "hostname": sysd.get("hostname"),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend_uptime_sec": round(now - started_at, 1),
        "cpu_model": sysd.get("cpu_model"),
        "cpu_threads": sysd.get("cpu_threads"),
        "cpu_percent": cpu,
        "load": list(sysd.get("load") or []),
        "mem_total_bytes": mem_total,
        "mem_available_bytes": mem_avail,
        "mem_available_percent": round(mem_avail_pct, 1) if mem_avail_pct is not None else None,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "disk_used_percent": round(disk_pct, 1),
        "ffmpeg_processes": sysd.get("ffmpeg_count"),
        "gpus": sysd.get("gpus") or [],
        "nics": [{"name": n["name"], "ips": n["ips"], "speed_mbps": n["speed_mbps"],
                  "rx_bps": round(n["rx_bps"]), "tx_bps": round(n["tx_bps"])} for n in sysd.get("nics") or []],
    }

    # ---- mediamtx
    snap = mtx_snapshot or {}
    paths = snap.get("paths") or []
    if not snap.get("ok"):
        flag(ERROR, f"mediamtx API 접속 불가: {snap.get('error') or '-'}")
    path_bps = {p["name"]: p.get("bps_in") for p in paths}
    mediamtx = {
        "ok": bool(snap.get("ok")),
        "error": snap.get("error") or None,
        "paths_total": len(paths),
        "paths_ready": sum(1 for p in paths if p.get("ready")),
        "readers_total": sum(len(p.get("readers") or []) for p in paths),
    }

    # ---- encoders
    encs = [_encoder(e["config"], e["status"], path_bps, now) for e in encoders]
    for e in encs:
        for msg in e["issues"]:
            flag(e["health"], f"[{e['title']}] {msg}")
    counts = {"total": len(encs)}
    for e in encs:
        counts[e["state"]] = counts.get(e["state"], 0) + 1

    # ---- monitors: a monitor watching a path that isn't published is worth knowing
    for m in monitors:
        if m.get("enabled") is False:
            continue
        missing = [p for p in m.get("paths") or [] if snap.get("ok") and p not in path_bps]
        if missing:
            flag(WARN, f"모니터 [{m.get('title')}] path 없음: {', '.join(missing)}")

    return {
        "health": level,
        "issues": issues,
        "summary": {
            "encoders": counts,
            "mediamtx_ok": mediamtx["ok"],
            "cpu_percent": cpu,
            "mem_available_percent": system["mem_available_percent"],
        },
        "system": system,
        "mediamtx": mediamtx,
        "encoders": encs,
        "ts": now,
    }


def to_text(doc):
    """Short plain-text rendering (Telegram message friendly)."""
    icon = {OK: "✅", WARN: "⚠️", ERROR: "❌"}
    s, c = doc["system"], doc["summary"]["encoders"]
    up = int(s["backend_uptime_sec"])
    lines = [
        f"{icon[doc['health']]} mediarouter {s['hostname']} — {doc['health'].upper()}",
        f"{s['time']} · backend up {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m",
        f"CPU {s['cpu_percent']:.0f}% · RAM avail {s['mem_available_percent']}% · disk {s['disk_used_percent']}% · ffmpeg {s['ffmpeg_processes']}",
        f"mediamtx {'OK' if doc['mediamtx']['ok'] else 'DOWN'} · paths {doc['mediamtx']['paths_ready']}/{doc['mediamtx']['paths_total']}"
        f" · readers {doc['mediamtx']['readers_total']}",
    ]
    for g in s.get("gpus") or []:
        f = lambda v, u="%": f"{v:.0f}{u}" if v is not None else "-"
        lines.append(f"GPU{g['index']} {g['name']} · gpu {f(g['util_gpu'])} enc {f(g['util_enc'])} dec {f(g['util_dec'])}"
                     f" · {f(g['temp_c'], '°C')} · {f(g['power_w'], 'W')}")
    lines += [
        "encoders " + ", ".join(f"{k} {v}" for k, v in c.items()),
    ]
    for e in doc["encoders"]:
        br = f"{e['bitrate_bps'] / 1e6:.2f}Mb/s" if e["bitrate_bps"] else "-"
        lines.append(f"{icon[e['health']]} {e['title']}: {e['state']} · fps {e['fps'] or '-'} · "
                     f"speed {e['speed'] if e['speed'] is not None else '-'}x · {br} · restarts {e['restarts']}")
    if doc["issues"]:
        lines.append("")
        lines += [f"- {i['message']}" for i in doc["issues"]]
    return "\n".join(lines)
