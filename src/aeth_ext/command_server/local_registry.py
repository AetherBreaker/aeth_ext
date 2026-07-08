# Standard library imports
from logging import getLogger
from pathlib import Path
from typing import TypedDict

# Third party imports
from orjson import OPT_INDENT_2, dumps, loads

# First party imports
from aeth_ext.settings import BaseSettings

logger = getLogger(__name__)

__all__ = ["read_registry", "register", "registry_path", "unregister"]


class RegistryEntry(TypedDict):
  host: str
  port: int


def registry_path() -> Path:
  """Location of the local (non-Docker) command server registry file."""
  settings = BaseSettings.get_settings()
  return settings.persisted_dir_loc / "command_server_registry.json"


def read_registry() -> dict[str, RegistryEntry]:
  """Read the registry file, returning an empty mapping if it doesn't exist or is corrupt."""
  path = registry_path()
  try:
    return loads(path.read_bytes())
  except FileNotFoundError:
    return {}
  except ValueError:
    logger.warning("Corrupt command server registry at %s; treating as empty", path)
    return {}


def _write_registry(entries: dict[str, RegistryEntry]) -> None:
  """Atomically replace the registry file with ``entries``."""
  path = registry_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(".json.tmp")
  tmp.write_bytes(dumps(entries, option=OPT_INDENT_2))
  tmp.replace(path)


def register(name: str, host: str, port: int) -> None:
  """Upsert this program's endpoint into the local registry."""
  entries = read_registry()
  entries[name] = RegistryEntry(host=host, port=port)
  _write_registry(entries)
  logger.debug("Registered command server %r at %s:%d", name, host, port)


def unregister(name: str) -> None:
  """Remove this program's endpoint from the local registry, if present."""
  entries = read_registry()
  if entries.pop(name, None) is not None:
    _write_registry(entries)
    logger.debug("Unregistered command server %r", name)
