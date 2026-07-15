# Standard library imports
import sys
import tomllib
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

# Local folder imports
from .merge import assemble_configs, merge_configs, strip_merge_markers

if TYPE_CHECKING:
  # Standard library imports
  from typing import Any, Literal

__all__ = [
  "DEFAULT_OVERRIDE_FILENAME",
  "assemble_default_config",
  "find_override_config",
  "load_effective_config",
  "load_packaged_fragment",
  "pre_resolve",
]

DEFAULT_OVERRIDE_FILENAME = "logging_config.toml"

_DEFAULTS_PACKAGE = "aeth_ext.logging.config.defaults"


def load_packaged_fragment(name: str) -> dict[str, Any]:
  """Load the packaged default TOML fragment *name* (without extension) as a dict."""
  resource = files(_DEFAULTS_PACKAGE) / f"{name}.toml"
  try:
    text = resource.read_text(encoding="utf-8")
  except FileNotFoundError:
    raise ValueError(f"No packaged logging-config fragment named {name!r}") from None
  return tomllib.loads(text)


def assemble_default_config(*fragment_names: str) -> dict[str, Any]:
  """Load and merge the named packaged fragments left-to-right into one config dict."""
  return assemble_configs(*(load_packaged_fragment(name) for name in fragment_names))


def find_override_config(filename: str = DEFAULT_OVERRIDE_FILENAME) -> Path | None:
  """
  Locate a project logging-config override file.

  Search order (first hit wins):

  1. ``BaseSettings.logging_config_loc`` - either the file itself, or a
     directory expected to contain *filename*.
  2. The directory of the running program's ``__main__`` module.
  3. The current working directory.
  """
  # First party imports
  from aeth_ext.settings import BaseSettings

  settings_loc = BaseSettings.get_settings().logging_config_loc
  if settings_loc is not None:
    if settings_loc.is_file():
      return settings_loc
    candidate = settings_loc / filename
    if candidate.is_file():
      return candidate

  main_file = getattr(sys.modules.get("__main__"), "__file__", None)
  if main_file is not None:
    candidate = Path(main_file).resolve().parent / filename
    if candidate.is_file():
      return candidate

  candidate = Path.cwd() / filename
  if candidate.is_file():
    return candidate

  return None


def load_effective_config(
  fragment_names: list[str] | tuple[str, ...],
  *,
  override_mode: Literal["replace", "merge"] = "replace",
  override_path: Path | None = None,
  override_filename: str = DEFAULT_OVERRIDE_FILENAME,
) -> dict[str, Any]:
  """
  Assemble the packaged default fragments and apply any project override.

  With ``override_mode="replace"`` (the default) a discovered override file
  fully replaces the assembled default. With ``"merge"`` it is merged onto the
  default using named-entry semantics (see `merge_configs`). *override_filename*
  controls which file name `find_override_config` searches for when no explicit
  *override_path* is given.
  """
  default = assemble_default_config(*fragment_names)

  path = override_path if override_path is not None else find_override_config(override_filename)
  if path is None:
    return default

  override = tomllib.loads(path.read_text(encoding="utf-8"))
  if override_mode == "replace":
    return strip_merge_markers(override)
  return merge_configs(default, override)


# Value prefixes that must be resolved on the machine that authored the config
# (they reference this process's runtime registry, settings, or environment).
# Everything else - notably logdir://, cfg://, and ext:// - is left for the
# configurator that ultimately applies the config (e.g. the log server).
_CLIENT_SIDE_CONVERTERS = {
  "runtime": "runtime_convert",
  "setting": "setting_convert",
  "env": "env_convert",
}


def _pre_resolve_value(value: Any, configurator: Any) -> Any:
  if isinstance(value, str):
    match = configurator.CONVERT_PATTERN.match(value)
    if match:
      converter_name = _CLIENT_SIDE_CONVERTERS.get(match["prefix"])
      if converter_name is not None:
        return getattr(configurator, converter_name)(match["suffix"])
    return value
  if isinstance(value, Mapping):
    return {key: _pre_resolve_value(item, configurator) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_pre_resolve_value(item, configurator) for item in value]
  return value


def pre_resolve(config: Mapping[str, Any]) -> dict[str, Any]:
  """Resolve the client-side value prefixes throughout *config*, returning a copy.

  Used before shipping a config to the central log server: ``runtime://``,
  ``setting://``, and ``env://`` values reference state that only exists in
  *this* process, so they are materialised here (a ``ValueError`` from a
  converter propagates - a config that cannot be resolved locally should fail
  fast rather than be shipped broken). Server-side prefixes (``logdir://``,
  ``cfg://``, ``ext://``) and ``definition`` payloads are passed through
  untouched for the server's configurator. Values are resolved exactly once -
  a resolved value is not scanned for further prefixes.
  """
  # Local folder imports
  from . import BaseConfigurator

  configurator = BaseConfigurator({})
  resolved = _pre_resolve_value(dict(config), configurator)
  assert isinstance(resolved, dict)
  return resolved
