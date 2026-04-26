# Edge Box → Cloud Health Reporting API

Each MerlinConnect edge box pushes its current state to a cloud endpoint
every 60 seconds. This document describes the contract the cloud must
implement to receive those reports.

## Endpoint

| Property | Value |
|---|---|
| Method | `POST` |
| URL | configured per-box as `cloud.healthUrl` in the box's `data/config.json`. Recommended: `http://<cloud-tailnet-host>/api/edge-health` over Tailscale, no public exposure. |
| Content-Type | `application/json` |
| Encoding | UTF-8 |
| Body size | Typically 1.5–4 KB (grows linearly with `cameraCount`) |

## Authentication (recommendations)

The box does not currently send an auth token. Recommended cloud-side
gating, in order of strength:

1. **Network gating via Tailscale** (simplest, recommended). The endpoint
   listens only on the cloud host's tailnet interface. Combined with
   tailnet ACLs that permit `tag:edge-box → tag:cloud:443/8080`, this is
   sufficient for an internal fleet.
2. **Per-box bearer token** (best for multi-tenant). Add a `boxToken` field
   to box config and have the box send `Authorization: Bearer <token>`.
   Cloud verifies token → site mapping. Schema is forward-compatible —
   server can introduce this without box changes; we'll add the box-side
   header when needed.
3. **Source-IP allowlist** as a backstop: reject any source IP outside the
   tailnet CGNAT range `100.64.0.0/10`.

## Request body schema

```json
{
  "site":          "string — kebab-case site slug; primary key for the box",
  "tailnetHost":   "string — box's own Tailscale FQDN, may be empty if not yet set in UI",
  "playbackHost":  "string — cloud's playback hostname as configured on the box (informational)",
  "cameraCount":   "integer — total cameras configured (enabled + disabled)",
  "supervisor": {
    "site":     "string — should equal top-level site",
    "updated":  "integer — unix epoch seconds when supervisor last wrote status",
    "cameras": [
      {
        "slug":            "string — camera slug",
        "running":         "boolean — true if ffmpeg child is alive right now",
        "uptimeSeconds":   "integer — seconds since current ffmpeg started, 0 if not running",
        "lastExit":        "integer or null — exit code of last dead ffmpeg, null if alive",
        "restartCount":    "integer — total restarts since supervisor started (cumulative)",
        "lastFailureAt":   "number or null — unix epoch (float) of most recent ffmpeg exit"
      }
    ]
  },
  "host": {
    "cpuPercent":     "number or null — host CPU % since previous report, 0–100",
    "memory": {
      "totalBytes":     "integer",
      "availableBytes": "integer",
      "usedPercent":    "number"
    } | null,
    "disk": {
      "totalBytes":     "integer — /data filesystem on the box",
      "freeBytes":      "integer",
      "usedPercent":    "number"
    } | null,
    "networkOutMbps": "number or null — current host upload rate, sum of all non-loopback non-docker NICs",
    "recordings": {
      "totalBytes": "integer — total recording size on disk across all cameras",
      "perCamera": {
        "<camera-slug>": {
          "sizeBytes":   "integer",
          "fileCount":   "integer",
          "oldestEpoch": "integer or null"
        }
      }
    }
  },
  "reportedAt": "integer — unix epoch seconds at which the box built this payload (NB: trust server time more, clocks drift)"
}
```

`null` values appear when the box can't read the underlying source (e.g.
first sample of a delta stat like CPU or networkOutMbps; status file not
yet written by supervisor). Server should treat `null` as "not yet known"
and not fail validation.

## Example payload

```json
{
  "site": "school-main",
  "tailnetHost": "school-main.tail1234.ts.net",
  "playbackHost": "merlin-cloud",
  "cameraCount": 3,
  "supervisor": {
    "site": "school-main",
    "updated": 1777164514,
    "cameras": [
      {
        "slug": "front-door",
        "running": true,
        "uptimeSeconds": 1843,
        "lastExit": null,
        "restartCount": 0,
        "lastFailureAt": null
      },
      {
        "slug": "parking-east",
        "running": true,
        "uptimeSeconds": 1837,
        "lastExit": null,
        "restartCount": 1,
        "lastFailureAt": 1777162680.4
      },
      {
        "slug": "loading-dock",
        "running": false,
        "uptimeSeconds": 0,
        "lastExit": 1,
        "restartCount": 14,
        "lastFailureAt": 1777164510.1
      }
    ]
  },
  "host": {
    "cpuPercent": 4.3,
    "memory": {
      "totalBytes": 8323796992,
      "availableBytes": 5418729472,
      "usedPercent": 34.9
    },
    "disk": {
      "totalBytes": 250790436864,
      "freeBytes": 198432624640,
      "usedPercent": 20.9
    },
    "networkOutMbps": 0.18,
    "recordings": {
      "totalBytes": 14826739200,
      "perCamera": {
        "front-door":   { "sizeBytes": 5234567168, "fileCount": 168, "oldestEpoch": 1776560000 },
        "parking-east": { "sizeBytes": 9018273280, "fileCount": 168, "oldestEpoch": 1776560000 },
        "loading-dock": { "sizeBytes": 573898752,  "fileCount": 12,  "oldestEpoch": 1777121200 }
      }
    }
  },
  "reportedAt": 1777164520
}
```

## Response expectations

- `200 OK` (or `204 No Content`) on success. Body ignored by box.
- Any other status logged on box and the report retried on next 60s tick;
  no exponential backoff at the moment, just regular cadence.
- A response time over 10 seconds causes the box to give up and log a
  failure. Cloud should respond within ~1 second; do durable processing
  asynchronously.

## Frequency and retry

- Report cadence: **60 seconds**, fixed.
- Box does not buffer reports across failures — one missed POST is one
  missed 60-second window. The next successful report has the current
  state; nothing is queued.
- No deduplication needed server-side; each report is a snapshot.

## What the server should do with the data

Minimum viable:

- **Track per-site last-seen timestamp** (server's wall clock, not the
  box's `reportedAt` — clocks drift). Mark a site stale if no report in
  ≥ 3 minutes (i.e. ≥ 3 missed reports).
- **Index by `site` slug**. Use it as the box's primary key in your store.
- **Surface flapping cameras**: any camera with `restartCount > N` over a
  rolling window is misconfigured / source unreachable / cert problem.
- **Disk-fill alerts**: alert when `host.disk.usedPercent > 85` so retention
  or storage can be tuned before recordings stop.

Nice-to-haves:

- **Time-series store** of host stats (CPU, RAM, disk, networkOutMbps,
  per-camera restartCount) for fleet-wide trending. The payload is
  designed to be append-only — keep raw and aggregate later.
- **Auto-generate cloud mediamtx paths**: read each box's enabled cameras
  and build `<site>-<camera-slug>` path entries with
  `source: rtsp://<tailnetHost>:8554/<camera-slug>` automatically. This
  removes the manual paste step from the installer flow. The box's
  `tailnetHost` field is the source of truth.

## Idempotence and ordering

Reports are independent snapshots. There is no monotonic version or
sequence number. If two reports arrive out of order, take the one with
the larger `reportedAt` (or just use receive time). Do not assume
sequential delivery.

## Schema versioning

The current payload has no explicit `schemaVersion`. Server should:

- Treat unknown top-level fields as forward-compat additions and ignore.
- Treat absent fields as `null`.

If the schema breaks compatibly, we'll add a `schemaVersion: 2` field
and document the diff here. Server can branch on it then.

## CORS

Not applicable — box-to-cloud is server-to-server, not browser-originated.
Cloud need not send CORS headers on this endpoint.

## Testing the endpoint without a box

```bash
curl -i -X POST https://merlin-cloud/api/edge-health \
  -H 'content-type: application/json' \
  -d @- <<'JSON'
{
  "site": "test-box",
  "tailnetHost": "test-box.tail1234.ts.net",
  "playbackHost": "merlin-cloud",
  "cameraCount": 1,
  "supervisor": { "site": "test-box", "updated": 0,
    "cameras": [{
      "slug": "front-door", "running": true, "uptimeSeconds": 60,
      "lastExit": null, "restartCount": 0, "lastFailureAt": null
    }]
  },
  "host": {
    "cpuPercent": 5.0,
    "memory": { "totalBytes": 1, "availableBytes": 1, "usedPercent": 0 },
    "disk": { "totalBytes": 1, "freeBytes": 1, "usedPercent": 0 },
    "networkOutMbps": 0,
    "recordings": { "totalBytes": 0, "perCamera": {} }
  },
  "reportedAt": 0
}
JSON
```

Expect `200` (or `204`) within ~1 s.

## Reference: where this comes from in the box code

- POST loop: [web-ui/app.py](../web-ui/app.py) `_cloud_health_loop`
- Payload assembly: [web-ui/app.py](../web-ui/app.py) `build_health`
- Supervisor state file: [relay/supervisor.py](../relay/supervisor.py) `write_status`
- Same payload is also available from the box at `GET /api/health` if you
  want to pull rather than wait for the next push.
