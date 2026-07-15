# Standard library imports
import sys
from collections.abc import Mapping
from logging import Filter, getLevelNamesMapping, getLogger
from typing import TYPE_CHECKING, Any, Final, override

logger = getLogger(__name__)

# First party imports

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.logging.bases import TaggedLogRecord


class NotFilter(Filter):
  @override
  def filter(self, record: TaggedLogRecord) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
    if self.nlen == 0:
      return True
    elif self.name == record.name:
      return False
    elif record.name.find(self.name, 0, self.nlen) != 0:
      return True
    return record.name[self.nlen] != "."


# Threshold meaning "no remote handler can ever receive this logger's records".
UNREACHABLE: Final[int] = sys.maxsize


def _levelno(value: Any) -> int | None:
  """Best-effort conversion of a config level value to a numeric level.

  Returns ``None`` when the value is absent or cannot be interpreted (e.g. an
  unresolved converter string), so callers can fall back to the *most
  permissive* interpretation - a record must never be dropped client-side
  unless the config proves it undeliverable.
  """
  if isinstance(value, bool) or value is None:
    return None
  if isinstance(value, int):
    return value
  if isinstance(value, str):
    return getLevelNamesMapping().get(value.upper())
  return None


def _handler_min_levels(config: Mapping[str, Any]) -> dict[str, int]:
  """Map each configured handler name to its minimum accepted level (0 if unknown)."""
  handlers_cfg = config.get("handlers")
  if not isinstance(handlers_cfg, Mapping):
    return {}
  levels: dict[str, int] = {}
  for name, entry in handlers_cfg.items():
    level = _levelno(entry.get("level")) if isinstance(entry, Mapping) else None
    levels[str(name)] = level if level is not None else 0
  return levels


def _node_chain(name: str, nodes: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
  """Configured entries for *name* and each of its dotted ancestors, nearest first.

  The root entry (keyed ``""``) is always last unless an intermediate node
  sets ``propagate = false``, exactly mirroring how ``Logger.callHandlers``
  climbs the hierarchy.
  """
  chain: list[Mapping[str, Any]] = []
  key = name
  while key:
    entry = nodes.get(key)
    if entry is not None:
      chain.append(entry)
      if entry.get("propagate") is False:
        return chain
    key = key.rpartition(".")[0]
  root_entry = nodes.get("")
  if root_entry is not None:
    chain.append(root_entry)
  return chain


def _effective_level(name: str, nodes: Mapping[str, Mapping[str, Any]]) -> int:
  """The effective level a record must meet at *name*, mirroring `Logger.getEffectiveLevel`.

  Unknown/unparseable values resolve to 0 (most permissive). When no logger in
  the chain sets a level, the private hierarchy's root default (``WARNING``,
  i.e. 30) applies.
  """
  key = name
  while True:
    entry = nodes.get(key)
    if entry is not None and "level" in entry:
      level = _levelno(entry.get("level"))
      return level if level is not None else 0
    if not key:
      return 30  # logging.WARNING: default level of a fresh RootLogger
    key = key.rpartition(".")[0]


def _threshold(name: str, nodes: Mapping[str, Mapping[str, Any]], handler_levels: Mapping[str, int]) -> int:
  """Minimum ``levelno`` a record logged at *name* needs to reach any remote handler."""
  min_handler: int | None = None
  for entry in _node_chain(name, nodes):
    handlers = entry.get("handlers")
    if handlers is None:
      continue
    if not isinstance(handlers, (list, tuple)):
      # Unresolved converter string or unexpected shape: assume it could
      # resolve to a catch-all handler and never drop.
      min_handler = 0
      continue
    for handler_name in handlers:
      level = handler_levels.get(str(handler_name), 0)
      if min_handler is None or level < min_handler:
        min_handler = level
  if min_handler is None:
    return UNREACHABLE
  return max(_effective_level(name, nodes), min_handler)


class RemoteReachability:
  """Per-logger delivery thresholds computed from a remote logging config.

  Used by the client's socket handler to skip sending records the server's
  config *provably* cannot deliver anywhere. Only logger/handler **levels**
  and ``propagate`` flags are considered: filters can only drop records that
  already passed the level checks, so level math alone gives a sound
  "never deliverable" criterion - anything uncertain (unresolved converter
  strings, unknown level names, odd shapes) resolves to "send it".
  """

  def __init__(self, config: Mapping[str, Any]) -> None:
    self._thresholds: dict[str, int] = {"": 0}
    self._cache: dict[str, int] = {}
    try:
      nodes: dict[str, Mapping[str, Any]] = {}
      loggers_cfg = config.get("loggers")
      if isinstance(loggers_cfg, Mapping):
        for name, entry in loggers_cfg.items():
          nodes[str(name)] = entry if isinstance(entry, Mapping) else {}
      root_cfg = config.get("root")
      nodes[""] = root_cfg if isinstance(root_cfg, Mapping) else {}
      handler_levels = _handler_min_levels(config)
      self._thresholds = {name: _threshold(name, nodes, handler_levels) for name in nodes}
    except Exception:
      # A config this code fails to analyse must never cause drops.
      logger.exception("Failed to analyse remote logging config; disabling client-side pre-filtering")
      self._thresholds = {"": 0}

  def threshold_for(self, name: str) -> int:
    """The minimum ``levelno`` a record logged at *name* needs to be deliverable remotely."""
    cached = self._cache.get(name)
    if cached is not None:
      return cached
    key = name if name != "root" else ""
    while key and key not in self._thresholds:
      key = key.rpartition(".")[0]
    result = self._thresholds.get(key, 0)
    self._cache[name] = result
    return result
