#!/usr/bin/env bash
# One-shot installer for a fresh Merlin Edge box.
#
# Usage on a fresh machine:
#   curl -fsSL <raw-url-to-this-script> | sudo bash
#
# Or, if the project is already cloned locally:
#   sudo bash scripts/install.sh
#
# Env overrides:
#   MERLIN_REPO       git URL of the project (default: see REPO_DEFAULT below)
#   MERLIN_BRANCH     git branch (default: main)
#   MERLIN_DIR        install path (default: /opt/merlin-edge)
#   TAILSCALE_AUTH_KEY  if set, bootstrap joins the tailnet non-interactively
set -euo pipefail

REPO_DEFAULT="https://github.com/bmhess68/Merlin-Client.git"
REPO="${MERLIN_REPO:-${REPO_DEFAULT}}"
BRANCH="${MERLIN_BRANCH:-main}"
TARGET="${MERLIN_DIR:-/opt/merlin-edge}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install.sh"
  exit 1
fi

echo "==> Installing into ${TARGET}"

apt-get update
apt-get install -y ca-certificates curl git

if [[ ! -d "${TARGET}/.git" ]]; then
  if [[ -z "${REPO}" ]]; then
    echo "ERROR: MERLIN_REPO is not set and ${TARGET} is not a git checkout."
    echo "       Either set MERLIN_REPO, or scp the project to ${TARGET} first."
    exit 1
  fi
  git clone --branch "${BRANCH}" "${REPO}" "${TARGET}"
else
  git -C "${TARGET}" fetch --all --prune
  git -C "${TARGET}" checkout "${BRANCH}"
  git -C "${TARGET}" pull --ff-only
fi

cd "${TARGET}"

bash scripts/bootstrap-box.sh

echo "==> Bringing the stack up"
docker compose up -d --build

echo
echo "==> docker compose ps"
docker compose ps

echo
echo "Install complete."
echo "  Installer UI:  http://$(hostname -I | awk '{print $1}'):8080"
if command -v tailscale >/dev/null 2>&1; then
  ts_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -n "${ts_ip}" ]]; then
    echo "  Tailnet UI:    http://${ts_ip}:8080"
  fi
fi
