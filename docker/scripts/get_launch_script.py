import sys
import tomllib

with open("/app/pyproject.toml", "rb") as f:
    data = tomllib.load(f)

scripts = data.get("project", {}).get("scripts", {})
matches = [name for name in scripts if name.startswith("run-app-")]

if len(matches) == 0:
    available = ", ".join(scripts) if scripts else "(none)"
    print(
        f"error: no [project.scripts] entry with a 'run-app-' prefix found"
        f" (available scripts: {available})",
        file=sys.stderr,
    )
    sys.exit(1)

if len(matches) > 1:
    names = ", ".join(matches)
    print(f"error: multiple 'run-app-' scripts found ({names}); define exactly one", file=sys.stderr)
    sys.exit(1)

print(matches[0])
