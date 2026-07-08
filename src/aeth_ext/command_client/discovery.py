# Standard library imports
import sys
from dataclasses import dataclass
from logging import getLogger
from typing import Any

# Third party imports
from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector
from orjson import dumps

# First party imports
from aeth_ext.command_server.local_registry import read_registry

logger = getLogger(__name__)

__all__ = ["ServerEndpoint", "discover"]

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_API = "http://localhost/v1.41"

ENABLED_LABEL = "aeth.command_server.enabled"
PORT_LABEL = "aeth.command_server.port"
NAME_LABEL = "aeth.command_server.name"


@dataclass(frozen=True, slots=True)
class ServerEndpoint:
  """A discovered command server: where it lives and what it calls itself."""

  name: str
  host: str
  port: int


async def _discover_docker() -> list[ServerEndpoint]:
  """Query the Docker daemon for containers labelled as command servers.

  Raises on any failure (missing socket, daemon down, bad response) so the
  caller can fall back to the local registry.
  """
  filters = dumps({"label": [f"{ENABLED_LABEL}=true"]}).decode()
  async with (
    ClientSession(connector=UnixConnector(path=DOCKER_SOCKET), timeout=ClientTimeout(total=5)) as session,
    session.get(f"{DOCKER_API}/containers/json", params={"filters": filters}) as resp,
  ):
    resp.raise_for_status()
    containers = await resp.json()

  endpoints: list[ServerEndpoint] = []
  for container in containers:
    labels: dict[str, str] = container.get("Labels", {})
    port_raw = labels.get(PORT_LABEL)
    if port_raw is None:
      logger.warning("Container %s has %s but no %s label; skipping", container.get("Id", "?")[:12], ENABLED_LABEL, PORT_LABEL)
      continue

    # Prefer the explicit name label, then the container name.
    name = labels.get(NAME_LABEL) or container["Names"][0].lstrip("/")

    # Host: first network alias if available, else the container name.
    host = name
    networks: dict[str, dict[str, Any]] = container.get("NetworkSettings", {}).get("Networks", {})
    for net in networks.values():
      aliases = net.get("Aliases") or []
      if aliases:
        host = aliases[0]
        break

    endpoints.append(ServerEndpoint(name=name, host=host, port=int(port_raw)))
  return endpoints


def _discover_local() -> list[ServerEndpoint]:
  """Read the local registry file written by non-Docker command servers."""
  return [ServerEndpoint(name=name, host=entry["host"], port=entry["port"]) for name, entry in read_registry().items()]


async def discover() -> list[ServerEndpoint]:
  """Discover every reachable command server.

  Tries Docker label-based discovery first (production/Coolify); if the
  Docker socket is unavailable, falls back to the local registry file that
  servers write when running outside Docker (development).
  """
  if sys.platform != "win32":
    try:
      return await _discover_docker()
    except OSError, ClientError, KeyError, ValueError:
      logger.debug("Docker discovery unavailable; falling back to local registry", exc_info=True)
  return _discover_local()
