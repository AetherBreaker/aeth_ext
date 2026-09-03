"""Print `--extra app` when pyproject declares an `app` optional-dependency group, for the image's `uv sync`."""

# Standard library imports
import tomllib

with open("/app/pyproject.toml", "rb") as f:
    data = tomllib.load(f)

optional_deps = data.get("project", {}).get("optional-dependencies", {})
if "app" in optional_deps:
    print("--extra app", end="")
