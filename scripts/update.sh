#!/usr/bin/env bash
# Pull the latest project tree and rebuild the stack in place.
# Use after a "git push" from your dev machine.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

if [[ -d .git ]]; then
  echo "==> git pull"
  git fetch --all --prune
  git pull --ff-only
else
  echo "WARN: ${repo_root} is not a git checkout; skipping pull. Update files by hand."
fi

echo "==> docker compose up -d --build"
docker compose up -d --build

echo
echo "==> docker compose ps"
docker compose ps
