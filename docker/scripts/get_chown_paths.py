import tomllib

with open("/app/pyproject.toml", "rb") as f:
    data = tomllib.load(f)

for path in data.get("tool", {}).get("docker", {}).get("chown_paths", []):
    print(path)
