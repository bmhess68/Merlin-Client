# Edge Box → Merlin-Cloud & Merlin-Map API contract

This is the contract the **edge box** (installer-ui) calls. Implement these
endpoints on **merlin-cloud** and **merlin-map** to match. The box derives every
URL from two operator-entered addresses in its Site & Cloud screen:

| Operator enters | Box derives |
|---|---|
| **Merlin-cloud address** (`cloud.cloudHost`) | `http://<cloud>/api/v1/admin/cloud-pull-cameras` (stream paths)<br>`http://<cloud>/edge-health` (health POSTs)<br>playback host for display |
| **Merlin-map address** (`cloud.mapHost`) | `http://<map>/api/v1/cameras/geo` (camera geo) |

`<cloud>` and `<map>` may be the **same** host. Each is a bare IP or host,
optionally with `:port` (e.g. `100.112.231.52` or `10.0.0.5:8080`); the box
prepends `http://` and the fixed path. The paths below are **fixed** — do not
make the box configure them; only the host is configurable.

All calls go over Tailscale. No public exposure required.

---

## Authentication

Every box→cloud and box→map call carries a shared secret:

```
x-api-key: <CONTROL_API_KEY>
```

`CONTROL_API_KEY` lives in the box's `.env`. The cloud and map must be
configured with the **same** value and reject (HTTP 401/403) any request whose
`x-api-key` doesn't match. (The health endpoint is currently unauthenticated —
see §3.)

---

## 1. Stream-path registration — merlin-cloud

Registers the box's camera list so the cloud mediamtx can pull each camera
on demand. **The cloud is master**: it owns mediamtx path generation.

| | |
|---|---|
| Method | `POST` |
| URL | `http://<merlin-cloud>/api/v1/admin/cloud-pull-cameras` |
| Headers | `content-type: application/json`, `x-api-key: <CONTROL_API_KEY>` |
| When | Operator clicks **Sync now…** on the box (manual, confirmed). **Not** on every camera save anymore. |

### Request body

```json
{
  "site": "site1",
  "tailnetHost": "100.86.38.62",
  "cameras": [
    { "slug": "front-door", "label": "Front Door",
      "sourceOnDemand": true,
      "sourceOnDemandStartTimeout": "15s",
      "sourceOnDemandCloseAfter": "30s",
      "rtspTransport": "tcp" },
    { "slug": "parking", "label": "Parking Lot",
      "sourceOnDemand": true,
      "sourceOnDemandStartTimeout": "15s",
      "sourceOnDemandCloseAfter": "30s",
      "rtspTransport": "tcp" }
  ]
}
```

- `site` — the site slug; namespaces all of this box's paths.
- `tailnetHost` — the **box's own** tailnet IP. The cloud uses it to build the
  pull source (see below).
- `cameras[]` — per camera:
  - `slug` (unique within the site) and a human `label`. Only enabled cameras
    are sent; the box guarantees slugs are unique per payload.
  - **`sourceOnDemand`** — always `true`. The cloud must pull only when a
    viewer is watching (this is the core "pull on demand" design). Earlier the
    box omitted this and relied on the cloud to default it; it now states it
    explicitly. **Honor it; do not hardcode the opposite.**
  - `sourceOnDemandStartTimeout` / `sourceOnDemandCloseAfter` — pull timing
    (strings like `"15s"`, `"30s"`).
  - `rtspTransport` — always `"tcp"`. **REQUIRED** — the box mediamtx is
    `rtspTransports: [tcp]`; UDP gets RTSP 400.

### Required semantics — **full replace per site (upsert, never append)**

This is what prevents duplicate/stale paths and the YAML reverts we hit before:

1. Treat the posted `cameras[]` as the **complete, authoritative** set for
   `site`. Delete any previously-generated path for this `site` whose slug is
   **not** in the new array; upsert the rest. Leave other sites untouched.
2. For each camera, generate a mediamtx path **from the fields in the payload**
   (do not substitute your own pull policy):

   ```yaml
   <site>-<slug>:                                  # from site + slug
     source: rtsp://<tailnetHost>:8554/<slug>      # from tailnetHost + slug
     sourceOnDemand: yes                           # from cameras[].sourceOnDemand
     sourceOnDemandStartTimeout: 15s               # from cameras[].sourceOnDemandStartTimeout
     sourceOnDemandCloseAfter: 30s                 # from cameras[].sourceOnDemandCloseAfter
     rtspTransport: tcp                            # from cameras[].rtspTransport
   ```

   The unique key for a cloud path is **`(site, slug)`** → path name
   `<site>-<slug>`. Re-posting the same set must be idempotent (update in
   place, no duplicates).

### Response

Return `2xx`. JSON body is read opportunistically by the box:

```json
{ "cameraCount": 2, "streamPaths": ["site1-front-door", "site1-parking"] }
```

- `cameraCount` (int) and `streamPaths` (array of generated path names) are
  surfaced in the box UI. Any 2xx is treated as success even without a body.
- On error return a non-2xx with a short text/JSON body; the box shows the
  first 200 chars.

### `HEAD` probe (Test cloud)

The box's **Test cloud** button sends `HEAD` to this URL with the API key.
Respond **405** (method not allowed, since it's POST-only) or **200** — both
read as "reachable". **401/403** reads as "auth wrong"; **404** as "URL wrong".

---

## 2. Camera geo — merlin-map

Pushes per-camera map metadata (latitude, longitude, heading) for the map
program's database. **Sent directly to merlin-map**, deliberately kept out of
the mediamtx admin flow.

| | |
|---|---|
| Method | `POST` |
| URL | `http://<merlin-map>/api/v1/cameras/geo` |
| Headers | `content-type: application/json`, `x-api-key: <CONTROL_API_KEY>` |
| When | Same **Sync now…** action as §1 (both fire together). |

### Request body

```json
{
  "site": "site1",
  "cameras": [
    { "slug": "front-door", "label": "Front Door",
      "lat": 42.6526, "lon": -73.7562, "bearing": 270 }
  ]
}
```

Field reference:

| Field | Type | Notes |
|---|---|---|
| `site` | string | Site slug. |
| `cameras[].slug` | string | Unique within site. |
| `cameras[].label` | string | Human display name. |
| `cameras[].lat` | number \| null | WGS-84 latitude, −90..90. |
| `cameras[].lon` | number \| null | WGS-84 longitude, −180..180. |
| `cameras[].bearing` | number \| null | **Heading** in degrees, `0`..`360`. **`0` = north / straight up on the map**, increasing **clockwise** (90 = east/right, 180 = south/down, 270 = west/left). The box normalizes 360 → 0. |

- Any of `lat`/`lon`/`bearing` may be `null` individually.
- Cameras with **no** geo set at all are **omitted** from the payload — they
  won't create empty rows.
- Only enabled cameras are sent; slugs are unique per payload.

### Required semantics — **upsert by `(site, slug)`**

The unique key is **`(site, slug)`**. Update the row if it exists, insert if
new. Re-syncing must never duplicate. Recommended schema:

```sql
CREATE TABLE camera_geo (
  site       TEXT NOT NULL,
  slug       TEXT NOT NULL,
  label      TEXT,
  lat        DOUBLE PRECISION,
  lon        DOUBLE PRECISION,
  bearing    DOUBLE PRECISION,   -- heading degrees, 0 = north, clockwise
  updated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (site, slug)
);
```

```sql
-- one per camera in the posted array
INSERT INTO camera_geo (site, slug, label, lat, lon, bearing, updated_at)
VALUES ($site, $slug, $label, $lat, $lon, $bearing, now())
ON CONFLICT (site, slug) DO UPDATE SET
  label = EXCLUDED.label, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
  bearing = EXCLUDED.bearing, updated_at = now();
```

> **Note on deletions:** the box currently sends an **upsert** set, not a
> reconcile set. If a camera is deleted or renamed on the box, its old
> `(site, slug)` row will **linger** in merlin-map until cleaned up. If you want
> renamed/removed cameras to disappear automatically, tell the box team and the
> contract switches to "full replace per site" (delete `(site, slug)` rows for
> this site not present in the payload) — same as §1.

### Response

Return `2xx` on success (body optional). On error, non-2xx + short body
(first 200 chars surfaced in the box UI). `HEAD` probe behaves as in §1.

---

## 3. Health reporting — merlin-cloud (optional)

If the merlin-cloud address is set, the box POSTs a status snapshot every
60 seconds.

| | |
|---|---|
| Method | `POST` |
| URL | `http://<merlin-cloud>/edge-health` |
| Headers | `content-type: application/json` |
| Auth | none currently (gate by Tailscale source IP; see `docs/cloud-health-api.md`) |

### Request body (abridged)

```json
{
  "site": "site1",
  "tailnetHost": "100.86.38.62",
  "cloudHost": "100.112.231.52",
  "cameraCount": 3,
  "supervisor": { "...": "per-camera state" },
  "host": { "cpuPercent": 4.1, "memory": {}, "disk": {}, "networkOutMbps": 0.0, "recordings": {} },
  "reportedAt": 1749600000
}
```

Return any `2xx`. See `docs/cloud-health-api.md` for the full field list.

---

## Summary for implementers

| Endpoint | Host | Method | Auth | Key | Idempotency |
|---|---|---|---|---|---|
| `/api/v1/admin/cloud-pull-cameras` | merlin-cloud | POST | `x-api-key` | `(site, slug)` | full replace per site |
| `/api/v1/cameras/geo` | merlin-map | POST | `x-api-key` | `(site, slug)` | upsert |
| `/edge-health` | merlin-cloud | POST | none (IP-gated) | — | n/a |

The box never sends duplicate slugs; duplicate **rows/paths** can only arise if
the receiver appends instead of upserting on `(site, slug)`. Upsert on that key
and the system is duplicate-free no matter how often Sync runs.
