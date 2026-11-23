#!/usr/bin/env sh
# Walking-skeleton check for #72. See scripts/hello_world.py for what it does.
# Run from the repository root with `docker compose up -d --build` running.
set -eu
cd "$(dirname "$0")/.."
exec python3 scripts/hello_world.py "$@"
