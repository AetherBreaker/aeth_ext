import tomllib

with open("/app/pyproject.toml", "rb") as f:
    data = tomllib.load(f)

optional_deps = data.get("project", {}).get("optional-dependencies", {})
if "app" in optional_deps:
    print("--extra app", end="")
