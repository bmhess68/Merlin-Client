#!/usr/bin/env bash
# Probe a configured camera's local source URL using ffmpeg on the host.
# Usage: bash scripts/test-local-camera.sh [camera-slug]
# If no slug is given, the first enabled camera in data/config.json is used.
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

slug="${1:-}"
if [[ -z "${slug}" ]]; then
  slug="$(jq -r '.cameras[] | select(.enabled==true) | .slug' data/config.json | head -n1)"
fi

if [[ -z "${slug}" ]]; then
  echo "No enabled camera found in data/config.json."
  exit 1
fi

cam_json="$(jq --arg s "${slug}" '.cameras[] | select(.slug==$s)' data/config.json)"
if [[ -z "${cam_json}" ]]; then
  echo "Camera '${slug}' not found."
  exit 1
fi

source_url="$(echo "${cam_json}" | jq -r '.sourceUrl')"
transport="$(echo "${cam_json}" | jq -r '.rtspTransport // "tcp"')"
tls_verify="$(echo "${cam_json}" | jq -r '.tlsVerify // false')"

echo "Testing camera '${slug}': ${source_url}"

tls_args=()
if [[ "${tls_verify}" != "true" ]]; then
  tls_args=(-tls_verify 0)
fi

if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -hide_banner -loglevel error \
    "${tls_args[@]}" \
    -rtsp_transport "${transport}" \
    -i "${source_url}" \
    -t 2 -f null -
else
  timeout 12 docker run --rm --network host linuxserver/ffmpeg:latest \
    ffmpeg -hide_banner -loglevel error \
    "${tls_args[@]}" \
    -rtsp_transport "${transport}" \
    -i "${source_url}" \
    -t 2 -f null -
fi

echo "Local camera probe succeeded for '${slug}'."
