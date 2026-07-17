#!/bin/sh
set -e

# Read chown_paths from [tool.docker] in /app/pyproject.toml and ensure each
# directory exists and is owned by the nonroot user before handing off.
uv run --no-project python /app/scripts/get_chown_paths.py | while IFS= read -r rel_path; do
    [ -n "$rel_path" ] || continue
    target="/app/${rel_path}"
    mkdir -p "$target"
    chown -R nonroot:nonroot "$target"
done

# Detect the launch script from [project.scripts] by finding the entry
# prefixed with 'run-app-', then drop privileges and exec it.
launch_cmd=$(uv run --no-project python /app/scripts/get_launch_script.py)
exec gosu nonroot "$launch_cmd"
