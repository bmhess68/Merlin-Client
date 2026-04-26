#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

echo "== docker compose ps =="
docker compose ps
echo

echo "== installer-ui health =="
curl -fsS http://127.0.0.1:8080/api/health || true
echo
echo

echo "== mediamtx api paths =="
curl -fsS http://127.0.0.1:9997/v3/paths/list || true
echo
echo

echo "== recent relay supervisor logs =="
docker compose logs --tail=40 relay
