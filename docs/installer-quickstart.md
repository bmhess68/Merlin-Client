# MerlinConnect Edge Box — Installer Quick Start

A printed cheat-sheet for the technician installing a box at a customer site.
Designed to fit on **two pages**. Skip nothing.

---

## Before you arrive

Bring:

- The Merlin Edge mini-PC (with power adapter)
- An Ethernet cable
- A laptop or phone on the same Wi-Fi/LAN as you'll be installing on
- The Tailscale **auth key** for this site (your dispatcher provides this — looks like `tskey-auth-...`)
- The site's intended **slug** (lowercase letters, digits, dashes — e.g. `riverside-gym`, `acme-warehouse-2`)
- A list of cameras and their RTSP/RTSPS URLs (with credentials if needed)

Confirm with the customer:

- The mini-PC will live somewhere with cooling, power, and Ethernet — same network as the cameras
- They've allowed outbound internet (we don't need any inbound ports)

---

## On site

### 1. Plug in

- Connect Ethernet from the customer's switch to the box
- Connect power
- Wait ~60 seconds for it to boot

### 2. Find the box's LAN IP

On the box's screen (if attached) it'll show in the system tray. If headless:

- Check the customer's router's connected-devices list — look for `merlin-edge-...`
- Or scan the LAN: `nmap -sn 192.168.1.0/24` from your laptop

Note the IP. You'll use it for SSH and the installer UI.

### 3. SSH in (one time, for bootstrap)

Default login on a fresh box is the credential your dispatcher gave you.

```bash
ssh wcpd@<box-ip>
```

### 4. Run the one-shot installer

```bash
curl -fsSL https://raw.githubusercontent.com/bmhess68/Merlin-Client/main/scripts/install.sh \
  | sudo SITE_SLUG=<site-slug> \
         TAILSCALE_AUTH_KEY=tskey-auth-... \
         bash
```

What this does (takes 3–5 minutes):

- Installs Docker, Tailscale, and ffmpeg (if not already there)
- Joins the box to the Merlin tailnet with the site slug as its hostname
- Pulls the latest project from GitHub
- Builds and starts the three services (installer-ui, mediamtx, relay supervisor)

When it finishes, you'll see something like:

```
Bootstrap complete.
  - Docker:    Docker version 29.x.x
  - Compose:   Docker Compose version v5.x
  - Tailscale: 100.112.231.52
  - Tailnet DNS: riverside-gym.tail7e85.ts.net

Install complete.
  Installer UI: http://192.168.1.105:8080
  Tailnet UI:   http://100.112.231.52:8080
```

**Write down the Tailnet DNS** — you'll need it in the next step.

### 5. Open the installer UI

In your laptop's browser:

```
http://<box-ip>:8080
```

You'll see three pills at the top — `installer-ui: running`, `mediamtx: running`, `relay: running`. All three should be green. If any are red or yellow, scroll to the **Logs** card at the bottom, pick that service from the dropdown, and read the last few lines.

### 6. Configure Site & Cloud (right column)

Use **IP addresses, not Tailscale MagicDNS names**, for any tailnet host. The
cloud server's mediamtx runs in a Docker container that doesn't always
resolve Tailscale DNS; IPs are stable and reliable.

- **Site slug**: same slug you used in the install command (e.g. `riverside-gym`). Click **Save site**.
- **Box's tailnet address**: the **Tailnet IP** the install printed (e.g. `100.86.38.62`). NOT the MagicDNS name.
- **Cloud playback hostname**: the cloud's public IP or DNS name as supplied by the dispatcher (e.g. `147.182.179.39`).
- **Cloud admin API URL**: `http://<cloud-tailnet-ip>/api/v1/admin/cloud-pull-cameras` — the dispatcher gives you the cloud's tailnet IP.
- **Cloud health URL**: `http://<cloud-tailnet-ip>/edge-health` (optional; if dispatcher hasn't set up the receiver, leave blank).

You also need the **`CONTROL_API_KEY`** the dispatcher supplied. Add it to the box's `.env`:

```bash
echo 'CONTROL_API_KEY=<paste-the-key>' | sudo tee -a /opt/merlin-edge/.env
sudo docker compose -f /opt/merlin-edge/compose.yaml up -d --build installer-ui
```

Then click **Save cloud**, then **Test cloud** — every check should be ✓ green. If any are ✗, **stop and call your dispatcher** before adding cameras.

### 7. Add cameras

For each camera the customer wants:

- **Slug**: short, lowercase, dashes. Examples: `front-door`, `parking-east`, `loading-bay`. **Don't use spaces or capitals.**
- **Display name**: human-readable label (e.g. "Front Door").
- **Source URL**: full RTSP/RTSPS URL with any credentials. UniFi cameras are usually `rtsps://192.168.x.x:7441/<long-token>?enableSrtp`.
- **TLS verify**: pick **no** for any UniFi camera or any camera with a self-signed certificate. Pick **yes** only if you know the camera has a real certificate.
- **Record locally**: usually **yes**.
- **Retain hours**: how long to keep recordings. Default 168 (one week). Tune to customer's storage and policy.

Click **Add Camera**.

Within ~10 seconds the row should show:
- A live snapshot thumbnail of the camera
- A **running** green pill

If it shows **flapping** or **failing**, the URL or credentials are wrong. Click **Edit** to fix, or **Delete** and try again.

### 8. Verify recording is working

```bash
ls -lh /opt/merlin-edge/data/recordings/<camera-slug>/
```

You should see one or more `.mp4` files growing over time. If the directory is empty after 60+ seconds, recording isn't running — check the relay logs.

### 9. Test cloud playback

In your laptop's browser:

```
http://merlin-cloud/live/<site-slug>-<camera-slug>/index.m3u8
```

If you have **mpv** or **ffplay** installed locally, that's more reliable than VLC.

```bash
ffplay http://merlin-cloud/live/<site-slug>-<camera-slug>/index.m3u8
```

You should see the camera within ~3 seconds. If the player shows nothing or errors, **stop** — go back and check the cloud sync.

### 10. Hand off

Tell the customer:

- The box runs in the background, no interaction needed
- Cameras are recording locally for `<retainHours>` then auto-deleted
- They can view live and recorded video from the Merlin web app
- If they ever need to add a camera or change a setting, they should call you (or whoever holds the SSH/installer UI access)

You're done.

---

## Troubleshooting cheat sheet

**A service pill is red or yellow.** Scroll to **Logs** card, pick that service, read the last 50 lines. Copy and send to dispatch if you can't fix it.

**Camera row shows flapping.** Open the relay log, look for `[<slug>] starting` — the next line after that is ffmpeg's actual error. Common causes:
- Wrong RTSP URL or token expired → re-copy from UniFi Protect
- Camera is on a different VLAN the box can't reach → ask the customer to fix the network
- TLS verify set wrong → toggle the **TLS verify** field

**Test cloud shows ✗ on Cloud mediamtx API.** Ask dispatch:
- Is the cloud's IP the right one for the Cloud mediamtx API URL?
- Has dispatch added this site's tailnet IP to the cloud's auth list?

**No cloud playback even though everything is green on the box.** Click **Sync cloud now** in the Site & Cloud card. If the toast says `1 added (...)`, retry playback. If it says `Cloud sync errors: ...`, send the message to dispatch.

**Disk filling up faster than expected.** In each camera row, click **Edit**, drop **Retain hours**. 168 hours (one week) at 8 cameras × 4 Mbps ≈ 1 TB.

**Box was rebooted by customer / power outage.** Should come back automatically. If it doesn't, SSH in and run:

```bash
cd /opt/merlin-edge
sudo docker compose up -d
```

**To update a box to the latest version:**

```bash
sudo bash /opt/merlin-edge/scripts/update.sh
```

That pulls latest from GitHub and rebuilds. Cameras drop offline for ~30 seconds.

---

## What this box does, in plain English

It sits on the customer's network, connects to each camera, and continuously records video to its local SSD. It's also joined to a private network (Tailscale) that connects it to your office. When somebody on Merlin's web app clicks a camera, the cloud reaches out over Tailscale, asks this box for the camera's stream, and shows it to the user. **No video flows to the internet unless someone is watching it**, which is why this is so much more bandwidth-friendly than always-on cloud uploads. The recordings stay on the box's local disk until they age out per your retention setting.
