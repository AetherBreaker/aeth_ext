"""Print the project readme path from pyproject, without a trailing newline."""

# Standard library imports
import tomllib

with open("/app/pyproject.toml", "rb") as f:
  data = tomllib.load(f)

print(data.get("project", {}).get("readme", ""), end="")
