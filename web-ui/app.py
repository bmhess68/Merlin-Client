from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, request

try:
    import docker  # docker SDK; mount /var/run/docker.sock to use
    _docker_client = docker.from_env()
except Exception:  # noqa: BLE001
    _docker_client = None

app = Flask(__name__)

DATA_DIR = Path("/data")
CONFIG_PATH = DATA_DIR / "config.json"
STATUS_PATH = DATA_DIR / "status.json"
MEDIAMTX_PATH = DATA_DIR / "mediamtx.yml"
RECORDINGS_ROOT = DATA_DIR / "recordings"
THUMBNAILS_ROOT = DATA_DIR / "thumbnails"

HOST_PROC = Path("/host/proc")

DEFAULT_CONFIG = {
    "version": 3,
    "site": {"slug": "site1", "displayName": ""},
    "cloud": {
        "tailnetHost": "",
        "playbackHost": "",
        "healthUrl": "",
        "adminApiUrl": "",      # http://<cloud>/api/v1/admin/cloud-pull-cameras
        "mediamtxApiUrl": "",   # legacy; unused by the new admin-API flow
    },
    "defaults": {
        "rtspTransport": "tcp",
        "tlsVerify": False,
        "logLevel": "warning",
        "record": True,
        "retainHours": 168,
    },
    "cameras": [],
}

VALID_TRANSPORTS = {"tcp", "udp"}
VALID_LOG_LEVELS = {
    "quiet", "panic", "fatal", "error", "warning", "info", "verbose", "debug", "trace",
}

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"

# Shared password for the cloud Caddy → mediamtx playback proxy.
# When unset, a placeholder password is written so mediamtx still loads;
# playback auth simply won't succeed until the env var is set.
MERLINREAD_PASSWORD = os.environ.get(
    "MERLINREAD_PASSWORD",
    "unset-set-MERLINREAD_PASSWORD-in-env",
).strip()

# Control-plane API key for POSTing camera registrations to the cloud's
# /api/v1/admin/cloud-pull-cameras endpoint. Cloud is master — it owns
# mediamtx path generation. This key is sent as the `x-api-key` header.
# Set in .env on this box; never written to git or config.json.
CONTROL_API_KEY = os.environ.get("CONTROL_API_KEY", "").strip()

MEDIAMTX_API = "http://mediamtx:9997/v3/config"


# --- config + validation -----------------------------------------------------

def normalize_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", str(value).lower())
    return re.sub(r"-+", "-", cleaned).strip("-")


def migrate_legacy(legacy: dict) -> dict:
    """v1 (single-camera scalar) → v3 (cloud-pull schema)."""
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["site"]["slug"] = legacy.get("siteSlug") or "site1"
    if legacy.get("camera1SourceUrl"):
        cfg["cameras"].append({
            "slug": normalize_slug(legacy.get("camera1Slug", "camera1")),
            "displayName": "",
            "sourceUrl": legacy["camera1SourceUrl"],
            "rtspTransport": (legacy.get("camera1RtspTransport") or "tcp").lower(),
            "tlsVerify": False,
            "logLevel": (legacy.get("relayLogLevel") or "warning").lower(),
            "record": True,
            "retainHours": 168,
            "enabled": True,
        })
    return cfg


def upgrade_to_v3(cfg: dict) -> dict:
    """v2 (push-to-cloud schema) → v3 (cloud-pull schema). Drops cloud.host and
    cloud.rtspPort (no longer relevant); preserves them as playbackHost so the
    UI can still display playback URLs against the same hostname.
    """
    cloud = cfg.get("cloud", {}) or {}
    new_cloud = {
        "tailnetHost": cloud.get("tailnetHost", ""),
        "playbackHost": cloud.get("playbackHost") or cloud.get("host", ""),
        "healthUrl": cloud.get("healthUrl", ""),
    }
    cfg["cloud"] = new_cloud
    cfg["version"] = 3
    defaults = cfg.setdefault("defaults", {})
    defaults.setdefault("record", True)
    defaults.setdefault("retainHours", 168)
    for cam in cfg.setdefault("cameras", []):
        cam.setdefault("record", True)
        cam.setdefault("retainHours", 168)
    return cfg


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        cfg = deepcopy(DEFAULT_CONFIG)
        save_config(cfg, write_mediamtx=True)
        return cfg
    raw = json.loads(CONFIG_PATH.read_text())
    if "version" not in raw:
        cfg = migrate_legacy(raw)
        save_config(cfg, write_mediamtx=True)
        return cfg
    if raw.get("version", 0) < 3:
        cfg = upgrade_to_v3(raw)
        save_config(cfg, write_mediamtx=True)
        return cfg
    return raw


def save_config(cfg: dict, *, write_mediamtx: bool = True) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)
    if write_mediamtx:
        write_mediamtx_yml(cfg)
        sync_mediamtx_paths(cfg)
        register_cameras_with_cloud(cfg)


def write_mediamtx_yml(cfg: dict) -> None:
    header = (
        "logLevel: info\n"
        "\n"
        "api: yes\n"
        "apiAddress: :9997\n"
        "\n"
        "metrics: yes\n"
        "metricsAddress: :9998\n"
        "\n"
        "rtsp: yes\n"
        "rtspAddress: :8554\n"
        "rtspTransports: [tcp]\n"
        "\n"
        "playback: yes\n"
        "playbackAddress: :9996\n"
        "\n"
        "webrtc: no\n"
        "hls: no\n"
        "rtmp: no\n"
        "srt: no\n"
        "\n"
        "authMethod: internal\n"
        "\n"
        "authInternalUsers:\n"
        "  # Tailnet-internal: cloud's RTSP pull + edge supervisor publish.\n"
        "  # 100.64.0.0/10 is the Tailscale CGNAT range — covers all tailnet IPs.\n"
        "  - user: any\n"
        "    ips: [100.64.0.0/10, 172.16.0.0/12, 127.0.0.1, '::1']\n"
        "    permissions:\n"
        "      - action: publish\n"
        "      - action: read\n"
        "\n"
        "  # Public-facing playback: gated by the shared read credential\n"
        "  # the Merlin web app passes via cloud Caddy for HLS / DVR.\n"
        "  # Password from MERLINREAD_PASSWORD env var (set in .env).\n"
        "  - user: merlinread\n"
        f"    pass: {MERLINREAD_PASSWORD}\n"
        "    permissions:\n"
        "      - action: playback\n"
        "\n"
        "  # API / metrics / pprof: loopback + Docker bridge only.\n"
        "  - user: any\n"
        "    ips: [127.0.0.1, '::1', 172.16.0.0/12]\n"
        "    permissions:\n"
        "      - action: api\n"
        "      - action: metrics\n"
        "      - action: pprof\n"
        "\n"
        "pathDefaults:\n"
        "  recordPath: /recordings/%path/%Y-%m-%d_%H-%M-%S-%f\n"
        "  recordFormat: fmp4\n"
        "  recordPartDuration: 1s\n"
        "  recordSegmentDuration: 1h\n"
        "\n"
    )
    lines = ["paths:"]
    cams = [c for c in cfg.get("cameras", []) if c.get("slug")]
    if not cams:
        lines = ["paths: {}"]
    else:
        for cam in cams:
            slug = cam["slug"]
            record = bool(cam.get("record", False))
            retain = int(cam.get("retainHours", 24) or 24)
            lines.append(f"  {slug}:")
            lines.append(f"    record: {'yes' if record else 'no'}")
            if record:
                lines.append(f"    recordDeleteAfter: {retain}h")
    MEDIAMTX_PATH.write_text(header + "\n".join(lines) + "\n")


def _mediamtx_call(method: str, path: str, body: dict | None = None,
                   base: str = MEDIAMTX_API, timeout: int = 5) -> tuple[int, str]:
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def register_cameras_with_cloud(cfg: dict) -> dict:
    """POST the box's current camera list to the cloud's admin endpoint.
    Cloud is master — it owns mediamtx path generation. Body shape:

        { "site": "<slug>",
          "tailnetHost": "<box>.<tailnet>.ts.net",
          "cameras": [{"slug": "...", "label": "..."}, ...] }

    Authenticated with `x-api-key: <CONTROL_API_KEY>`. Re-posting replaces
    the cloud's saved camera list for this site.
    """
    result: dict = {
        "ok": False, "skipped": None,
        "registered": [], "cameraCount": 0, "errors": [],
    }
    cloud = cfg.get("cloud", {}) or {}
    api = cloud.get("adminApiUrl", "").strip().rstrip("/")
    if not api:
        result["skipped"] = "Cloud admin API URL is not set in Site & Cloud."
        return result
    if not CONTROL_API_KEY:
        result["skipped"] = (
            "CONTROL_API_KEY env var is not set on the box. "
            "Add to .env and restart the installer-ui container."
        )
        return result
    site_slug = cfg["site"]["slug"]
    tailnet_host = cloud.get("tailnetHost", "").strip()
    if not tailnet_host:
        result["skipped"] = "Box's Tailscale hostname is not set in Site & Cloud."
        return result

    cameras = []
    for cam in cfg.get("cameras", []):
        if not cam.get("enabled", True):
            continue
        slug = cam.get("slug")
        if not slug:
            continue
        cameras.append({
            "slug": slug,
            "label": cam.get("displayName") or slug,
        })

    body = {
        "site": site_slug,
        "tailnetHost": tailnet_host,
        "cameras": cameras,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        api,
        data=data,
        headers={
            "content-type": "application/json",
            "x-api-key": CONTROL_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
                result["registered"] = payload.get("streamPaths", [])
                result["cameraCount"] = payload.get("cameraCount", len(cameras))
                result["ok"] = True
            except json.JSONDecodeError:
                result["errors"].append(
                    f"cloud accepted (HTTP {r.status}) but returned non-JSON body: {raw[:200]}"
                )
                # Treat 2xx as success even if body is unparseable
                if 200 <= r.status < 300:
                    result["ok"] = True
                    result["cameraCount"] = len(cameras)
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        result["errors"].append(f"HTTP {exc.code}: {err_body[:200] or exc.reason}")
    except (urllib.error.URLError, OSError) as exc:
        result["errors"].append(f"cannot connect: {exc}")

    return result


def sync_mediamtx_paths(cfg: dict) -> None:
    """Push path config to the running mediamtx via API. Per-path add/patch/delete.
    Idempotent and forgiving — if mediamtx isn't reachable yet, log and continue;
    the disk file (mediamtx.yml) is the source of truth on cold start."""
    desired: dict[str, dict] = {}
    for cam in cfg.get("cameras", []):
        slug = cam.get("slug")
        if not slug:
            continue
        record = bool(cam.get("record", False))
        retain = int(cam.get("retainHours", 24) or 24)
        body = {"record": record}
        if record:
            body["recordDeleteAfter"] = f"{retain}h"
        desired[slug] = body

    status, raw = _mediamtx_call("GET", "/paths/list")
    if status != 200:
        print(f"mediamtx path list failed ({status}): {raw}", flush=True)
        return
    try:
        items = json.loads(raw).get("items", [])
    except json.JSONDecodeError:
        items = []
    current = {item.get("name"): item for item in items if item.get("name")}

    for slug, body in desired.items():
        if slug in current:
            s, m = _mediamtx_call("PATCH", f"/paths/patch/{slug}", body)
        else:
            s, m = _mediamtx_call("POST", f"/paths/add/{slug}", body)
        if s not in (200, 201):
            print(f"mediamtx upsert {slug} → {s}: {m}", flush=True)

    for slug in current:
        if slug in desired or slug.startswith("~") or slug == "all_others":
            continue
        s, m = _mediamtx_call("DELETE", f"/paths/delete/{slug}")
        if s not in (200, 204):
            print(f"mediamtx delete {slug} → {s}: {m}", flush=True)


def validate_site(payload: dict) -> dict:
    slug = normalize_slug(payload.get("slug", ""))
    if not slug:
        raise ValueError("site slug is required")
    return {"slug": slug, "displayName": str(payload.get("displayName", ""))}


def _strip_scheme(value: str) -> str:
    v = value.strip()
    for prefix in ("https://", "http://"):
        if v.lower().startswith(prefix):
            v = v[len(prefix):]
            break
    return v.rstrip("/")


def _ensure_url(value: str) -> str:
    """Auto-prepend http:// when a host-only value was pasted."""
    v = value.strip().rstrip("/")
    if not v:
        return ""
    if not (v.lower().startswith("http://") or v.lower().startswith("https://")):
        v = "http://" + v
    return v


def validate_cloud(payload: dict) -> dict:
    return {
        "tailnetHost": _strip_scheme(str(payload.get("tailnetHost", ""))),
        "playbackHost": _strip_scheme(str(payload.get("playbackHost", ""))),
        "healthUrl": _ensure_url(str(payload.get("healthUrl", ""))),
        "adminApiUrl": _ensure_url(str(payload.get("adminApiUrl", ""))),
        "mediamtxApiUrl": _ensure_url(str(payload.get("mediamtxApiUrl", ""))),
    }


def validate_defaults(payload: dict) -> dict:
    transport = str(payload.get("rtspTransport", "tcp")).lower()
    if transport not in VALID_TRANSPORTS:
        raise ValueError("rtspTransport must be tcp or udp")
    log_level_raw = payload.get("logLevel") or "warning"
    log_level = str(log_level_raw).lower()
    if log_level not in VALID_LOG_LEVELS:
        raise ValueError(f"logLevel '{log_level}' is invalid (allowed: {', '.join(sorted(VALID_LOG_LEVELS))})")
    try:
        retain_hours = int(payload.get("retainHours", 168))
    except (TypeError, ValueError) as exc:
        raise ValueError("retainHours must be a number") from exc
    if retain_hours < 1:
        raise ValueError("retainHours must be >= 1")
    return {
        "rtspTransport": transport,
        "tlsVerify": bool(payload.get("tlsVerify", False)),
        "logLevel": log_level,
        "record": bool(payload.get("record", True)),
        "retainHours": retain_hours,
    }


def validate_camera(payload: dict, conflicting_slugs: set[str]) -> dict:
    slug = normalize_slug(payload.get("slug", ""))
    if not slug:
        raise ValueError("slug is required")
    if slug in conflicting_slugs:
        raise ValueError(f"camera slug '{slug}' already exists")
    url = str(payload.get("sourceUrl", "")).strip()
    if not (url.startswith("rtsp://") or url.startswith("rtsps://")):
        raise ValueError("sourceUrl must start with rtsp:// or rtsps://")
    transport = str(payload.get("rtspTransport", "tcp")).lower()
    if transport not in VALID_TRANSPORTS:
        raise ValueError("rtspTransport must be tcp or udp")
    log_level_raw = payload.get("logLevel")
    if log_level_raw in (None, ""):
        log_level = ""
    else:
        log_level = str(log_level_raw).lower()
        if log_level not in VALID_LOG_LEVELS:
            raise ValueError(f"logLevel '{log_level}' is invalid (allowed: {', '.join(sorted(VALID_LOG_LEVELS))})")
    try:
        retain_hours = int(payload.get("retainHours", 168))
    except (TypeError, ValueError) as exc:
        raise ValueError("retainHours must be a number") from exc
    if retain_hours < 1:
        raise ValueError("retainHours must be >= 1")
    return {
        "slug": slug,
        "displayName": str(payload.get("displayName", "")),
        "sourceUrl": url,
        "rtspTransport": transport,
        "tlsVerify": bool(payload.get("tlsVerify", False)),
        "logLevel": log_level or None,
        "record": bool(payload.get("record", True)),
        "retainHours": retain_hours,
        "enabled": bool(payload.get("enabled", True)),
    }


def stream_urls(cfg: dict) -> dict:
    """Per-camera URL set:
       - boxLocalUrl: what the cloud mediamtx puts in its source: line
       - cloudPath:   the cloud path namespace (live/<site>-<slug>)
       - playbackUrl: what a browser opens (informational)
    """
    site_slug = cfg["site"]["slug"]
    cloud = cfg.get("cloud", {})
    tailnet_host = cloud.get("tailnetHost", "").strip() or f"<{site_slug}-tailnet-host>"
    playback_host = cloud.get("playbackHost", "").strip()
    out = {}
    for cam in cfg.get("cameras", []):
        slug = cam["slug"]
        cloud_path = f"live/{site_slug}-{slug}"
        playback = f"http://{playback_host}/{cloud_path}/index.m3u8" if playback_host else ""
        out[slug] = {
            "boxLocalUrl":  f"rtsp://{tailnet_host}:8554/{slug}",
            "cloudPath":    cloud_path,
            "playbackUrl":  playback,
        }
    return out


def supervisor_status() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def cloud_paths_yaml(cfg: dict) -> str:
    """Emit a copy-paste mediamtx paths block for the cloud server,
    one path per camera on this box.

    `rtspTransport: tcp` is required because the box's mediamtx is
    configured with `rtspTransports: [tcp]`. Without it, the cloud's
    puller falls back to `automatic`, attempts UDP first, the box
    returns RTSP 400 Bad Request, and the cloud gives up that pull
    cycle. Diagnosed 2026-04-29.
    """
    site_slug = cfg["site"]["slug"]
    tailnet_host = cfg.get("cloud", {}).get("tailnetHost", "").strip() or f"<{site_slug}-tailnet-host>"
    lines = ["paths:"]
    for cam in cfg.get("cameras", []):
        if not cam.get("enabled", True):
            continue
        slug = cam["slug"]
        lines.append(f"  {site_slug}-{slug}:")
        lines.append(f"    source: rtsp://{tailnet_host}:8554/{slug}")
        lines.append(f"    sourceOnDemand: yes")
        lines.append(f"    sourceOnDemandStartTimeout: 15s")
        lines.append(f"    sourceOnDemandCloseAfter: 30s")
        lines.append(f"    rtspTransport: tcp")
    return "\n".join(lines) + "\n"


# --- host stats --------------------------------------------------------------

_host_state = {"cpu": None, "net": None}


def _read_proc(path: str) -> str | None:
    p = HOST_PROC / path
    try:
        return p.read_text()
    except OSError:
        return None


def host_cpu_percent() -> float | None:
    raw = _read_proc("stat")
    if not raw:
        return None
    parts = raw.splitlines()[0].split()
    if parts[0] != "cpu":
        return None
    nums = [int(x) for x in parts[1:8]]
    idle = nums[3] + nums[4]
    total = sum(nums)
    prev = _host_state["cpu"]
    _host_state["cpu"] = (idle, total)
    if prev is None:
        return None
    didle = idle - prev[0]
    dtotal = total - prev[1]
    if dtotal <= 0:
        return None
    return round(100.0 * (1.0 - didle / dtotal), 1)


def host_memory() -> dict | None:
    raw = _read_proc("meminfo")
    if not raw:
        return None
    fields = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    def _kb(k):
        try:
            return int(fields[k].split()[0]) * 1024
        except (KeyError, ValueError, IndexError):
            return None
    total = _kb("MemTotal")
    avail = _kb("MemAvailable")
    if total is None or avail is None:
        return None
    return {
        "totalBytes": total,
        "availableBytes": avail,
        "usedPercent": round(100.0 * (total - avail) / total, 1),
    }


def host_network_mbps() -> float | None:
    raw = _read_proc("net/dev")
    if not raw:
        return None
    tx_total = 0
    for line in raw.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith("docker") or name.startswith("br-") or name.startswith("veth"):
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            tx_total += int(cols[8])
        except ValueError:
            continue
    now = time.time()
    prev = _host_state["net"]
    _host_state["net"] = (tx_total, now)
    if prev is None:
        return None
    dbytes = tx_total - prev[0]
    dtime = now - prev[1]
    if dtime <= 0:
        return None
    return round((dbytes * 8.0) / (dtime * 1_000_000), 2)


def disk_stats() -> dict | None:
    try:
        usage = shutil.disk_usage("/data")
    except OSError:
        return None
    return {
        "totalBytes": usage.total,
        "freeBytes": usage.free,
        "usedPercent": round(100.0 * (usage.total - usage.free) / usage.total, 1),
    }


def recordings_summary() -> dict:
    if not RECORDINGS_ROOT.exists():
        return {"totalBytes": 0, "perCamera": {}}
    total = 0
    per_cam: dict[str, dict] = {}
    try:
        for slug_dir in RECORDINGS_ROOT.iterdir():
            if not slug_dir.is_dir():
                continue
            cam_total = 0
            cam_count = 0
            oldest = None
            for f in slug_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    stat = f.stat()
                except OSError:
                    continue
                cam_total += stat.st_size
                cam_count += 1
                if oldest is None or stat.st_mtime < oldest:
                    oldest = stat.st_mtime
            total += cam_total
            per_cam[slug_dir.name] = {
                "sizeBytes": cam_total,
                "fileCount": cam_count,
                "oldestEpoch": int(oldest) if oldest else None,
            }
    except OSError:
        pass
    return {"totalBytes": total, "perCamera": per_cam}


# --- compose service inspection + log tail ----------------------------------

COMPOSE_PROJECT = "merlin-edge"
SERVICES = ("installer-ui", "mediamtx", "relay")

# Match credentials in URLs and replace the password part.
# rtsp://user:pass@host  →  rtsp://user:****@host
_CRED_RE = re.compile(r"(rtsps?://)([^:/@\s]+):([^@\s]+)@", re.IGNORECASE)


def redact(line: str) -> str:
    return _CRED_RE.sub(r"\1\2:****@", line)


def list_services() -> list[dict]:
    if _docker_client is None:
        return [{"name": s, "state": "unknown", "error": "docker socket not available"} for s in SERVICES]
    try:
        cs = _docker_client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"},
        )
    except Exception as exc:  # noqa: BLE001
        return [{"name": s, "state": "unknown", "error": str(exc)} for s in SERVICES]

    by_service = {c.labels.get("com.docker.compose.service"): c for c in cs}
    out = []
    for svc in SERVICES:
        c = by_service.get(svc)
        if c is None:
            out.append({"name": svc, "state": "missing"})
            continue
        try:
            attrs = c.attrs
            state = attrs.get("State", {}).get("Status", c.status)
            started_at = attrs.get("State", {}).get("StartedAt", "")
            restarting = attrs.get("State", {}).get("Restarting", False)
            restart_count = attrs.get("RestartCount", 0)
            out.append({
                "name": svc,
                "container": c.name,
                "state": state,
                "restarting": restarting,
                "restartCount": restart_count,
                "startedAt": started_at,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"name": svc, "state": "unknown", "error": str(exc)})
    return out


def tail_logs(service: str, n: int = 200) -> list[str]:
    if service not in SERVICES:
        return [f"unknown service: {service}"]
    if _docker_client is None:
        return ["docker socket not available — installer-ui can't read container logs"]
    try:
        cs = _docker_client.containers.list(
            all=True,
            filters={
                "label": [
                    f"com.docker.compose.project={COMPOSE_PROJECT}",
                    f"com.docker.compose.service={service}",
                ],
            },
        )
        if not cs:
            return [f"no container found for service '{service}'"]
        c = cs[0]
        raw = c.logs(tail=n, timestamps=False)
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)
        return [redact(line) for line in text.splitlines()]
    except Exception as exc:  # noqa: BLE001
        return [f"log read failed: {exc}"]


# --- cloud connectivity test -------------------------------------------------

def cloud_test(cfg: dict) -> dict:
    cloud = cfg.get("cloud", {}) or {}
    checks = []

    api = cloud.get("adminApiUrl", "").strip()
    if not api:
        checks.append({
            "name": "Cloud admin API URL",
            "ok": False,
            "message": "Not set. Add it in Site & Cloud and Save (e.g. http://merlin-cloud/api/v1/admin/cloud-pull-cameras).",
        })
    elif not CONTROL_API_KEY:
        checks.append({
            "name": "CONTROL_API_KEY env var",
            "ok": False,
            "message": "Not set. Add CONTROL_API_KEY=... to .env and `docker compose up -d --build installer-ui`.",
        })
    else:
        # Probe with HEAD; expect 405 Method Not Allowed if the endpoint exists
        # (it only accepts POST), 401/403 if our key is wrong, etc.
        try:
            req = urllib.request.Request(
                api, method="HEAD",
                headers={"x-api-key": CONTROL_API_KEY},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                checks.append({
                    "name": f"Cloud admin API ({api})",
                    "ok": True,
                    "message": f"reachable (HTTP {r.status})",
                })
        except urllib.error.HTTPError as exc:
            if exc.code == 405:
                checks.append({
                    "name": f"Cloud admin API ({api})",
                    "ok": True,
                    "message": "reachable (HTTP 405 — endpoint expects POST, which the Sync button does)",
                })
            elif exc.code in (401, 403):
                checks.append({
                    "name": f"Cloud admin API ({api})",
                    "ok": False,
                    "message": f"auth denied (HTTP {exc.code}). Check CONTROL_API_KEY value matches what the cloud expects.",
                })
            elif exc.code == 404:
                checks.append({
                    "name": f"Cloud admin API ({api})",
                    "ok": False,
                    "message": f"HTTP 404 — URL is wrong. Should be http://<cloud>/api/v1/admin/cloud-pull-cameras",
                })
            else:
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    body = ""
                checks.append({
                    "name": f"Cloud admin API ({api})",
                    "ok": False,
                    "message": f"HTTP {exc.code}: {body[:160] or exc.reason}",
                })
        except (urllib.error.URLError, OSError) as exc:
            checks.append({
                "name": f"Cloud admin API ({api})",
                "ok": False,
                "message": f"cannot connect: {exc}. If the host is a tailnet name, the installer-ui container's DNS may not resolve it; use the tailnet IP or fix Docker daemon DNS.",
            })

    health = cloud.get("healthUrl", "").strip()
    if not health:
        checks.append({
            "name": "Cloud health URL",
            "ok": True,
            "message": "not configured (optional — leave empty if you don't have a receiver yet)",
        })
    else:
        try:
            req = urllib.request.Request(health, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as r:
                checks.append({
                    "name": f"Cloud health URL ({health})",
                    "ok": True,
                    "message": f"reachable (HTTP {r.status})",
                })
        except urllib.error.HTTPError as e:
            # 404/405 etc still means we reached the host
            checks.append({
                "name": f"Cloud health URL ({health})",
                "ok": True,
                "message": f"reachable (HTTP {e.code} — endpoint may not be implemented yet, but the host responded)",
            })
        except (urllib.error.URLError, OSError) as e:
            checks.append({
                "name": f"Cloud health URL ({health})",
                "ok": False,
                "message": f"cannot reach: {e}",
            })

    tailnet_host = cloud.get("tailnetHost", "").strip()
    if not tailnet_host:
        checks.append({
            "name": "Box's Tailscale hostname",
            "ok": False,
            "message": "Not set — cloud paths emitted with placeholder. Run `tailscale status --self --json` and paste DNSName.",
        })
    else:
        checks.append({
            "name": "Box's Tailscale hostname",
            "ok": True,
            "message": tailnet_host,
        })

    overall_ok = all(c["ok"] for c in checks)
    return {"ok": overall_ok, "checks": checks}


# --- cloud health POST (background) ------------------------------------------

def _cloud_health_loop() -> None:
    while True:
        try:
            cfg = load_config()
            url = cfg.get("cloud", {}).get("healthUrl", "").strip()
            if url:
                payload = build_health(cfg)
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    url, data=data,
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=10).read()
                except urllib.error.URLError as exc:
                    print(f"cloud health POST failed: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"cloud health loop error: {exc}", flush=True)
        time.sleep(60)


def build_health(cfg: dict) -> dict:
    return {
        "site": cfg["site"]["slug"],
        "tailnetHost": cfg["cloud"].get("tailnetHost", ""),
        "playbackHost": cfg["cloud"].get("playbackHost", ""),
        "cameraCount": len(cfg.get("cameras", [])),
        "supervisor": supervisor_status(),
        "host": {
            "cpuPercent": host_cpu_percent(),
            "memory": host_memory(),
            "disk": disk_stats(),
            "networkOutMbps": host_network_mbps(),
            "recordings": recordings_summary(),
        },
        "reportedAt": int(time.time()),
    }


# --- auth --------------------------------------------------------------------

def _check_auth(auth) -> bool:
    if not ADMIN_PASSWORD:
        return True
    if not auth:
        return False
    return auth.username == ADMIN_USERNAME and auth.password == ADMIN_PASSWORD


def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _check_auth(request.authorization):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Merlin Edge"'},
            )
        return fn(*args, **kwargs)
    return wrapper


# --- routes ------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
@auth_required
def api_get_config():
    cfg = load_config()
    return jsonify({
        "config": cfg,
        "urls": stream_urls(cfg),
        "cloudPathsYaml": cloud_paths_yaml(cfg),
    })


@app.put("/api/site")
@auth_required
def api_put_site():
    cfg = load_config()
    try:
        cfg["site"] = validate_site(request.get_json(force=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    save_config(cfg)
    return jsonify({"saved": True, "config": cfg, "urls": stream_urls(cfg)})


@app.put("/api/cloud")
@auth_required
def api_put_cloud():
    cfg = load_config()
    cfg["cloud"] = validate_cloud(request.get_json(force=True))
    save_config(cfg, write_mediamtx=True)  # triggers cloud sync if mediamtxApiUrl/tailnetHost changed
    return jsonify({"saved": True, "config": cfg, "urls": stream_urls(cfg)})


@app.put("/api/defaults")
@auth_required
def api_put_defaults():
    cfg = load_config()
    try:
        cfg["defaults"] = validate_defaults(request.get_json(force=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    save_config(cfg)
    return jsonify({"saved": True, "config": cfg})


@app.get("/api/cameras")
@auth_required
def api_list_cameras():
    cfg = load_config()
    return jsonify({"cameras": cfg.get("cameras", []), "urls": stream_urls(cfg)})


@app.post("/api/cameras")
@auth_required
def api_add_camera():
    cfg = load_config()
    existing = {c["slug"] for c in cfg.get("cameras", [])}
    try:
        cam = validate_camera(request.get_json(force=True), existing)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg.setdefault("cameras", []).append(cam)
    save_config(cfg)
    return jsonify({"saved": True, "camera": cam, "urls": stream_urls(cfg)}), 201


@app.put("/api/cameras/<slug>")
@auth_required
def api_update_camera(slug: str):
    cfg = load_config()
    cams = cfg.get("cameras", [])
    idx = next((i for i, c in enumerate(cams) if c["slug"] == slug), -1)
    if idx < 0:
        return jsonify({"error": "camera not found"}), 404
    others = {c["slug"] for c in cams if c["slug"] != slug}
    payload = {**cams[idx], **request.get_json(force=True)}
    try:
        updated = validate_camera(payload, others)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    cams[idx] = updated
    save_config(cfg)
    return jsonify({"saved": True, "camera": updated, "urls": stream_urls(cfg)})


@app.delete("/api/cameras/<slug>")
@auth_required
def api_delete_camera(slug: str):
    cfg = load_config()
    cams = cfg.get("cameras", [])
    new_cams = [c for c in cams if c["slug"] != slug]
    if len(new_cams) == len(cams):
        return jsonify({"error": "camera not found"}), 404
    cfg["cameras"] = new_cams
    save_config(cfg)
    return jsonify({"saved": True, "urls": stream_urls(cfg)})


@app.get("/api/health")
@auth_required
def api_health():
    cfg = load_config()
    return jsonify(build_health(cfg))


@app.get("/api/snapshot/<slug>.jpg")
@auth_required
def api_snapshot(slug: str):
    safe_slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
    if not safe_slug or safe_slug != slug:
        return Response("not found", 404)
    path = THUMBNAILS_ROOT / f"{safe_slug}.jpg"
    if not path.exists():
        return Response("no snapshot yet", 404)
    return Response(path.read_bytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/services")
@auth_required
def api_services():
    return jsonify({"services": list_services()})


@app.get("/api/logs/<service>")
@auth_required
def api_logs(service: str):
    try:
        n = int(request.args.get("tail", 200))
    except (TypeError, ValueError):
        n = 200
    n = max(20, min(2000, n))
    return jsonify({"service": service, "lines": tail_logs(service, n=n)})


@app.post("/api/cloud-test")
@auth_required
def api_cloud_test():
    return jsonify(cloud_test(load_config()))


@app.post("/api/cloud-sync")
@auth_required
def api_cloud_sync():
    """Register this box's current camera list with the cloud's admin
    endpoint. Cloud is master and generates mediamtx paths from the
    registration. Same logic that runs on every save_config()."""
    return jsonify(register_cameras_with_cloud(load_config()))


@app.get("/api/cloud-config")
@auth_required
def api_cloud_config():
    """Returns a YAML snippet to drop into the cloud mediamtx config so it
    can pull this box's cameras on demand. Plain text."""
    cfg = load_config()
    return Response(cloud_paths_yaml(cfg), mimetype="text/yaml")


@app.get("/")
@auth_required
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Merlin Edge Installer</title>
  <style>
    :root {
      --bg: #eef3ec; --panel: #ffffff; --ink: #15231a; --muted: #587060;
      --line: #cfd8d1; --accent: #1f6b48; --accent-2: #d8efe1;
      --warn: #b3551e; --ok: #1f6b48;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
    }
    [hidden] { display: none !important; }
    body { margin: 0; background: linear-gradient(180deg, #e7efe8, #f5f8f3); color: var(--ink); }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 64px; }
    h1 { margin: 0 0 6px; font-size: 28px; }
    h2 { margin: 0 0 14px; font-size: 18px; }
    p { color: var(--muted); margin: 4px 0 18px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
            padding: 20px; box-shadow: 0 12px 40px rgba(20, 35, 26, 0.06); margin-bottom: 18px; }
    .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
    label { display: block; font-size: 13px; font-weight: 700; margin-bottom: 4px; }
    input, select, textarea { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid var(--line);
                    border-radius: 10px; font-size: 14px; background: white; font-family: inherit; }
    textarea { font-family: "SF Mono", Consolas, monospace; font-size: 12px; }
    input[type=checkbox] { width: auto; }
    .full { grid-column: 1 / -1; }
    button { border: 0; background: var(--accent); color: white; padding: 10px 16px; border-radius: 999px;
             font-size: 14px; font-weight: 700; cursor: pointer; }
    button.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
    button.danger { background: var(--warn); }
    .actions { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); font-size: 14px; vertical-align: top; }
    th { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
    .mono { font-family: "SF Mono", Consolas, monospace; font-size: 12px; word-break: break-all; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .pill.ok { background: var(--accent-2); color: var(--ok); }
    .pill.bad { background: #f7d8c8; color: var(--warn); }
    .pill.warn { background: #fbe8c8; color: #8a5a16; }
    .pill.off { background: #e7ecea; color: var(--muted); }
    .toast { padding: 10px 14px; border-radius: 12px; background: var(--accent-2); margin-top: 10px; }
    .toast.err { background: #f7d8c8; color: var(--warn); }
    .grid2 { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }
    @media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; } }
    details { margin-top: 8px; }
    summary { cursor: pointer; font-weight: 700; }
    .small { font-size: 12px; color: var(--muted); }
    .stat { display: flex; justify-content: space-between; align-items: baseline; padding: 6px 0; border-bottom: 1px dashed var(--line); }
    .stat:last-child { border-bottom: 0; }
    .stat .v { font-weight: 700; font-size: 15px; }
    .bar { height: 6px; background: #eaf1ec; border-radius: 999px; overflow: hidden; margin-top: 4px; }
    .bar > span { display: block; height: 100%; background: var(--accent); }
    .bar.warn > span { background: var(--warn); }
    .thumb { display: block; width: 220px; max-width: 100%; height: auto; border-radius: 8px;
             background: #d8e0d6 url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 220 124'><rect width='220' height='124' fill='%23d8e0d6'/><text x='50%' y='50%' fill='%23587060' font-family='sans-serif' font-size='14' text-anchor='middle' dominant-baseline='middle'>no snapshot yet</text></svg>") center/cover no-repeat;
             min-height: 124px; border: 1px solid var(--line); cursor: pointer; }
    .modal-overlay { position: fixed; inset: 0; background: rgba(20,35,26,0.55);
                     display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
    .modal { background: var(--panel); border-radius: 16px; padding: 22px; max-width: 600px; width: 100%;
             max-height: 90vh; overflow-y: auto; box-shadow: 0 24px 80px rgba(0,0,0,0.3); }
    .lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: flex;
                align-items: center; justify-content: center; z-index: 101; padding: 20px; cursor: pointer; }
    .lightbox img { max-width: 95vw; max-height: 95vh; border-radius: 8px; }
    .services-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 16px; }
    .logbox { max-height: 360px; overflow-y: auto; background: #15231a; color: #d6e6dc;
              padding: 12px; border-radius: 10px; font: 11px/1.45 "SF Mono", Consolas, monospace;
              white-space: pre-wrap; word-break: break-all; }
    .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
    .toolbar select, .toolbar input[type=number] { width: auto; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Merlin Edge Installer</h1>
    <p>Box-local cameras → MediaMTX (records + serves on demand) → cloud pulls over Tailscale when a Merlin user opens a camera.</p>
    <div class="services-row" id="services"></div>

    <div id="edit-overlay" class="modal-overlay" hidden>
      <div class="modal">
        <h2>Edit camera <span id="edit-title" class="mono"></span></h2>
        <p class="small">Slug is the primary key — to rename, delete and re-add.</p>
        <form id="edit-form" class="row">
          <div><label>Slug (read-only)</label><input id="edit-slug" disabled></div>
          <div><label>Display name</label><input id="edit-displayName" name="displayName"></div>
          <div class="full"><label>Source URL</label>
            <input id="edit-sourceUrl" name="sourceUrl">
          </div>
          <div><label>Transport</label>
            <select id="edit-rtspTransport" name="rtspTransport">
              <option>tcp</option><option>udp</option>
            </select>
          </div>
          <div><label>TLS verify</label>
            <select id="edit-tlsVerify" name="tlsVerify">
              <option value="false">no</option><option value="true">yes</option>
            </select>
          </div>
          <div><label>Record locally</label>
            <select id="edit-record" name="record">
              <option value="true">yes</option><option value="false">no</option>
            </select>
          </div>
          <div><label>Retain hours</label>
            <input id="edit-retainHours" type="number" name="retainHours" min="1">
          </div>
          <div><label>Enabled</label>
            <select id="edit-enabled" name="enabled">
              <option value="true">yes</option><option value="false">no</option>
            </select>
          </div>
          <div class="full actions">
            <button type="submit">Save changes</button>
            <button type="button" class="ghost" id="edit-cancel">Cancel</button>
          </div>
        </form>
        <div id="edit-msg"></div>
      </div>
    </div>

    <div id="lightbox" class="lightbox" hidden></div>

    <div class="grid2">
      <div>
        <div class="card">
          <h2>Cameras</h2>
          <table id="cams">
            <thead><tr><th>Slug</th><th>Source &amp; URLs</th><th>Settings</th><th>State</th><th></th></tr></thead>
            <tbody></tbody>
          </table>
          <details>
            <summary>Add a camera</summary>
            <form id="add-form" class="row" style="margin-top:10px;">
              <div><label>Slug</label><input name="slug" placeholder="front-door" required></div>
              <div><label>Display name</label><input name="displayName" placeholder="Front Door"></div>
              <div class="full"><label>Camera RTSP/RTSPS URL</label>
                <input name="sourceUrl" placeholder="rtsp://user:pass@192.168.1.50:554/stream1 or rtsps://...">
              </div>
              <div><label>Transport</label>
                <select name="rtspTransport"><option>tcp</option><option>udp</option></select>
              </div>
              <div><label>TLS verify</label>
                <select name="tlsVerify"><option value="false">no (UniFi/self-signed)</option><option value="true">yes</option></select>
              </div>
              <div><label>Record locally</label>
                <select name="record"><option value="true">yes</option><option value="false">no</option></select>
              </div>
              <div><label>Retain hours</label><input type="number" name="retainHours" value="168" min="1"></div>
              <div><label>Enabled</label>
                <select name="enabled"><option value="true">yes</option><option value="false">no</option></select>
              </div>
              <div class="full actions">
                <button type="submit">Add Camera</button>
              </div>
            </form>
          </details>
          <div id="cam-msg"></div>
        </div>

        <div class="card">
          <h2>Cloud-side mediamtx config</h2>
          <p class="small">Drop this paths block into your cloud mediamtx.yml so it can pull each camera on demand. Updates whenever you add/remove cameras here.</p>
          <textarea id="cloud-yaml" rows="10" readonly></textarea>
          <div class="actions">
            <button class="ghost" id="copy-yaml">Copy to clipboard</button>
            <a class="small" href="/api/cloud-config" target="_blank" style="margin-left:auto;align-self:center;">Open as raw .yaml</a>
          </div>
        </div>
      </div>

      <div>
        <div class="card">
          <h2>Site &amp; Cloud</h2>
          <form id="site-form" class="row">
            <div><label>Site slug</label><input name="slug" id="site-slug" required></div>
            <div><label>Display name</label><input name="displayName" id="site-display"></div>
            <div class="full actions"><button type="submit">Save site</button></div>
          </form>
          <div id="site-msg"></div>
          <hr style="border:none;border-top:1px solid var(--line);margin:14px 0">
          <form id="cloud-form" class="row">
            <div class="full"><label>Box's tailnet address (use the IP, not the MagicDNS name)</label>
              <input name="tailnetHost" id="cloud-tailnet" placeholder="100.86.38.62">
            </div>
            <div class="full"><label>Cloud playback hostname (informational)</label>
              <input name="playbackHost" id="cloud-playback" placeholder="147.182.179.39 or merlin-cloud">
            </div>
            <div class="full"><label>Cloud health URL (optional)</label>
              <input name="healthUrl" id="cloud-health" placeholder="https://cloud.example/api/edge-health">
            </div>
            <div class="full"><label>Cloud admin API URL (camera registration)</label>
              <input name="adminApiUrl" id="cloud-admin-api" placeholder="http://merlin-cloud/api/v1/admin/cloud-pull-cameras">
            </div>
            <div class="full actions">
              <button type="submit">Save cloud</button>
              <button type="button" class="ghost" id="cloud-sync-btn">Sync cloud now</button>
              <button type="button" class="ghost" id="cloud-test-btn">Test cloud</button>
            </div>
          </form>
          <div id="cloud-msg"></div>
          <p class="small" style="margin-top:10px"><strong>Use the box's tailnet IP address</strong> — find it with <code class="mono">tailscale ip -4</code> on the box (e.g. <code class="mono">100.86.38.62</code>). Avoid the MagicDNS name (<code class="mono">*.tail*.ts.net</code>): the cloud's mediamtx runs in a Docker container whose embedded resolver doesn't always reach Tailscale's DNS, and the resulting "name does not resolve" failures are silent until a viewer can't load a camera.</p>
          <p class="small">Cloud admin API URL: this box POSTs its camera list to the cloud's <code>/api/v1/admin/cloud-pull-cameras</code> on every save. The cloud is master — it generates mediamtx paths from the registration. Requires <code>CONTROL_API_KEY</code> in the box's <code>.env</code>. <strong>Sync cloud now</strong> re-posts immediately; <strong>Test cloud</strong> probes the URL + auth without changing state.</p>
        </div>

        <div class="card">
          <h2>Host</h2>
          <div id="host"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Logs</h2>
      <div class="toolbar">
        <select id="logs-service">
          <option value="relay">relay (camera ingest)</option>
          <option value="mediamtx">mediamtx</option>
          <option value="installer-ui">installer-ui</option>
        </select>
        <label>Lines: <input type="number" id="logs-tail" value="200" min="20" max="2000" step="20"></label>
        <label><input type="checkbox" id="logs-auto" checked> Auto-refresh (3s)</label>
        <button class="ghost" id="logs-refresh">Refresh now</button>
        <span class="small">Camera URL passwords are masked. Stream tokens are visible — keep this UI behind Tailscale.</span>
      </div>
      <div class="logbox" id="logs-output">…</div>
    </div>
  </div>

  <script>
    const $ = (q) => document.querySelector(q);
    const camsBody = $('#cams tbody');

    function pill(state) {
      if (state === 'running') return '<span class="pill ok">running</span>';
      if (state === 'disabled') return '<span class="pill off">disabled</span>';
      if (state === 'starting') return '<span class="pill off">starting</span>';
      if (state === 'flapping') return '<span class="pill warn">flapping</span>';
      return '<span class="pill bad">'+state+'</span>';
    }

    function deriveState(cam, s) {
      if (!cam.enabled) return 'disabled';
      if (!s) return 'starting';
      const nowSec = Date.now() / 1000;
      const sinceFail = (s.lastFailureAt != null) ? (nowSec - s.lastFailureAt) : Infinity;
      if (!s.running) return 'failing';
      if (s.uptimeSeconds < 5) return 'starting';
      if (sinceFail < 30 || (s.restartCount > 0 && s.uptimeSeconds < 30)) return 'flapping';
      return 'running';
    }

    function showMsg(target, msg, isErr) {
      const el = document.querySelector(target);
      el.innerHTML = '<div class="toast '+(isErr?'err':'')+'">'+msg+'</div>';
      setTimeout(() => { el.innerHTML = ''; }, 4500);
    }

    async function api(method, path, body) {
      const opts = { method, headers: { 'content-type': 'application/json' } };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
      return data;
    }

    function fmtBytes(b) {
      if (b == null) return '-';
      const u = ['B','KB','MB','GB','TB'];
      let i = 0;
      while (b >= 1024 && i < u.length-1) { b /= 1024; i++; }
      return b.toFixed(b < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
    }

    function renderCameras(cameras, urls, statusByCam, recPerCam) {
      camsBody.innerHTML = '';
      if (!cameras.length) {
        camsBody.innerHTML = '<tr><td colspan="5" class="small">No cameras yet. Add one below.</td></tr>';
        return;
      }
      for (const c of cameras) {
        const tr = document.createElement('tr');
        const u = urls[c.slug] || {};
        const s = (statusByCam || {})[c.slug];
        const rec = (recPerCam || {})[c.slug];
        const state = deriveState(c, s);
        const recInfo = rec
          ? `<div class="small">recordings: ${rec.fileCount} files / ${fmtBytes(rec.sizeBytes)}</div>`
          : '';
        const thumb = `<img class="thumb" data-act="enlarge" data-slug="${c.slug}" src="/api/snapshot/${c.slug}.jpg?t=${Date.now()}" onerror="this.removeAttribute('src')" alt="snapshot">`;
        tr.innerHTML = `
          <td><strong>${c.slug}</strong><br><span class="small">${c.displayName||''}</span></td>
          <td>
            ${thumb}
            <div class="small mono" style="margin-top:6px">${c.sourceUrl}</div>
            <div class="small mono">box → cloud: ${u.boxLocalUrl||'-'}</div>
            <div class="small mono">cloud path: ${u.cloudPath||'-'}</div>
            <div class="small mono">playback: ${u.playbackUrl||'(set playbackHost)'}</div>
          </td>
          <td>
            <div class="small">${c.rtspTransport}${c.tlsVerify?'':' / no-verify'}</div>
            <div class="small">record: ${c.record?'yes ('+c.retainHours+'h)':'no'}</div>
            ${recInfo}
          </td>
          <td>${pill(state)}</td>
          <td>
            <button data-act="edit" data-slug="${c.slug}">Edit</button>
            <button class="ghost" data-act="toggle-record" data-slug="${c.slug}">${c.record?'Stop rec':'Record'}</button>
            <button class="ghost" data-act="toggle" data-slug="${c.slug}">${c.enabled?'Disable':'Enable'}</button>
            <button class="danger" data-act="del" data-slug="${c.slug}">Delete</button>
          </td>`;
        camsBody.appendChild(tr);
      }
    }

    function renderHost(h) {
      const cpu = h.host && h.host.cpuPercent;
      const mem = h.host && h.host.memory;
      const disk = h.host && h.host.disk;
      const net = h.host && h.host.networkOutMbps;
      const rec = h.host && h.host.recordings;
      const camTotal = h.cameraCount || 0;
      const supUpd = h.supervisor && h.supervisor.updated
        ? new Date(h.supervisor.updated * 1000).toLocaleTimeString() : 'never';

      const cpuBar = cpu == null ? '' :
        `<div class="bar ${cpu>80?'warn':''}"><span style="width:${Math.min(100,cpu)}%"></span></div>`;
      const memBar = !mem ? '' :
        `<div class="bar ${mem.usedPercent>85?'warn':''}"><span style="width:${Math.min(100,mem.usedPercent)}%"></span></div>`;
      const diskBar = !disk ? '' :
        `<div class="bar ${disk.usedPercent>85?'warn':''}"><span style="width:${Math.min(100,disk.usedPercent)}%"></span></div>`;

      $('#host').innerHTML = `
        <div class="stat"><span>Site</span><span class="v">${h.site||'-'}</span></div>
        <div class="stat"><span>Tailnet host</span><span class="v">${h.tailnetHost||'(unset)'}</span></div>
        <div class="stat"><span>Cameras</span><span class="v">${camTotal}</span></div>
        <div class="stat"><span>CPU</span><span class="v">${cpu==null?'…':cpu+'%'}</span></div>${cpuBar}
        <div class="stat"><span>RAM</span><span class="v">${mem?fmtBytes(mem.totalBytes-mem.availableBytes)+' / '+fmtBytes(mem.totalBytes):'…'}</span></div>${memBar}
        <div class="stat"><span>Disk</span><span class="v">${disk?fmtBytes(disk.totalBytes-disk.freeBytes)+' / '+fmtBytes(disk.totalBytes):'…'}</span></div>${diskBar}
        <div class="stat"><span>Upload now</span><span class="v">${net==null?'…':net+' Mbps'}</span></div>
        <div class="stat"><span>Recordings on disk</span><span class="v">${fmtBytes(rec?rec.totalBytes:0)}</span></div>
        <div class="stat"><span>Supervisor seen</span><span class="v">${supUpd}</span></div>
      `;
    }

    function setIfNotFocused(id, value) {
      const el = document.getElementById(id);
      if (el && document.activeElement !== el) el.value = value;
    }

    async function refresh() {
      const cfg = await api('GET', '/api/config');
      const health = await api('GET', '/api/health');
      const statusByCam = {};
      const supCams = (health.supervisor && health.supervisor.cameras) || [];
      for (const s of supCams) statusByCam[s.slug] = s;
      const recPerCam = (health.host && health.host.recordings && health.host.recordings.perCamera) || {};

      setIfNotFocused('site-slug', cfg.config.site.slug);
      setIfNotFocused('site-display', cfg.config.site.displayName);
      setIfNotFocused('cloud-tailnet', cfg.config.cloud.tailnetHost || '');
      setIfNotFocused('cloud-playback', cfg.config.cloud.playbackHost || '');
      setIfNotFocused('cloud-health', cfg.config.cloud.healthUrl || '');
      setIfNotFocused('cloud-admin-api', cfg.config.cloud.adminApiUrl || '');
      setIfNotFocused('cloud-yaml', cfg.cloudPathsYaml || '');

      renderCameras(cfg.config.cameras, cfg.urls, statusByCam, recPerCam);
      renderHost(health);
    }

    $('#site-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.target).entries());
      try { await api('PUT', '/api/site', data); showMsg('#site-msg', 'Saved.'); refresh(); }
      catch (err) { showMsg('#site-msg', err.message, true); }
    });

    $('#cloud-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.target).entries());
      try { await api('PUT', '/api/cloud', data); showMsg('#cloud-msg', 'Saved.'); refresh(); }
      catch (err) { showMsg('#cloud-msg', err.message, true); }
    });

    $('#cloud-test-btn').addEventListener('click', async () => {
      try {
        const r = await api('POST', '/api/cloud-test');
        const lines = r.checks.map(c => (c.ok ? '✓' : '✗') + ' ' + c.name + ' — ' + c.message);
        const html = lines.map(l => '<div class="small mono">'+l+'</div>').join('');
        document.querySelector('#cloud-msg').innerHTML =
          '<div class="toast '+(r.ok?'':'err')+'">'+(r.ok?'All cloud checks passed':'Some cloud checks failed')+'<div style="margin-top:6px">'+html+'</div></div>';
      } catch (err) { showMsg('#cloud-msg', err.message, true); }
    });

    $('#cloud-sync-btn').addEventListener('click', async () => {
      try {
        const r = await api('POST', '/api/cloud-sync');
        if (r.skipped) {
          showMsg('#cloud-msg', 'Skipped: ' + r.skipped, true);
          return;
        }
        if (!r.ok) {
          const errs = (r.errors || []).join('; ');
          showMsg('#cloud-msg', 'Cloud registration failed: ' + errs, true);
          return;
        }
        const paths = (r.registered || []).join(', ');
        const msg = 'Registered ' + r.cameraCount + ' cameras with cloud'
                  + (paths ? ' · ' + paths : '');
        showMsg('#cloud-msg', msg);
      } catch (err) { showMsg('#cloud-msg', err.message, true); }
    });

    $('#add-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.target).entries());
      data.tlsVerify = data.tlsVerify === 'true';
      data.enabled = data.enabled === 'true';
      data.record = data.record === 'true';
      data.retainHours = Number(data.retainHours);
      try { await api('POST', '/api/cameras', data); e.target.reset(); showMsg('#cam-msg', 'Camera added.'); refresh(); }
      catch (err) { showMsg('#cam-msg', err.message, true); }
    });

    function openEdit(cam) {
      $('#edit-title').textContent = cam.slug;
      $('#edit-slug').value = cam.slug;
      $('#edit-displayName').value = cam.displayName || '';
      $('#edit-sourceUrl').value = cam.sourceUrl || '';
      $('#edit-rtspTransport').value = cam.rtspTransport || 'tcp';
      $('#edit-tlsVerify').value = cam.tlsVerify ? 'true' : 'false';
      $('#edit-record').value = cam.record ? 'true' : 'false';
      $('#edit-retainHours').value = cam.retainHours || 168;
      $('#edit-enabled').value = cam.enabled ? 'true' : 'false';
      $('#edit-msg').innerHTML = '';
      $('#edit-overlay').hidden = false;
    }

    function closeEdit() { $('#edit-overlay').hidden = true; }

    function openLightbox(slug) {
      const lb = $('#lightbox');
      lb.innerHTML = `<img src="/api/snapshot/${slug}.jpg?t=${Date.now()}" alt="snapshot">`;
      lb.hidden = false;
    }
    $('#lightbox').addEventListener('click', () => { $('#lightbox').hidden = true; });

    camsBody.addEventListener('click', async (e) => {
      const target = e.target.closest('[data-act]');
      if (!target) return;
      const slug = target.dataset.slug;
      const act = target.dataset.act;
      try {
        if (act === 'del') {
          if (!confirm('Delete camera "'+slug+'"?')) return;
          await api('DELETE', '/api/cameras/'+slug);
        } else if (act === 'toggle') {
          const cfg = await api('GET', '/api/config');
          const cam = cfg.config.cameras.find(c => c.slug === slug);
          await api('PUT', '/api/cameras/'+slug, { enabled: !cam.enabled });
        } else if (act === 'toggle-record') {
          const cfg = await api('GET', '/api/config');
          const cam = cfg.config.cameras.find(c => c.slug === slug);
          await api('PUT', '/api/cameras/'+slug, { record: !cam.record });
        } else if (act === 'edit') {
          const cfg = await api('GET', '/api/config');
          const cam = cfg.config.cameras.find(c => c.slug === slug);
          if (cam) openEdit(cam);
          return;
        } else if (act === 'enlarge') {
          openLightbox(slug);
          return;
        }
        showMsg('#cam-msg', 'Updated.');
        refresh();
      } catch (err) {
        showMsg('#cam-msg', err.message, true);
      }
    });

    $('#edit-cancel').addEventListener('click', closeEdit);
    $('#edit-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const slug = $('#edit-slug').value;
      const payload = {
        displayName: $('#edit-displayName').value,
        sourceUrl: $('#edit-sourceUrl').value,
        rtspTransport: $('#edit-rtspTransport').value,
        tlsVerify: $('#edit-tlsVerify').value === 'true',
        record: $('#edit-record').value === 'true',
        retainHours: Number($('#edit-retainHours').value),
        enabled: $('#edit-enabled').value === 'true',
      };
      try {
        await api('PUT', '/api/cameras/'+slug, payload);
        closeEdit();
        showMsg('#cam-msg', 'Saved.');
        refresh();
      } catch (err) {
        $('#edit-msg').innerHTML = '<div class="toast err">'+err.message+'</div>';
      }
    });

    $('#copy-yaml').addEventListener('click', () => {
      const ta = $('#cloud-yaml');
      ta.select();
      document.execCommand('copy');
    });

    function renderServices(services) {
      const el = $('#services');
      const cls = (s) => s.state === 'running' && !s.restarting ? 'ok'
                      : s.state === 'restarting' || s.restarting ? 'warn'
                      : s.state === 'exited' || s.state === 'missing' ? 'bad'
                      : 'off';
      el.innerHTML = services.map(s => {
        const restarts = (s.restartCount && s.restartCount > 0) ? ' ↻' + s.restartCount : '';
        return '<span class="pill '+cls(s)+'">'+s.name+': '+(s.state||'?')+restarts+'</span>';
      }).join('');
    }

    async function refreshServices() {
      try {
        const r = await api('GET', '/api/services');
        renderServices(r.services);
      } catch (err) { /* silent */ }
    }

    async function refreshLogs() {
      try {
        const svc = $('#logs-service').value;
        const tail = $('#logs-tail').value || 200;
        const r = await api('GET', '/api/logs/' + svc + '?tail=' + tail);
        const box = $('#logs-output');
        const wasAtBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
        box.textContent = r.lines.join('\\n') || '(no log lines)';
        if (wasAtBottom) box.scrollTop = box.scrollHeight;
      } catch (err) {
        $('#logs-output').textContent = 'log fetch failed: ' + err.message;
      }
    }

    $('#logs-refresh').addEventListener('click', refreshLogs);
    $('#logs-service').addEventListener('change', refreshLogs);
    $('#logs-tail').addEventListener('change', refreshLogs);

    refresh();
    refreshServices();
    refreshLogs();
    setInterval(refresh, 5000);
    setInterval(refreshServices, 5000);
    setInterval(() => { if ($('#logs-auto').checked) refreshLogs(); }, 3000);
  </script>
</body>
</html>"""


def _start_health_thread() -> None:
    t = threading.Thread(target=_cloud_health_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    cfg = load_config()
    save_config(cfg, write_mediamtx=True)
    _start_health_thread()
    app.run(host="0.0.0.0", port=8080)
