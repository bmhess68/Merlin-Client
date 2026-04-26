#!/usr/bin/env bash
# Publish the local installer UI over Tailscale Serve so other tailnet members
# can reach it via https://<box-tailnet-name>.<tailnet>.ts.net/ — with a real
# cert provisioned by Tailscale automatically.
#
# Run once after the box has joined the tailnet. State persists across reboots.
set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Tailscale not installed. Run: sudo bash scripts/bootstrap-box.sh"
  exit 1
fi

if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale not joined. Run: sudo tailscale up --auth-key=tskey-... --hostname=... --ssh"
  exit 1
fi

echo "==> Publishing http://127.0.0.1:8080 at https:// (default tailnet cert)"
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080

echo
echo "==> Tailscale serve status"
sudo tailscale serve status
echo
echo "Reachable at: https://$(tailscale status --self --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || echo '<box>.<tailnet>.ts.net')/"
