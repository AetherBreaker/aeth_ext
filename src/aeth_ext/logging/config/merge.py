# Standard library imports
from copy import deepcopy
from functools import reduce
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from typing import Any

__all__ = ["MERGE_MARKER", "assemble_configs", "merge_configs", "strip_merge_markers"]

# Marker key an override entry may carry to request a deep (field-level) merge
# into the same-named base entry instead of wholesale replacement.
MERGE_MARKER = "__merge__"

# Top-level sections whose values are name-keyed maps of entries.
_SECTION_KEYS = ("formatters", "filters", "handlers", "loggers")


def strip_merge_markers(config: Mapping[str, Any]) -> dict[str, Any]:
  """Return a deep copy of *config* with all `MERGE_MARKER` keys removed recursively."""
  result: dict[str, Any] = {}
  for key, value in config.items():
    if key == MERGE_MARKER:
      continue
    result[key] = strip_merge_markers(value) if isinstance(value, dict) else deepcopy(value)
  return result


def _deep_merge(base: Any, override: Any) -> Any:
  """
  Recursively merge *override* into a copy of *base*.

  Dicts merge per key; lists concatenate (base items first, then override
  items not already present); any other combination is replaced by the
  override value.
  """
  if isinstance(base, dict) and isinstance(override, dict):
    merged = {k: deepcopy(v) for k, v in base.items()}
    for key, value in override.items():
      if key == MERGE_MARKER:
        continue
      if key in merged:
        merged[key] = _deep_merge(merged[key], value)
      else:
        merged[key] = strip_merge_markers(value) if isinstance(value, dict) else deepcopy(value)
    return merged
  if isinstance(base, list) and isinstance(override, list):
    return deepcopy(base) + [deepcopy(item) for item in override if item not in base]
  return strip_merge_markers(override) if isinstance(override, dict) else deepcopy(override)


def _wants_deep_merge(entry: Any) -> bool:
  """Return whether *entry* is a dict carrying an active deep-merge marker."""
  return isinstance(entry, dict) and entry.get(MERGE_MARKER) == "deep"


def merge_configs(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
  """
  Merge *override* onto *base* using named-entry semantics and return the result.

  - Top-level scalar keys (``version``, ``incremental``, ...) are replaced.
  - Within the name-keyed sections (``formatters``, ``filters``, ``handlers``,
    ``loggers``) and for ``root``, a same-named override entry replaces the
    base entry wholesale, unless the override entry carries
    ``MERGE_MARKER = "deep"``, in which case it is recursively field-merged
    into the base entry.
  - All merge markers are stripped from the result.
  """
  result = strip_merge_markers(base)

  for key, value in override.items():
    if key in _SECTION_KEYS and isinstance(value, dict):
      section = result.setdefault(key, {})
      for entry_name, entry_value in value.items():
        if _wants_deep_merge(entry_value) and entry_name in section:
          section[entry_name] = _deep_merge(section[entry_name], entry_value)
        else:
          section[entry_name] = strip_merge_markers(entry_value) if isinstance(entry_value, dict) else deepcopy(entry_value)
    elif key == "root" and isinstance(value, dict):
      if _wants_deep_merge(value) and "root" in result:
        result["root"] = _deep_merge(result["root"], value)
      else:
        result["root"] = strip_merge_markers(value)
    else:
      result[key] = deepcopy(value)

  return result


def assemble_configs(*configs: Mapping[str, Any]) -> dict[str, Any]:
  """Merge *configs* left-to-right into a single config dict (later configs win)."""
  if not configs:
    raise ValueError("assemble_configs requires at least one config")
  return reduce(merge_configs, configs, {})
