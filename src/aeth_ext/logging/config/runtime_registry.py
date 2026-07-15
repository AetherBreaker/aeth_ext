# Standard library imports
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator
  from typing import Any

__all__ = ["clear", "register", "registered_names", "resolve", "temporarily_registered", "unregister"]

# Well-known names registered by aeth_ext.logging.setup and referenced by the
# packaged default TOML fragments. Projects may register additional names for
# use in their own override configs.
_registry: dict[str, Any] = {}


def register(name: str, obj: Any) -> None:
  """Register *obj* under *name* for resolution via ``runtime://name`` in logging configs."""
  _registry[name] = obj


def resolve(name: str) -> Any:
  """Return the object registered under *name*, raising `ValueError` if absent."""
  try:
    return _registry[name]
  except KeyError:
    raise ValueError(
      f"No runtime object registered under {name!r}. "
      "Register it with aeth_ext.logging.config.runtime_registry.register() before applying the config."
    ) from None


def unregister(name: str) -> None:
  """Remove *name* from the registry if present."""
  _registry.pop(name, None)


def clear() -> None:
  """Remove all registered objects."""
  _registry.clear()


def registered_names() -> frozenset[str]:
  """Return a snapshot of all currently registered names."""
  return frozenset(_registry)


@contextmanager
def temporarily_registered(**objects: Any) -> Generator[None]:
  """Context manager registering *objects* on entry and restoring prior state on exit."""
  saved = {name: _registry[name] for name in objects if name in _registry}
  _registry.update(objects)
  try:
    yield
  finally:
    for name in objects:
      _registry.pop(name, None)
    _registry.update(saved)
