# Cloud MediaMTX setup for auto-sync from edge boxes

When a box has **Cloud mediamtx API URL** set in its installer UI, every
camera change on the box auto-pushes the corresponding
`<site>-<camera>` path to the cloud's mediamtx via API. No more
copy-pasting YAML.

For this to work, the cloud's mediamtx needs two things:

1. API listener reachable from tailnet
2. Auth that permits API calls from tailnet IPs

## Required config in the cloud's `mediamtx.yml`

Add (or update) the `authInternalUsers` block. The block below is the same
shape as the box's, but the API permission is granted to the Tailscale
CGNAT range (`100.64.0.0/10`) so any edge box on the tailnet can call the
cloud API.

```yaml
authMethod: internal

authInternalUsers:
  - user: any
    ips: []
    permissions:
      - action: publish
      - action: read
      - action: playback
  - user: any
    ips:
      - 127.0.0.1
      - '::1'
      - 100.64.0.0/10        # Tailscale CGNAT — edge boxes' tailnet IPs
    permissions:
      - action: api
      - action: metrics
      - action: pprof
```

If your cloud mediamtx already has a custom `authInternalUsers` block,
merge the second entry (the `api/metrics/pprof` one with the CGNAT range)
into it.

## Cloud compose port mapping

The mediamtx API listens on port 9997 by default. Don't expose it
publicly — only on the tailnet interface. In the cloud's compose:

```yaml
mediamtx:
  ports:
    - "8554:8554"          # RTSP, public if you also serve direct push (optional)
    - "9997:9997"          # API — Tailscale ACL gates by source tag
    - "80:8888"            # HLS muxer for browser playback
```

Then restart cloud mediamtx once to pick up the new auth:

```bash
sudo docker compose restart mediamtx
```

## Tailscale ACL

Allow boxes to reach the cloud's mediamtx API:

```json
{
  "tagOwners": {
    "tag:cloud":    ["you@example.com"],
    "tag:edge-box": ["you@example.com"]
  },
  "acls": [
    { "action": "accept", "src": ["tag:cloud"],    "dst": ["tag:edge-box:8554"] },
    { "action": "accept", "src": ["tag:edge-box"], "dst": ["tag:cloud:9997"]   }
  ]
}
```

(If you're not using tags yet, the default "everyone in tailnet can reach
everyone" ACL works fine for a small fleet.)

## Box-side setting

In the box's installer UI, **Site & Cloud** card:

- **Box's Tailscale hostname**: this box's FQDN, e.g.
  `school-main.tail1234.ts.net`
- **Cloud playback hostname**: e.g. `merlin-cloud`
- **Cloud mediamtx API URL**: `http://merlin-cloud:9997`

Save. On every camera add/edit/delete after that, the box will:

1. Add/patch/delete its own local mediamtx path (already happens)
2. **Add/patch/delete the corresponding `<site>-<slug>` path on the cloud
   mediamtx via API** (new)

The cloud will then accept on-demand pulls for the new camera immediately —
no restart, no manual paste.

## What the box owns vs. doesn't

- The box only adds/patches/deletes paths whose name **starts with its own
  `<site-slug>-`** (e.g. `school-main-front-door`). Other paths on the
  cloud (from other sites, or manually defined) are untouched.
- If you change a box's site slug, the old `<old-slug>-*` paths on the
  cloud are **not** auto-cleaned. Delete them by hand on the cloud:
  ```bash
  curl -X DELETE http://merlin-cloud:9997/v3/config/paths/delete/<old-slug>-front-door
  ```

## Verify auto-sync is working

From any box, change something trivial in the UI (toggle a camera, save
site), then on the cloud:

```bash
curl -s http://localhost:9997/v3/config/paths/list | jq '.items[].name'
```

Should list every camera from every box. New cameras appear within ~2s
of being added on the box.

## When auto-sync fails

The box logs sync attempts in the installer-ui container:

```bash
sudo docker compose logs --tail=30 installer-ui | grep -iE 'cloud'
```

Common errors:

| Box log | Cause | Fix |
|---|---|---|
| `cloud paths/list failed (0): ...` | cloud unreachable | Tailscale ACL or DNS wrong |
| `cloud paths/list failed (401): ...` | auth denied | add CGNAT to cloud's `authInternalUsers` |
| `cloud sync skipped: tailnetHost not set` | box's own tailnet name unset | set in UI |

The auto-sync is best-effort — failures are logged but don't block the
local mediamtx from updating. The box's own paths and local recording
are unaffected.
