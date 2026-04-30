#!/usr/bin/env bash
# Provisions a fresh Linux host for the Merlin Edge box: Docker Engine + Compose
# plugin, ffmpeg for diagnostics, Tailscale joined with a stable per-site
# hostname so the cloud can pull from this box predictably.
#
# Idempotent.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/bootstrap-box.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

. /etc/os-release
arch="$(dpkg --print-architecture)"
codename="${VERSION_CODENAME}"

echo "==> apt prerequisites"
apt-get update
apt-get install -y ca-certificates curl gnupg ffmpeg jq

echo "==> Docker apt repo"
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi

cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable
EOF

echo "==> Docker Engine + Compose plugin"
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker

real_user="${SUDO_USER:-}"
if [[ -n "${real_user}" && "${real_user}" != "root" ]]; then
  if ! id -nG "${real_user}" | tr ' ' '\n' | grep -qx docker; then
    usermod -aG docker "${real_user}"
    echo "==> Added ${real_user} to docker group (log out/in to apply)"
  fi
fi

echo "==> Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable tailscaled
systemctl start tailscaled

# Determine the desired Tailscale hostname:
#   1. SITE_SLUG env var (preferred — matches site.slug in config.json)
#   2. site.slug from data/config.json if it already exists
#   3. TAILSCALE_HOSTNAME env var (legacy fallback)
#   4. prompt the operator
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_slug=""
if [[ -f "${repo_root}/data/config.json" ]]; then
  config_slug="$(jq -r '.site.slug // ""' "${repo_root}/data/config.json" 2>/dev/null || true)"
fi

ts_hostname=""
if [[ -n "${SITE_SLUG:-}" ]]; then
  ts_hostname="${SITE_SLUG}"
elif [[ -n "${config_slug}" && "${config_slug}" != "site1" ]]; then
  ts_hostname="${config_slug}"
elif [[ -n "${TAILSCALE_HOSTNAME:-}" ]]; then
  ts_hostname="${TAILSCALE_HOSTNAME}"
fi

if tailscale status --peers=false >/dev/null 2>&1 && tailscale ip -4 >/dev/null 2>&1; then
  current_host="$(tailscale status --self --json 2>/dev/null | jq -r '.Self.HostName // ""' 2>/dev/null || true)"
  echo "==> Tailscale already joined as '${current_host}': $(tailscale ip -4 | head -n1)"
  if [[ -n "${ts_hostname}" && "${ts_hostname}" != "${current_host}" ]]; then
    echo "    Renaming to '${ts_hostname}' to match site slug..."
    tailscale set --hostname="${ts_hostname}"
  fi
else
  if [[ -z "${ts_hostname}" ]]; then
    echo
    echo "==> Site slug for this box (becomes the Tailscale hostname)"
    echo "    Use lowercase letters, digits, and dashes — e.g. 'school-main' or 'rye-i95'"
    read -r -p "    Site slug: " ts_hostname
    while [[ -z "${ts_hostname}" || ! "${ts_hostname}" =~ ^[a-z0-9-]+$ ]]; do
      echo "    Invalid. Lowercase letters, digits, dashes only."
      read -r -p "    Site slug: " ts_hostname
    done
  fi

  ts_key="${TAILSCALE_AUTH_KEY:-}"
  if [[ -z "${ts_key}" ]]; then
    echo
    echo "==> Tailscale auth key prompt"
    echo "    Paste a reusable auth key from https://login.tailscale.com/admin/settings/keys"
    echo "    Press Enter with no input to skip Tailscale for now (you can join later)."
    read -r -p "    Auth key (tskey-...): " ts_key
  fi

  if [[ -n "${ts_key}" ]]; then
    tailscale up --auth-key="${ts_key}" --hostname="${ts_hostname}" --ssh
    echo "==> Tailscale joined as ${ts_hostname}: $(tailscale ip -4 | head -n1)"
  else
    echo "==> Skipping Tailscale join. Run later with:"
    echo "    sudo tailscale up --auth-key=tskey-... --hostname=${ts_hostname} --ssh"
  fi
fi

echo
echo "Bootstrap complete."
echo "  - Docker:    $(docker --version 2>/dev/null || echo 'not on PATH yet (re-login)')"
echo "  - Compose:   $(docker compose version 2>/dev/null || echo 'not on PATH yet')"
ts_ip="$(tailscale ip -4 2>/dev/null | head -n1 || echo '')"
ts_dns="$(tailscale status --self --json 2>/dev/null | jq -r '.Self.DNSName // ""' 2>/dev/null | sed 's/\.$//' || true)"
echo "  - Tailnet IP:  ${ts_ip:-not joined}"
if [[ -n "${ts_dns}" ]]; then
  echo "  - Tailnet DNS: ${ts_dns} (informational; use the IP in the box UI)"
fi

# Persist the tailnet IP so the installer-ui can auto-fill the
# "Box's tailnet address" field. Re-run this script any time Tailscale
# rejoins / changes IP to refresh.
if [[ -n "${ts_ip}" ]]; then
  install -d -m 0755 -o "${SUDO_USER:-root}" -g "${SUDO_USER:-root}" "${repo_root}/data" 2>/dev/null || mkdir -p "${repo_root}/data"
  printf '%s\n' "${ts_ip}" > "${repo_root}/data/tailnet-ip.txt"
  chown "${SUDO_USER:-root}":"${SUDO_USER:-root}" "${repo_root}/data/tailnet-ip.txt" 2>/dev/null || true
  echo "  - Wrote ${repo_root}/data/tailnet-ip.txt for installer-ui auto-fill"
fi
echo
echo "Next:"
echo "  cd ${repo_root}"
echo "  docker compose up -d --build"
echo "  open http://<lan-ip>:8080  in a browser on the same LAN"
echo
echo "In the box UI's Site & Cloud card, paste these:"
echo "  Box's tailnet address: ${ts_ip:-<rejoin tailnet first>}"
echo "                         ^^^^^ use the IP, NOT the MagicDNS name."
echo "                         The cloud's mediamtx container can't always"
echo "                         resolve tailnet DNS; IPs are stable and reliable."
if [[ -n "${ts_ip}" ]]; then
  echo
  echo "Cloud-side reference (informational — the cloud admin API generates"
  echo "this automatically when the box registers):"
  echo "  source: rtsp://${ts_ip}:8554/<camera-slug>"
fi
