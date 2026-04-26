# MerlinConnect Edge Box

A Linux appliance that pulls one or more local IP cameras, records each stream
locally, and exposes them on a Tailscale tailnet so a Merlin cloud server can
**pull on demand** when a user opens a camera in the Merlin map. Designed to
be installed at a customer site by a junior operator with a browser and to
operate without any inbound public ports.

## Architecture (cloud pull on demand)

```
Camera (RTSP/RTSPS, LAN)
    │
    │  ffmpeg ingest (per camera, always on)
    ▼
┌─ Box (Tailscale-joined as <site-slug>.<tailnet>.ts.net) ──┐
│                                                            │
│  MediaMTX                                                  │
│   ├─ path "<camera-slug>"  (publisher, fed by ingest)     │
│   ├─ records to /data/recordings/<camera-slug>/...        │
│   └─ serves on demand to any tailnet peer                 │
│                                                            │
│  RTSP listener: 0.0.0.0:8554 (LAN + tailnet)              │
└────────────────────────────────────────────────────────────┘
            │
            │  Tailscale (encrypted, no public port at site)
            │  Pulled only when cloud has a viewer
            ▼
┌─ Cloud MediaMTX ──────────────────────────────────────────┐
│                                                            │
│  paths.<site-slug>-<camera-slug>:                         │
│    source: rtsp://<site-slug>.<tailnet>.ts.net:8554/<cam> │
│    sourceOnDemand: yes                                    │
│                                                            │
│  Serves HLS / WebRTC to Merlin viewers                    │
└────────────────────────────────────────────────────────────┘
            │
            ▼
        Merlin map (browser)
```

**Streaming model is "cloud pull when requested". Continuous push from box to
cloud is not the design.** Direct camera-to-cloud push is reserved as an
optional separate mode for cameras that can reach the cloud directly on a
public port — out of scope here.

## Stable naming

| Layer | Format | Example |
|---|---|---|
| Box MediaMTX path | `<camera-slug>` | `front-door` |
| Box-local RTSP URL (cloud's `source:`) | `rtsp://<site-slug>.<tailnet>.ts.net:8554/<camera-slug>` | `rtsp://school-main.tail1234.ts.net:8554/front-door` |
| Cloud MediaMTX path | `<site-slug>-<camera-slug>` | `school-main-front-door` |
| Cloud HLS URL (Merlin reference) | `http://<cloud-host>/live/<site-slug>-<camera-slug>/index.m3u8` | `http://merlin.example/live/school-main-front-door/index.m3u8` |

The box's installer UI generates a copy-paste mediamtx config snippet for the
cloud at `GET /api/cloud-config` (also visible in the UI's Cloud-side card).

## End-to-end flow when a user clicks a camera

1. User opens Merlin map. Nothing is streaming. Box's WAN: 0 bps.
2. User clicks camera `school-main / front-door`.
3. Browser loads `http://<cloud>/live/school-main-front-door/index.m3u8`.
4. Cloud MediaMTX sees a viewer on path `school-main-front-door`. With
   `sourceOnDemand: yes`, it opens `rtsp://school-main.tail1234.ts.net:8554/front-door`.
5. The connection routes over Tailscale to the box's MediaMTX, which is
   already serving the path (the box's ingest ffmpeg has been publishing to
   it continuously for local recording).
6. Cloud wraps the stream as HLS/WebRTC. Browser plays. Cold-start ~3 s.
7. User closes the tab. After `sourceOnDemandCloseAfter: 30s` of no viewers,
   cloud closes the source. Box keeps ingesting (still recording locally).
   WAN drops to 0.

If the box is offline when a viewer clicks, cloud's pull fails and the player
shows an error. **Local recording continues regardless of cloud availability.**

## Hardware

For one to a few dozen cameras at 1080p:

- Intel NUC, Beelink, ASUS PN, or similar mini-PC
- 8 GB RAM minimum (16 GB if you'll go past ~16 cameras)
- 256 GB SSD minimum (recordings live here — see "Disk math" below)
- Gigabit Ethernet
- Ubuntu 24.04 / 26.04 LTS or Debian 12+

The real ceilings are **disk write throughput** (for recording) and the LAN
to cameras. CPU and WAN upload are not the bottleneck under the pull model.

**Disk math:** at ~4 Mbps per 1080p camera, 24 hours ≈ 43 GB per camera; one
week ≈ 300 GB per camera. Plan SSD size against camera count × retainHours.

## Network requirements

The site-side box only needs **outbound** internet for Tailscale to reach the
coordination service. **No inbound firewall openings at the site.** The cloud
reaches the box via Tailscale (CGNAT, encrypted, NAT-traversed).

- Outbound `443/tcp` to Tailscale
- Outbound to local cameras on whatever ports they use (usually 554 RTSP / 7441 RTSPS)

## First install on a fresh box

```bash
git clone <repo-url> /opt/merlin-edge
cd /opt/merlin-edge
sudo SITE_SLUG=school-main \
     TAILSCALE_AUTH_KEY=tskey-auth-... \
     bash scripts/bootstrap-box.sh

docker compose up -d --build
```

`SITE_SLUG` becomes both the box's Tailscale hostname and the `site.slug` in
config. Without these env vars, the bootstrap prompts.

After the first compose-up, open the installer UI:

```
http://<box-ip>:8080
```

Enter the cloud playback hostname and (if known) the box's Tailscale hostname,
then add cameras one at a time.

## Tailscale ACL (cloud side)

Add to your tailnet ACL so the cloud server can reach edge boxes on `:8554`:

```json
{
  "tagOwners": {
    "tag:cloud":    ["your-email@example.com"],
    "tag:edge-box": ["your-email@example.com"]
  },
  "acls": [
    { "action": "accept",
      "src":    ["tag:cloud"],
      "dst":    ["tag:edge-box:8554"] }
  ]
}
```

Box bootstrap should `tailscale up --advertise-tags=tag:edge-box --hostname=<site-slug>`.
(Add `--advertise-tags` to bootstrap if your tailnet enforces tags.)

## Cloud-side mediamtx (one block per box)

The installer UI shows a ready-to-paste paths block per box. For
`school-main` with two cameras it looks like:

```yaml
paths:
  school-main-front-door:
    source: rtsp://school-main.tail1234.ts.net:8554/front-door
    sourceOnDemand: yes
    sourceOnDemandStartTimeout: 15s
    sourceOnDemandCloseAfter: 30s
  school-main-parking:
    source: rtsp://school-main.tail1234.ts.net:8554/parking
    sourceOnDemand: yes
    sourceOnDemandStartTimeout: 15s
    sourceOnDemandCloseAfter: 30s
```

For a fleet, automate config assembly: run a cloud agent that consumes each
box's `POST /api/edge-health` (sent every 60s if `cloud.healthUrl` is set)
and rewrites the cloud mediamtx config from the union of all reported boxes.

## Adding a camera

In the box UI:

1. **Site & Cloud**: confirm the site slug, paste the box's Tailscale hostname
   (find with `tailscale status --self --json | jq -r '.Self.DNSName'`),
   and the cloud playback hostname.
2. **Cameras → Add a camera**:
   - **Slug**: short id (e.g. `front-door`).
   - **Camera RTSP/RTSPS URL**: full URL with credentials.
   - **TLS verify**: `no` for UniFi Protect (self-signed cert valid only for
     `127.0.0.1`); `yes` for cameras with a real cert.
   - **Record locally**: usually yes.
   - **Retain hours**: per-camera retention; mediamtx prunes via
     `recordDeleteAfter`.

Within ~2 seconds the supervisor spawns ffmpeg, ffmpeg publishes into
mediamtx, and the row flips to `running`. The cloud-side YAML snippet updates
automatically.

## Camera compatibility

- **Plain RTSP** (`rtsp://...`): works as-is.
- **RTSPS with self-signed cert** (UniFi Protect 2025+): set TLS verify to
  `no`. ffmpeg's `-tls_verify 0` skips hostname/CA checks.
- **RTSPS with valid cert**: set TLS verify to `yes`.
- **HTTP / RTMP cameras**: not currently exposed in the UI.

## Local recording

Every camera has a **Record locally** toggle and **Retain hours** field. When
`record: yes`, mediamtx writes 1-hour fMP4 segments (with 1-second parts) to
`data/recordings/<camera-slug>/...`. mediamtx prunes files older than the
camera's `retainHours` automatically.

Recording is **independent of cloud playback**. WAN can be down for days; the
box still records.

For larger retention, mount a bigger volume to `./data/recordings`.

## Host stats panel

The Host card in the UI shows live CPU, RAM, disk, current upload Mbps, and
total recording size. Reads `/host/proc` and `/host/sys` (mounted read-only
into the installer-ui container).

## Auth

Set `ADMIN_PASSWORD` in `.env` (and optionally `ADMIN_USERNAME`, default
`admin`). When set, all UI and API routes require HTTP Basic auth. When
blank, no auth — fine if access is gated by Tailscale + LAN.

## Updating an installed box

```bash
sudo bash scripts/update.sh
```

`git pull` + `docker compose up -d --build`. Idempotent. Desktop launcher
**Merlin Edge — Update** runs the same.

## Reaching the UI over the tailnet (HTTPS)

```bash
sudo bash scripts/tailscale-serve.sh
```

Publishes `http://127.0.0.1:8080` at `https://<site-slug>.<tailnet>.ts.net/`
with a real TLS cert. State persists across reboots.

## File layout

```
.
├── compose.yaml             # 3 services + log rotation
├── data/
│   ├── config.json          # site, cloud, cameras (UI-managed)
│   ├── status.json          # supervisor's per-camera state
│   ├── mediamtx.yml         # generated by installer-ui on every camera change
│   └── recordings/          # mediamtx-written fMP4 segments per camera
├── relay/
│   ├── Dockerfile           # python:3.12-slim + BtbN static ffmpeg 8.x
│   └── supervisor.py        # spawns one ffmpeg per camera → publishes to mediamtx
├── scripts/
│   ├── install.sh           # one-shot bootstrap
│   ├── bootstrap-box.sh     # Docker + Tailscale provisioning
│   ├── update.sh
│   ├── tailscale-serve.sh
│   ├── status.sh
│   ├── test-local-camera.sh
│   └── test-cloud-publish.sh
└── web-ui/
    ├── Dockerfile
    ├── app.py               # Flask + UI + mediamtx config writer
    └── requirements.txt
```

## Health endpoint

```bash
curl -s http://127.0.0.1:8080/api/health
```

Returns site slug, tailnet host, camera count, host stats, supervisor's
per-camera state, recording sizes per camera. If `cloud.healthUrl` is set,
the box POSTs the same JSON to that URL every 60 seconds — the cloud agent
hook for fleet-wide visibility.

## Why pull, not push (the short version)

- Box-to-cloud WAN is 0 bps when nobody is watching, instead of
  `cameras × bitrate` continuously. ~90% bandwidth savings at typical use.
- Tailscale solves the "no inbound port at site" requirement that historically
  pushed people to push.
- Local recording on the box gives WAN-outage resilience independent of cloud
  playback.
- Cold-start latency (~3 s HLS / <1 s WebRTC) is acceptable for click-to-view.
# Merlin-Client
