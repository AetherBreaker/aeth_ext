#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../.."

# Pull first to sync incoming changes
echo "Pulling latest changes..."
git pull

# Parse new version from pyproject.toml
new_version=$(grep -m1 'version' pyproject.toml | sed 's/.*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/')
if [ -z "$new_version" ]; then
    echo "Error: Could not parse version from pyproject.toml" >&2
    exit 1
fi
echo "New version: $new_version"

# Stage version bump and commit
git add pyproject.toml
git add uv.lock
git commit -m "Bump version to $new_version"

# Create an annotated tag for the new version
git tag -a "v$new_version" -m "Version $new_version"

# Push commits and all reachable tags together
echo "Pushing commits and tags..."
git push --follow-tags

echo "Done! Version bumped to $new_version and pushed."
