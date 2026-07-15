"""Shared pytest fixtures for the `dict_config` test suite."""

# Standard library imports
import logging

# Third party imports
import pytest

# First party imports
from aeth_ext.logging import config as dc
from aeth_ext.logging.config import runtime_registry


@pytest.fixture(autouse=True)
def _isolate_runtime_registry():
  """Snapshot and restore the runtime object registry around each test."""
  saved = dict(runtime_registry._registry)  # pyright: ignore[reportPrivateUsage]

  yield

  runtime_registry._registry.clear()  # pyright: ignore[reportPrivateUsage]
  runtime_registry._registry.update(saved)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _isolate_logging_state():
  """Snapshot and restore global logging state around each test."""
  root = logging.root
  old_handlers = root.handlers[:]
  old_level = root.level
  old_logger_names = set(root.manager.loggerDict.keys())
  old_module_handlers = dict(dc._logging_handlers)  # pyright: ignore[reportPrivateUsage]
  old_module_handler_list = dc._logging_handler_list[:]  # pyright: ignore[reportPrivateUsage]

  yield

  for h in root.handlers[:]:
    root.removeHandler(h)
  for h in old_handlers:
    root.addHandler(h)
  root.setLevel(old_level)

  new_logger_names = set(root.manager.loggerDict.keys()) - old_logger_names
  for name in new_logger_names:
    del root.manager.loggerDict[name]

  dc._logging_handlers.clear()  # pyright: ignore[reportPrivateUsage]
  dc._logging_handlers.update(old_module_handlers)  # pyright: ignore[reportPrivateUsage]
  dc._logging_handler_list[:] = old_module_handler_list  # pyright: ignore[reportPrivateUsage]
