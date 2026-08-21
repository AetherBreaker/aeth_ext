"""Shared autouse isolation fixtures for the whole test suite.

Every subpackage under `tests/` picks these up automatically (pytest applies
fixtures from parent-directory `conftest.py` files to all tests below them),
so no subpackage needs to redefine or import them.
"""

# Standard library imports
import logging
from time import monotonic, sleep
from typing import TYPE_CHECKING

# Third party imports
import pytest
from hypothesis import settings as hypothesis_settings
from hypothesis.database import DirectoryBasedExampleDatabase

# First party imports
from aeth_ext.errors.shutdown import SHUTDOWN
from aeth_ext.logging import config as dc
from aeth_ext.logging.bases import TaggedLogRecord
from aeth_ext.logging.config import runtime_registry

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Generator

hypothesis_settings.register_profile(
  "aeth_ext", database=DirectoryBasedExampleDatabase(".cache/hypothesis")
)
hypothesis_settings.load_profile("aeth_ext")


@pytest.fixture(autouse=True)
def _isolate_runtime_registry() -> Generator[None]:
  """Snapshot and restore the runtime object registry around each test."""
  saved = dict(runtime_registry._registry)  # pyright: ignore[reportPrivateUsage]

  yield

  runtime_registry._registry.clear()  # pyright: ignore[reportPrivateUsage]
  runtime_registry._registry.update(saved)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _isolate_logging_state() -> Generator[None]:
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


@pytest.fixture(autouse=True)
def _project_name_set(monkeypatch: pytest.MonkeyPatch) -> None:
  """Stand in for a consuming program's ``PROJECT_NAME`` entrypoint constant.

  `TaggedLogRecord._resolve_project_name()` resolves and caches PROJECT_NAME lazily, on first
  real record construction, by walking up from the caller to the process entrypoint looking for a
  `PROJECT_NAME` constant (see `parse_and_grab_constants`). Under pytest there is no such constant
  anywhere in the ancestry, so it would resolve to the `"FIX_ME"` sentinel and any real record
  creation would raise. Tests that actually emit log records need a stand-in value, same as a real
  consumer would provide one.
  """
  monkeypatch.setattr(TaggedLogRecord, "_project_name", "aeth_ext-tests")


@pytest.fixture(autouse=True)
def _clear_shutdown_state() -> Generator[None]:
  """Fail loudly if a test tripped the (one-shot) process-wide shutdown signal."""
  yield

  assert not SHUTDOWN.is_set(), "test requested a process-wide shutdown; SHUTDOWN is one-shot and cannot be reset"


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
  """Polls `predicate` until it holds or `timeout` elapses.

  Lets concurrency tests hand off between threads on an observable state change rather than a fixed
  sleep, so they stay deterministic without pinning a timing assumption to the machine they run on.

  Args:
    predicate: The condition to wait for.
    timeout: Seconds to poll before giving up.

  Returns:
    Whether the condition held before the deadline.
  """
  deadline = monotonic() + timeout
  while monotonic() < deadline:
    if predicate():
      return True
    sleep(0.005)
  return predicate()
