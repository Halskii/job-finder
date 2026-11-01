#!/usr/bin/env bash
# Pull the latest main and rebuild, but only when there is something new. Safe to run from cron every day.
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(git branch --show-current)" = main ] || { echo "$(date -Is) not on main, nothing done"; exit 1; }
git fetch --quiet origin main
[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ] || exit 0

git pull --ff-only --quiet origin main
docker compose up -d --build
docker image prune -f > /dev/null
echo "$(date -Is) updated to $(git rev-parse --short HEAD)"
