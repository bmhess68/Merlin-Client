#!/usr/bin/env python3
"""Bulk-import NYSDOT 511NY public traffic cameras into this edge box as
hls-pull cameras. Interactive by default; every prompt has a CLI flag for
unattended use.

The script POSTs each camera through the installer-ui's /api/cameras
endpoint so it goes through the same validation + mediamtx.yml regen path
the UI uses. Cameras that already exist (by slug) are skipped.

Usage:
    scripts/sync-511ny-cameras.py
    scripts/sync-511ny-cameras.py --api-key XXX --bbox 40.87,-73.92,41.34,-73.45 \\
        --prefix ny511- --retain-hours 72 --yes

Run from the box host. The installer-ui must be reachable on
http://localhost:8080 and you'll need ADMIN_USERNAME / ADMIN_PASSWORD
(same creds the UI uses).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.parse
import urllib.request

API_URL = "https://511ny.org/api/GetCameras"
DEFAULT_UI = "http://localhost:8080"
DEFAULT_PREFIX = "ny511-"
DEFAULT_RETAIN_HOURS = 72


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            value = getpass.getpass(f"{label}{suffix}: ").strip()
        else:
            value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be 'south,west,north,east'")
    south, west, north, east = (float(p) for p in parts)
    if south >= north or west >= east:
        raise ValueError("bbox must be south<north and west<east")
    return south, west, north, east


def slugify(name: str, prefix: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = f"{prefix}{s}"
    return s[:60].rstrip("-")


def fetch_cameras(api_key: str) -> list[dict]:
    qs = urllib.parse.urlencode({"key": api_key, "format": "json"})
    req = urllib.request.Request(f"{API_URL}?{qs}",
                                 headers={"User-Agent": "merlin-edge-511ny-sync/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def in_bbox(cam: dict, bbox: tuple[float, float, float, float]) -> bool:
    south, west, north, east = bbox
    try:
        lat = float(cam.get("Latitude"))
        lng = float(cam.get("Longitude"))
    except (TypeError, ValueError):
        return False
    return south <= lat <= north and west <= lng <= east


def usable(cam: dict) -> bool:
    if cam.get("Disabled") or cam.get("Blocked"):
        return False
    url = (cam.get("VideoUrl") or "").strip()
    return url.startswith("http://") or url.startswith("https://")


def post_camera(ui: str, auth: tuple[str, str], payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{ui.rstrip('/')}/api/cameras",
        data=body,
        method="POST",
        headers={"content-type": "application/json"},
    )
    # Basic auth — installer-ui uses HTTP Basic via @auth_required.
    creds = f"{auth[0]}:{auth[1]}"
    import base64
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(creds.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-key", help="511NY API key (or env NY511_API_KEY)")
    parser.add_argument("--bbox", help="south,west,north,east (decimal degrees)")
    parser.add_argument("--prefix", help=f"slug prefix (default: {DEFAULT_PREFIX})")
    parser.add_argument("--retain-hours", type=int,
                        help=f"recording retention in hours (default: {DEFAULT_RETAIN_HOURS})")
    parser.add_argument("--ui", help=f"installer-ui base URL (default: {DEFAULT_UI})")
    parser.add_argument("--admin-user", help="installer-ui admin user (or env ADMIN_USERNAME)")
    parser.add_argument("--admin-pass", help="installer-ui admin password (or env ADMIN_PASSWORD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + filter + show plan; do not POST")
    parser.add_argument("--yes", action="store_true",
                        help="skip the final confirmation prompt")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("NY511_API_KEY") \
              or prompt("511NY API key", secret=True)
    bbox_raw = args.bbox or prompt("Bounding box (south,west,north,east)")
    try:
        bbox = parse_bbox(bbox_raw)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    prefix = args.prefix or prompt("Slug prefix", default=DEFAULT_PREFIX)
    retain_hours = args.retain_hours or int(prompt("Recording retention (hours)",
                                                   default=str(DEFAULT_RETAIN_HOURS)))
    ui = args.ui or prompt("Installer-ui URL", default=DEFAULT_UI)

    print(f"\nFetching 511NY camera list...")
    try:
        all_cams = fetch_cameras(api_key)
    except Exception as exc:
        print(f"error fetching: {exc}", file=sys.stderr)
        return 1
    print(f"  got {len(all_cams)} cameras")

    kept = [c for c in all_cams if usable(c) and in_bbox(c, bbox)]
    print(f"  {len(kept)} match bbox + usable")

    plan = []
    for c in kept:
        slug = slugify(c.get("Name", ""), prefix)
        if not slug or slug == prefix.rstrip("-"):
            continue
        plan.append({
            "slug": slug,
            "displayName": c.get("Name", ""),
            "sourceMode": "hls-pull",
            "sourceUrl": c["VideoUrl"].strip(),
            "record": True,
            "retainHours": retain_hours,
            "enabled": True,
            # informational; web-ui's validate_camera ignores unknown fields
            "lat": c.get("Latitude"),
            "lng": c.get("Longitude"),
            "ny511Id": c.get("ID"),
        })

    print(f"\nPlanned cameras ({len(plan)}):")
    for p in plan:
        print(f"  {p['slug']:50s}  {p['displayName']}")

    if args.dry_run:
        print("\n--dry-run: not posting.")
        return 0
    if not plan:
        print("nothing to do.")
        return 0

    if not args.yes:
        confirm = input(f"\nPOST {len(plan)} cameras to {ui}? [y/N] ").strip().lower()
        if confirm != "y":
            print("aborted.")
            return 0

    admin_user = args.admin_user or os.environ.get("ADMIN_USERNAME") \
                 or prompt("Installer-ui admin user", default="admin")
    admin_pass = args.admin_pass or os.environ.get("ADMIN_PASSWORD") \
                 or prompt("Installer-ui admin password", secret=True)
    auth = (admin_user, admin_pass)

    added, skipped, failed = 0, 0, 0
    for p in plan:
        # validate_camera doesn't accept extra keys; strip the metadata.
        payload = {k: v for k, v in p.items()
                   if k not in {"lat", "lng", "ny511Id"}}
        status, body = post_camera(ui, auth, payload)
        if status == 201:
            added += 1
            print(f"  + {p['slug']}")
        elif status == 400 and "already exists" in body:
            skipped += 1
            print(f"  = {p['slug']} (exists)")
        else:
            failed += 1
            print(f"  ! {p['slug']}  HTTP {status}  {body[:120]}")

    print(f"\nDone. added={added} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
