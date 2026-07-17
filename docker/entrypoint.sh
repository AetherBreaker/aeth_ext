#!/bin/sh
set -e

launch_cmd=$(uv run --no-project python /app/scripts/get_launch_script.py)

if [ "$(id -u)" = "0" ]; then
  # Create directories listed in [tool.docker] mkdirs.
  uv run --no-project python /app/scripts/get_mkdirs.py | while IFS= read -r rel_path; do
    [ -n "$rel_path" ] || continue
    mkdir -p "/app/${rel_path}"
  done

  # Chown all paths (chown_paths + mkdirs) to nonroot:nonroot.
  uv run --no-project python /app/scripts/get_chown_paths.py | while IFS= read -r rel_path; do
    [ -n "$rel_path" ] || continue
    chown -R nonroot:nonroot "/app/${rel_path}"
  done

  exec gosu nonroot "$launch_cmd"
else
  echo "error: entrypoint must run as root (uid 0); got uid $(id -u)" >&2
  exit 1
fi
