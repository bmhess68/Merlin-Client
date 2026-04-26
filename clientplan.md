# MerlinConnect Edge Box MVP Plan

This document is the clean implementation plan for the first MerlinConnect client box that will connect local cameras to the test cloud server running at `147.182.179.39`.

The goal is not to build the final production appliance yet. The goal is to build the simplest edge relay box that works reliably enough to prove the architecture.

## Objective

Build a small Linux box that:

- connects to one or more local RTSP cameras on the same LAN
- keeps all camera credentials on the box
- republishes those streams outbound to the cloud server
- survives reboot and network interruption
- can be operated by an LLM or a junior operator with clear instructions

## What We Are Building First

For the MVP, the box will:

- run `mediamtx`
- pull RTSP streams from local cameras
- publish them to the cloud `mediamtx` instance over outbound RTSP/TCP
- optionally use `ffmpeg` only for testing and diagnostics

This is intentionally simpler than the final MerlinConnect design in `plans.md`.

We are not building yet:

- device provisioning
- mTLS tunnel
- OTA updates
- TPM key handling
- encrypted local ring buffer
- tenant-aware control plane on the box

Those come after the first live relay works.

## Important Decision: No WireGuard For MVP Media

Do not make WireGuard a requirement for the first working box.

Why:

- the cloud server can already accept inbound RTSP publishes from the box
- the box only needs outbound connectivity
- the box does not need inbound access from the internet to relay media
- WireGuard adds network complexity before we have proven the media path

WireGuard may still be added later for:

- remote SSH or maintenance
- management-plane access
- support access to the box

But media delivery should not depend on it in the MVP.

## Architecture

### Local side

- One mini PC or NUC-class box running Ubuntu or Debian
- One or more cameras on the same LAN
- The box can open each camera's RTSP stream locally

### Cloud side

- Existing test server at `147.182.179.39`
- `mediamtx` already running there
- RTSP publish port exposed at `8554/tcp`
- HLS playback available at `http://147.182.179.39/live/<slug>/index.m3u8`

### Media flow

```text
Local camera RTSP -> Edge box MediaMTX -> outbound RTSP publish -> Cloud MediaMTX -> HLS playback
```

Example:

- Local camera source on the box:
  - `rtsp://admin:password@192.168.1.50:554/stream1`
- Cloud publish target:
  - `rtsp://147.182.179.39:8554/live/site1-front-door`
- Browser playback URL:
  - `http://147.182.179.39/live/site1-front-door/index.m3u8`

## Hardware Recommendation

For the MVP client box:

- Intel NUC, ASUS NUC, Beelink, or similar mini PC
- 8 GB RAM minimum
- 128 GB SSD minimum
- Gigabit Ethernet
- Ubuntu 24.04 LTS or Debian 12

Do not use SD-card-based Raspberry Pi for sustained production relay work unless you already know exactly why you are doing it.

## Software Stack On The Box

Install:

- Docker Engine
- Docker Compose plugin
- MediaMTX in Docker
- `curl`
- `ffmpeg` optional for testing only

Keep the box simple:

- one Compose project
- one config directory
- one `.env` file
- one systemd service if needed

## Box MVP Responsibilities

The box must:

- pull local camera RTSP streams
- reconnect automatically if camera or WAN drops
- publish those streams to cloud paths
- start on boot
- expose logs for troubleshooting

The box does not need:

- a web UI
- multi-tenant awareness
- browser playback
- direct public exposure

## Clean File Layout For The Box

Use this layout on the box:

```text
/opt/merlin-edge/
├── compose.yaml
├── .env
├── mediamtx.yml
├── scripts/
│   ├── status.sh
│   ├── test-local-camera.sh
│   └── test-cloud-publish.sh
└── README.md
```

## Phase 1: Bring Up The Box

### Step 1

Install Docker and Compose.

### Step 2

Create `compose.yaml` for one `mediamtx` container.

### Step 3

Create `mediamtx.yml` that defines:

- one path per local camera
- each path pulls from local RTSP
- each path can be forwarded or republished to the cloud

### Step 4

Put camera credentials in `.env`.

### Step 5

Bring the box up and verify:

- camera is reachable locally
- stream is visible inside box `mediamtx`
- stream can be published to the cloud

## Phase 2: Prove End-To-End Relay

### Success criteria

For one camera:

1. The box can read the camera locally.
2. The box can publish to `rtsp://147.182.179.39:8554/live/<slug>`.
3. The stream is viewable at `http://147.182.179.39/live/<slug>/index.m3u8`.

If those three are true, the edge relay concept is proven.

## Phase 3: Make It Operational

After first light works:

- enable restart policies
- add systemd autostart if needed
- add a health-check script
- add a simple naming convention for camera paths
- document replacement/reboot steps

## Naming Convention

Use predictable cloud paths:

```text
live/<site>-<camera-name>
```

Examples:

- `live/rye-i95-northbound`
- `live/church-front-door`
- `live/school-main-office`

Avoid spaces and punctuation. Use lowercase and dashes.

## What To Ask An LLM To Generate

Hand another LLM this exact task:

> Build a Docker Compose based Linux edge relay box for MerlinConnect MVP. The box must run MediaMTX, pull one or more local RTSP camera streams, and publish them outbound to a cloud MediaMTX server at `147.182.179.39:8554`. The box must use environment variables for camera URLs and destination paths, auto-reconnect on failure, and be easy to start on boot. Generate:
> 
> 1. `compose.yaml`
> 2. `mediamtx.yml`
> 3. `.env.example`
> 4. `scripts/test-local-camera.sh`
> 5. `scripts/test-cloud-publish.sh`
> 6. `scripts/status.sh`
> 7. `README.md`
> 
> Keep the setup minimal and production-sensible for a pilot deployment. Do not require WireGuard for media transport.

## Questions The Builder Must Resolve

Before or during implementation, the builder should confirm:

- the exact local RTSP URL for each camera
- whether the camera needs TCP transport forced
- the slug to use for each cloud stream path
- whether the box should transcode or pass through

Default answer for MVP:

- use RTSP over TCP
- use passthrough where possible
- do not transcode unless the source format forces it

## Passthrough vs Transcode

Prefer passthrough first.

Passthrough is best when the camera already emits:

- H.264
- H.265
- AAC or no audio

Use transcoding only if:

- the camera only provides MJPEG in a way the cloud side cannot use well
- the camera codec breaks playback compatibility
- bandwidth must be reduced

Transcoding increases CPU cost and complexity on the box. Avoid it until needed.

## Firewall Expectations

### At the remote site

The box should not require any inbound internet firewall openings for normal relay operation.

The box only needs outbound internet access to the cloud server.

### On the cloud server

Allow inbound from the box to:

- `8554/tcp` for RTSP publish
- `80/tcp` for HLS playback and control API if needed

## LLM-Friendly Acceptance Test

A builder should be able to complete this exact test:

1. Start the box stack.
2. Confirm local RTSP opens from the box.
3. Confirm the box can publish a test path to the cloud.
4. Open `http://147.182.179.39/live/<slug>/index.m3u8`.
5. Confirm video plays.

If that works, the MVP is complete.

## Recommended Next Step After MVP

Once the box can relay one camera reliably, the next upgrade should be:

1. persistent config storage
2. multiple camera support
3. health reporting to cloud
4. a secure control plane
5. only then evaluate WireGuard for admin access

## Bottom Line

The first box should be:

- simple
- outbound-only
- RTSP pull locally
- RTSP publish to cloud
- no VPN dependency

That gives the fastest path to a working field relay and creates the right base for the more advanced MerlinConnect appliance later.
