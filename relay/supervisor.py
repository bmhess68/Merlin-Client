"""Spawns one ffmpeg per enabled camera. Each ffmpeg pulls the camera and
publishes it into the box-local MediaMTX as path "<slug>". MediaMTX records
locally (per its config) and serves the same path on demand to the cloud
MediaMTX over Tailscale.

The box does NOT push to the cloud directly. Recording is MediaMTX-native;
this supervisor no longer manages segment files. Hot-reloads on config.json
change. Per-camera ffmpeg output is line-prefixed.
"""
from __future__ import annotations

import json
import signal
import subprocess
import threading
import time
from pathlib import Path

CONFIG_PATH = Path("/data/config.json")
STATUS_PATH = Path("/data/status.json")
THUMBNAILS_ROOT = Path("/data/thumbnails")
POLL_SECONDS = 2
RESTART_BACKOFF_SECONDS = 3
SNAPSHOT_EVERY_SECONDS = 60


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def merge(camera: dict, defaults: dict) -> dict:
    merged = dict(defaults)
    for key, value in camera.items():
        if value is None or value == "":
            continue
        merged[key] = value
    return merged


def signature(cam: dict) -> str:
    """Fields that, when changed, require restarting the ffmpeg child.
    Recording-related fields are NOT here — those are handled by mediamtx
    config reload without bouncing the ingest stream.
    """
    fields = (
        cam.get("slug", ""),
        cam.get("sourceUrl", ""),
        cam.get("rtspTransport", "tcp"),
        str(cam.get("tlsVerify", False)),
        cam.get("logLevel", "warning"),
    )
    return "|".join(fields)


def ffmpeg_cmd(cam: dict) -> list[str]:
    target = f"rtsp://mediamtx:8554/{cam['slug']}"
    args = ["ffmpeg", "-loglevel", cam.get("logLevel", "warning")]
    if not cam.get("tlsVerify", False):
        args += ["-tls_verify", "0"]
    args += [
        "-rtsp_transport", cam.get("rtspTransport", "tcp"),
        "-i", cam["sourceUrl"],
        "-c", "copy",
        "-map", "0:v:0",
        "-an",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        target,
    ]
    return args


def desired_cameras(cfg: dict) -> dict[str, dict]:
    site = cfg.get("site", {})
    if not site.get("slug"):
        return {}
    defaults = cfg.get("defaults", {})
    out: dict[str, dict] = {}
    for cam in cfg.get("cameras", []):
        if not cam.get("enabled", True):
            continue
        slug = cam.get("slug")
        url = cam.get("sourceUrl")
        if not slug or not url:
            continue
        out[slug] = merge(cam, defaults)
    return out


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def pump_logs(slug: str, stream) -> None:
    while True:
        line = stream.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            continue
        if text:
            print(f"[{slug}] {text}", flush=True)
    try:
        stream.close()
    except Exception:
        pass


def take_snapshot(slug: str) -> None:
    """Pull one frame from the box-local mediamtx path and save as JPEG.
    No-op if the path isn't ready yet (ffmpeg returns non-zero, file isn't
    moved into place)."""
    THUMBNAILS_ROOT.mkdir(parents=True, exist_ok=True)
    final = THUMBNAILS_ROOT / f"{slug}.jpg"
    tmp = THUMBNAILS_ROOT / f"{slug}.tmp.jpg"
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", f"rtsp://mediamtx:8554/{slug}",
        "-frames:v", "1",
        "-vf", "scale=640:-2",
        "-q:v", "5",
        str(tmp),
    ]
    try:
        result = subprocess.run(
            cmd, timeout=12,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and tmp.exists():
            tmp.replace(final)
        else:
            try:
                tmp.unlink()
            except OSError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        try:
            tmp.unlink()
        except OSError:
            pass


def write_status(procs: dict[str, dict], site_slug: str, now: float) -> None:
    cameras = []
    for slug, st in procs.items():
        proc: subprocess.Popen = st["proc"]
        alive = proc.poll() is None
        cameras.append({
            "slug": slug,
            "running": alive,
            "uptimeSeconds": int(now - st["started"]) if alive else 0,
            "lastExit": None if alive else proc.returncode,
            "restartCount": st.get("restart_count", 0),
            "lastFailureAt": st.get("last_failure_at"),
        })
    payload = {"site": site_slug, "updated": int(now), "cameras": cameras}
    try:
        STATUS_PATH.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


_running = True


def _shutdown(*_):
    global _running
    _running = False


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    procs: dict[str, dict] = {}
    last_snapshot: dict[str, float] = {}

    while _running:
        cfg = load_config()
        site = cfg.get("site", {"slug": ""})
        desired = desired_cameras(cfg)
        now = time.time()

        for slug in list(procs.keys()):
            st = procs[slug]
            keep = slug in desired and signature(desired[slug]) == st["sig"]
            if keep:
                continue
            print(f"[{slug}] stopping", flush=True)
            stop(st["proc"])
            if slug not in desired:
                procs.pop(slug)

        for slug, cam in desired.items():
            st = procs.get(slug)
            if st and st["proc"].poll() is None:
                continue
            if st is not None and st["proc"].returncode is not None and not st.get("logged_exit"):
                print(f"[{slug}] exited with code {st['proc'].returncode}", flush=True)
                st["logged_exit"] = True
                st["last_failure_at"] = now
            if st and (now - st["started"]) < RESTART_BACKOFF_SECONDS:
                continue
            restart_count = (st.get("restart_count", 0) + 1) if st else 0
            last_failure_at = st.get("last_failure_at") if st else None
            cmd = ffmpeg_cmd(cam)
            print(f"[{slug}] starting: {' '.join(cmd)}", flush=True)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
            log_thread = threading.Thread(target=pump_logs, args=(slug, proc.stdout), daemon=True)
            log_thread.start()
            procs[slug] = {
                "proc": proc,
                "sig": signature(cam),
                "started": now,
                "restart_count": restart_count,
                "last_failure_at": last_failure_at,
                "log_thread": log_thread,
            }

        for slug, st in procs.items():
            if st["proc"].poll() is not None:
                continue
            if (now - st["started"]) < 5:
                continue
            if (now - last_snapshot.get(slug, 0)) >= SNAPSHOT_EVERY_SECONDS:
                last_snapshot[slug] = now
                take_snapshot(slug)

        write_status(procs, site.get("slug", ""), now)
        time.sleep(POLL_SECONDS)

    for st in procs.values():
        stop(st["proc"])


if __name__ == "__main__":
    main()
