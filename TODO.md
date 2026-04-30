# MerlinConnect Edge Box — TODO

Active backlog. Check items off as they land. Add items at the bottom of the
relevant section. Each item has rough effort and ownership noted.

---

## Cloud-side items

The architecture moved to "cloud is master" via the
`/api/v1/admin/cloud-pull-cameras` admin endpoint
([docs at merlin-map-video-clients-api.txt](merlin-map-video-clients-api.txt)).
Several previously-listed cloud blockers are now obsolete (struck through).

- ~~Reopen mediamtx API port `:9997` on the cloud~~ — **no longer needed**.
  Box no longer talks to cloud's mediamtx API directly; it POSTs camera
  registration to the new admin endpoint on port 80 instead.

- ~~`authInternalUsers` permitting `100.64.0.0/10` for `action: api`~~ —
  no longer needed for the same reason.

- ~~Reopen mediamtx playback port `:9996` on the cloud~~ — **moot for
  DVR**. The agreed DVR plan goes browser → cloud Caddy → **box's** `:9996`
  (the playback-shim), so the cloud's own `:9996` was never in the data
  path.

- [ ] **Configure Caddy `handle /dvr/*` block on the cloud** that
      reverse-proxies to `http://merlin-edge-videoclient:9996`. Box-side is
      ready (playback-shim accepts `?user=&pass=`). After Caddy's wired,
      browser HLS DVR seeks should work. *Owner: cloud team. Effort: 4 lines
      Caddyfile + restart.*

- [ ] **Set Merlin webapp's `MEDIAMTX_PLAYBACK_URL` to point at the cloud's
      `/dvr` prefix.** Once Caddy is up, PlaybackView starts requesting
      DVR through the proxy. *Owner: cloud team. Effort: env var change +
      redeploy.*

- [ ] **Tailscale ACL: confirm `merlin-cloud → merlin-edge-videoclient:9996`
      is permitted.** Same direction/host pattern as the existing `:8554`
      rule. *Owner: tailnet admin. Effort: 1 ACL line.*

- [ ] **Cloud agent: build `/api/edge-health` receiver.** Box already POSTs
      every 60s when `cloud.healthUrl` is set. Spec at
      [docs/cloud-health-api.md](docs/cloud-health-api.md). MVP: persist
      last-seen + per-camera state, alert on staleness. *Owner: cloud team.*

- [ ] **Cloud-side gap.mp4 fix.** Cloud's HLS muxer references `gap.mp4`
      placeholders during source startup that 404 in the browser. Visible
      to viewers as a brief glitch on cold-start. *Owner: cloud team. Cosmetic.*

---

## Box-side: reliability

Reduces "alive but stuck" failures and gives explicit signal for cameras
that look healthy but aren't producing frames.

### Recommended bundle (highest value)

- [ ] **A. Frame-progress watchdog.** Every 60s, query mediamtx
      `bytesReceived` per path. If a path's byte count hasn't grown in
      60s, kill that camera's ffmpeg — supervisor restarts cleanly.
      Catches TLS-connected-but-no-media cases.
      *Owner: us. Effort: ~40 lines in [relay/supervisor.py](relay/supervisor.py).*

- [ ] **B. Disk-fill guardrail.** Watch `/data` free space; warn at <5 GB,
      surface in UI Host card and `/api/health`. At <1 GB, preemptively
      delete oldest recording files across all cameras until recovered
      (smarter than per-camera `recordDeleteAfter` because it triggers on
      actual pressure). *Owner: us. Effort: ~30 lines.*

- [ ] **F. Per-camera "last frame" timestamp in UI.** Camera row shows
      "live since 14:02" or "stale since 18:43". Reuses A's bytes-progress
      data. Makes "looks fine but isn't" cases visible at a glance.
      *Owner: us. Effort: ~15 lines, mostly UI.*

### Lower priority

- [ ] **C. Exponential backoff for chronic flappers.** Current: fixed 3s
      retry. Better: after 5 fast failures in 60s, step to 30s, then 5min.
      Reset to fast on first 30+s successful uptime. Cuts log spam from
      doorbell-style cameras. *Owner: us. Effort: ~20 lines in supervisor.*

- [ ] **D. Per-container memory limits in compose.** Defensive depth so
      one container's leak can't OOM-kill the others. *Owner: us. Effort:
      5 lines in `compose.yaml`.*

- [ ] **E. Mediamtx healthcheck in compose.** Compose `healthcheck:` that
      hits `/v3/paths/list` every 30s, marks unhealthy after 3 fails,
      `restart: unless-stopped` recreates. Catches deadlocked-but-running
      mediamtx. *Owner: us. Effort: 5 lines.*

---

## Box-side: ops + polish

- [ ] **Docker daemon DNS fix.** Add `100.100.100.100` to
      `/etc/docker/daemon.json` so containers resolve tailnet names via
      MagicDNS. Eliminates the recurring `cloud health POST failed: Name
      does not resolve` log spam and lets us put `merlin-cloud` (not its
      IP) into cloud config fields. *Owner: us. Effort: 3 lines + Docker
      restart.*

- [ ] **"Restart this camera" button** in each camera row. Kicks just one
      ffmpeg without bouncing the whole stack. *Effort: 20 lines.*

- [ ] **"Test now" button** on Add/Edit Camera form. Runs a preflight
      ffprobe against the entered URL before saving — catches typos and
      bad tokens before they become a flapping camera. *Effort: 30 lines.*

- [ ] **Config backup/restore in UI.** Download `data/config.json` button
      and Upload button. Disaster recovery for a dead SSD; also useful
      for cloning one box's config to another. *Effort: 25 lines.*

- [ ] **Set `ADMIN_PASSWORD` in `.env` on this box.** Optional auth on the
      installer UI. Currently anyone on tailnet/LAN can edit cameras.
      One-line change in `.env`; restart installer-ui. *Effort: 1 minute.*

- [ ] **Move installer-ui JS out of triple-quoted Python string.** The
      `\n` bug class will recur. Pull HTML/JS into a separate static
      file served by Flask. *Effort: half day. Risk: low — strictly
      mechanical.*

- [ ] **Tailscale ACL: allow SSH** to edge boxes from the operator tag.
      Currently `tailscale up --ssh` is enabled but the ACL doesn't grant
      access. Either add the ACL rule or drop `--ssh` from bootstrap.
      *Owner: tailnet admin. Effort: 1 ACL line.*

---

## Today's specific issues

- [ ] **doorbell**: drop UniFi quality from High to Medium. The 1600×1200
      stream is the highest-resolution camera and is dropping its TLS
      connection intermittently (visible as `End of file` / `Invalid
      data found` in `relay` logs). Lower bitrate likely fixes it. Also
      try refreshing the RTSPS token from UniFi Protect — could have
      rotated. *Owner: us. Effort: 5 minutes in UniFi Protect + paste new
      URL into UI Edit dialog.*

- [ ] **Verify backdoor playback.** Backdoor's box-side state shows
      healthy (no failures, bytes flowing). If it's failing in the cloud
      Merlin map, that's a cloud-side issue (auto-sync hasn't pushed
      changes since :9997 went down) — workaround is the manual YAML
      paste from the UI. *Owner: us / cloud team.*

---

## Future / nice-to-have

- [ ] **Snapshot live preview** in the UI. Click thumbnail → small inline
      hls.js player overlays. Removes the "open ffplay externally" step
      during install. *Effort: 1-2 hours.*

- [ ] **Per-camera schedule** (record only between e.g. 06:00–22:00).
      Useful for residential / business-hours customers; cuts disk usage
      on cameras pointed at non-active areas overnight. *Effort: half
      day.*

- [ ] **Multi-quality stream picker** for cameras that expose multiple
      qualities (UniFi exposes High/Medium/Low). UI dropdown; box pulls
      the picked one. *Effort: medium — needs UniFi-specific URL
      knowledge or per-source config.*

- [ ] **Per-camera bitrate meter (live)** in UI. Useful for sizing
      decisions and detecting misconfigured streams. *Effort: small —
      reuses watchdog A's bytes-progress data, displays as a moving
      average.*

- [ ] **ONVIF auto-discovery** for adding cameras. Scans LAN, lists
      ONVIF-capable devices, click to add. Eliminates URL hunting.
      *Effort: 1-2 days.*

- [ ] **Webhook for critical events** (camera flapping >5min, disk
      >90%, all cameras down). Pushes to Slack / email / cloud agent.
      Belongs partly on cloud agent receiving health POSTs, but a box-side
      stub for "I can't reach the cloud and something is on fire" has
      independent value. *Effort: small box side, larger cloud side.*

- [ ] **Per-box bearer token auth** for box → cloud health POSTs. Today
      the POST is anonymous over tailnet (acceptable for tight ACLs).
      For multi-tenant: add `boxToken` to box config, `Authorization:
      Bearer <token>` header on POST, cloud verifies token → site
      mapping. *Effort: small box side, varies cloud side.*

- [ ] **Snapshot thumbnails over WebRTC** instead of fMP4 1-frame
      ffmpeg pulls. Much faster, much less server load. Currently the
      snapshot loop spawns a fresh ffmpeg every 60s per camera. *Effort:
      medium.*

---

## Done (chronological reverse)

- ✅ Cloud registration via `/api/v1/admin/cloud-pull-cameras` admin endpoint
  (cloud is master; replaces the deprecated direct-mediamtx-API push)
- ✅ `CONTROL_API_KEY` env var plumbing through compose + .env
- ✅ `cloud_paths_yaml()` and former `sync_cloud_mediamtx_paths()` now emit
  `rtspTransport: tcp` so cloud puller doesn't try UDP first and get RTSP 400
- ✅ Edit Camera modal `logLevel: null` validation bug fix
- ✅ Playback shim: query-string auth → mediamtx Basic auth
- ✅ mediamtx config: `playback: yes` + `merlinread` user
- ✅ "Sync cloud now" button + structured cloud-sync result
- ✅ Auto-prepend `http://` on mediamtx API URL / health URL
- ✅ Cloud-side mediamtx auto-sync via API per-path add/patch/delete
- ✅ Switched architecture from option D (push) to option C (cloud pull on demand)
- ✅ Per-camera log prefix in supervisor
- ✅ Periodic snapshot thumbnails per camera
- ✅ Container health pills + log viewer + Test cloud button in UI
- ✅ Docker socket re-mounted on installer-ui (read-only) for log + service inspection
- ✅ Local recording with retention via mediamtx native (replaces supervisor segments)
- ✅ Multi-camera supervisor (one ffmpeg per enabled camera, hot-reload on config change)
- ✅ Tailscale-aware bootstrap (SITE_SLUG = tailnet hostname)
- ✅ One-shot install.sh
- ✅ Update.sh + desktop launchers
- ✅ Host stats panel (CPU/RAM/Disk/Mbps)
- ✅ Initial GitHub push + repo set up
