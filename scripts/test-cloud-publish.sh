#!/usr/bin/env bash
# Verify a configured camera's HLS playback URL is live on the cloud.
# Usage: bash scripts/test-cloud-publish.sh [camera-slug]
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

if [[ ! -f data/config.json ]]; then
  echo "Missing data/config.json. Save config in the installer UI first."
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Installing jq (sudo)..."
  sudo apt-get install -y jq
fi

site_slug="$(jq -r '.site.slug' data/config.json)"
cloud_host="$(jq -r '.cloud.host' data/config.json)"

slug="${1:-}"
if [[ -z "${slug}" ]]; then
  slug="$(jq -r '.cameras[] | select(.enabled==true) | .slug' data/config.json | head -n1)"
fi

if [[ -z "${slug}" || -z "${cloud_host}" || -z "${site_slug}" ]]; then
  echo "Missing site slug, cloud host, or camera slug."
  exit 1
fi

url="http://${cloud_host}/live/${site_slug}-${slug}/index.m3u8"
echo "Checking cloud playback URL: ${url}"
curl -fsS "${url}"
echo
echo "Cloud playback URL responded successfully for '${slug}'."
