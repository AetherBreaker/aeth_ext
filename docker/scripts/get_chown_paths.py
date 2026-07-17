import tomllib

with open("/app/pyproject.toml", "rb") as f:
    data = tomllib.load(f)

docker = data.get("tool", {}).get("docker", {})
seen: set[str] = set()
for path in (*docker.get("chown_paths", []), *docker.get("mkdirs", [])):
    if path not in seen:
        seen.add(path)
        print(path)
