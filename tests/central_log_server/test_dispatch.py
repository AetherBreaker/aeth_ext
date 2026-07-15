"""Tests for `aeth_ext.central_log_server.server.dispatch`."""

# Standard library imports
import logging
from pathlib import Path
from typing import override

# Third party imports
import pytest
from aiologic import Queue

# First party imports
from aeth_ext.central_log_server.server.dispatch import (
  QueueForwardHandler,
  RegisterClient,
  UnregisterClient,
  WriterItem,
  build_hierarchy,
  shutdown_hierarchy,
)
from aeth_ext.logging.bases import TaggedLogRecord

_CONNECTION_ID = 7


class _RecordingHandler(logging.Handler):
  """Handler that records emitted records and counts ``close`` calls."""

  def __init__(self) -> None:
    super().__init__()
    self.records: list[logging.LogRecord] = []
    self.close_calls = 0

  @override
  def emit(self, record: logging.LogRecord) -> None:
    self.records.append(record)

  @override
  def close(self) -> None:
    self.close_calls += 1
    super().close()


class _ExplodingOnCloseHandler(_RecordingHandler):
  @override
  def close(self) -> None:
    super().close()
    raise RuntimeError("close failed")


def _make_record(name: str = "prog.module") -> TaggedLogRecord:
  return TaggedLogRecord(name, logging.INFO, __file__, 1, "hello", None, None)


class TestEventDataclasses:
  def test_register_client_is_frozen(self):
    root = logging.RootLogger(logging.WARNING)
    manager = logging.Manager(root)
    event = RegisterClient("prog", manager, root, _CONNECTION_ID)

    with pytest.raises(AttributeError):
      event.program_name = "other"  # pyright: ignore[reportAttributeAccessIssue]

  def test_unregister_client_fields(self):
    event = UnregisterClient("prog", _CONNECTION_ID)

    assert event.program_name == "prog"
    assert event.connection_id == _CONNECTION_ID


class TestQueueForwardHandler:
  def test_emit_forwards_record_onto_queue(self):
    queue: Queue[WriterItem] = Queue()
    handler = QueueForwardHandler(queue)
    record = _make_record()

    handler.emit(record)

    forwarded = queue.green_get(blocking=False)
    assert isinstance(forwarded, logging.LogRecord)
    assert forwarded.getMessage() == "hello"
    assert forwarded.name == "prog.module"


class TestBuildHierarchy:
  def test_returns_linked_manager_and_root(self, tmp_path: Path):
    manager, root = build_hierarchy({"version": 1, "root": {"level": "DEBUG"}}, tmp_path)

    try:
      assert root.manager is manager
      assert manager.root is root
      assert root.level == logging.DEBUG
      assert manager is not logging.Logger.manager
      assert root is not logging.root
    finally:
      shutdown_hierarchy(manager, root)

  def test_private_hierarchy_leaves_global_state_untouched(self, tmp_path: Path):
    config = {
      "version": 1,
      "handlers": {"dispatch_test_iso": {"class": "logging.NullHandler"}},
      "loggers": {"dispatch_test_iso.child": {"level": "ERROR"}},
      "root": {"level": "DEBUG", "handlers": ["dispatch_test_iso"]},
    }
    global_root_level = logging.root.level
    global_root_handlers = logging.root.handlers[:]

    manager, root = build_hierarchy(config, tmp_path)

    try:
      # The handler exists (with its configured name) only inside the hierarchy.
      assert logging.getHandlerByName("dispatch_test_iso") is None
      (handler,) = root.handlers
      assert handler.get_name() == "dispatch_test_iso"
      # The configured logger lives in the private manager, not the global one.
      assert manager.getLogger("dispatch_test_iso.child").level == logging.ERROR
      assert "dispatch_test_iso.child" not in logging.Logger.manager.loggerDict
      # The global root logger is unaffected.
      assert logging.root.level == global_root_level
      assert logging.root.handlers == global_root_handlers
    finally:
      shutdown_hierarchy(manager, root)

  def test_logdir_paths_rooted_under_log_dir(self, tmp_path: Path):
    config = {
      "version": 1,
      "handlers": {
        "file": {"class": "logging.FileHandler", "filename": "logdir://sub/app.log", "delay": True},
      },
      "root": {"level": "DEBUG", "handlers": ["file"]},
    }

    manager, root = build_hierarchy(config, tmp_path)

    try:
      (handler,) = root.handlers
      assert isinstance(handler, logging.FileHandler)
      assert Path(handler.baseFilename) == tmp_path / "sub" / "app.log"
      assert (tmp_path / "sub").is_dir()
    finally:
      shutdown_hierarchy(manager, root)

  def test_invalid_config_raises_value_error(self, tmp_path: Path):
    config = {
      "version": 1,
      "handlers": {"bad": {"class": "not.a.real.module.Handler"}},
      "root": {"handlers": ["bad"]},
    }

    with pytest.raises(ValueError, match="bad"):
      build_hierarchy(config, tmp_path)


class TestShutdownHierarchy:
  def test_removes_flushes_and_closes_handlers(self):
    root = logging.RootLogger(logging.WARNING)
    manager = logging.Manager(root)
    root.manager = manager
    handler = _RecordingHandler()
    root.addHandler(handler)
    child = manager.getLogger("prog.child")
    child.addHandler(handler)

    shutdown_hierarchy(manager, root)

    assert root.handlers == []
    assert child.handlers == []
    # Shared handler is closed exactly once despite being attached twice.
    assert handler.close_calls == 1

  def test_swallows_close_exceptions(self):
    root = logging.RootLogger(logging.WARNING)
    manager = logging.Manager(root)
    root.manager = manager
    exploding = _ExplodingOnCloseHandler()
    surviving = _RecordingHandler()
    root.addHandler(exploding)
    root.addHandler(surviving)

    shutdown_hierarchy(manager, root)

    assert exploding.close_calls == 1
    assert surviving.close_calls == 1
