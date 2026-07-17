#!/bin/sh
set -e

launch_cmd=$(uv run --no-project python /app/scripts/get_launch_script.py)

if [ "$(id -u)" = "0" ]; then
  # Running as root: chown volume paths then drop privileges before exec.
  uv run --no-project python /app/scripts/get_chown_paths.py | while IFS= read -r rel_path; do
    [ -n "$rel_path" ] || continue
    target="/app/${rel_path}"
    mkdir -p "$target"
    chown -R nonroot:nonroot "$target"
  done
  exec gosu nonroot "$launch_cmd"
else
  echo "error: entrypoint must run as root (uid 0); got uid $(id -u)" >&2
  exit 1
fi
