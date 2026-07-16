"""Tests for `aeth_ext.central_log_server.server.writer_thread`.

The `LogWriterThread` is exercised without ever starting the thread: its
registration/teardown/dispatch methods are driven directly (via
``asyncio.run`` for the coroutines), which is exactly how the internal
record loop drives them.
"""

# Standard library imports
import asyncio
import logging
from typing import TYPE_CHECKING, override

# Third party imports
import pytest
from aiologic import Queue

# First party imports
from aeth_ext.central_log_server._types import RegisterClient, UnregisterClient, WriterItem
from aeth_ext.central_log_server.server import writer_thread as wt_mod
from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
from aeth_ext.central_log_server.server.writer_thread import LogWriterThread
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

_CONNECTION_A = 1
_CONNECTION_B = 2
_RECORD_ID = 5
# Mirrors writer_thread._SERVER_CONNECTION_ID.
_SERVER_CONNECTION_ID = -1


@pytest.fixture(autouse=True)
def _writer_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
  """Point every disk location the writer touches at the test's tmp dir."""
  shared = tmp_path / "shared"
  monkeypatch.setattr(wt_mod, "_SHARED_LOG_DIR", shared)
  monkeypatch.setattr(wt_mod, "_MIDNIGHT_BASELINE_PATH", shared / "midnight_baseline.json")
  monkeypatch.setattr(wt_mod.settings, "log_loc_folder", tmp_path / "logs")


class _RecordingHandler(logging.Handler):
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


def _make_hierarchy() -> tuple[logging.Manager, logging.Logger, _RecordingHandler]:
  """Build a minimal private hierarchy with a recording handler on its root."""
  root = logging.RootLogger(logging.DEBUG)
  manager = logging.Manager(root)
  root.manager = manager
  handler = _RecordingHandler()
  root.addHandler(handler)
  return manager, root, handler


def _make_writer(server_config: dict[str, object] | None = None) -> LogWriterThread:
  queue: Queue[WriterItem] = Queue()
  return LogWriterThread(queue, ClientIdRegistry(), server_config=server_config)


def _make_record(name: str = "prog.module", source_name: str | None = "prog") -> TaggedLogRecord:
  record = TaggedLogRecord(name, logging.INFO, __file__, 1, "hello", None, None)
  record.source_name = source_name
  return record


class TestServerPseudoClient:
  def test_server_config_builds_pseudo_hierarchy(self):
    writer = _make_writer(server_config={"version": 1, "root": {"level": "DEBUG"}})

    hierarchies = writer._hierarchies  # pyright: ignore[reportPrivateUsage]
    assert None in hierarchies
    entry = hierarchies[None]
    assert entry.connection_id == _SERVER_CONNECTION_ID
    assert entry.root.manager is entry.manager
    # The pseudo-client is not reported as a connected program.
    assert writer._live_snapshot["connected_programs"] == []  # pyright: ignore[reportPrivateUsage]

  def test_no_server_config_means_no_pseudo_hierarchy(self):
    writer = _make_writer()

    assert None not in writer._hierarchies  # pyright: ignore[reportPrivateUsage]

  def test_server_records_dispatch_into_pseudo_hierarchy(self):
    writer = _make_writer(server_config={"version": 1, "root": {"level": "DEBUG"}})
    handler = _RecordingHandler()
    writer._hierarchies[None].root.addHandler(handler)  # pyright: ignore[reportPrivateUsage]
    record = _make_record(name="aeth_ext.some.module", source_name=None)

    asyncio.run(writer._dispatch(record))  # pyright: ignore[reportPrivateUsage]

    assert [r.getMessage() for r in handler.records] == ["hello"]


class TestRegistration:
  def test_register_adopts_hierarchy_and_updates_snapshot(self):
    writer = _make_writer()
    manager, root, _handler = _make_hierarchy()

    asyncio.run(writer._process(RegisterClient("prog", manager, root, _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]

    entry = writer._hierarchies["prog"]  # pyright: ignore[reportPrivateUsage]
    assert entry.manager is manager
    assert entry.root is root
    assert entry.connection_id == _CONNECTION_A
    assert writer._live_snapshot["connected_programs"] == ["prog"]  # pyright: ignore[reportPrivateUsage]

  def test_reregister_replaces_and_closes_stale_hierarchy(self):
    writer = _make_writer()
    _m1, _r1, stale_handler = _make_hierarchy()
    m2, r2, _fresh_handler = _make_hierarchy()

    asyncio.run(writer._process(RegisterClient("prog", _m1, _r1, _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]
    asyncio.run(writer._process(RegisterClient("prog", m2, r2, _CONNECTION_B)))  # pyright: ignore[reportPrivateUsage]

    entry = writer._hierarchies["prog"]  # pyright: ignore[reportPrivateUsage]
    assert entry.connection_id == _CONNECTION_B
    assert stale_handler.close_calls == 1

  def test_unregister_with_matching_connection_tears_down(self):
    writer = _make_writer()
    manager, root, handler = _make_hierarchy()
    asyncio.run(writer._process(RegisterClient("prog", manager, root, _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]

    asyncio.run(writer._process(UnregisterClient("prog", _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]

    assert "prog" not in writer._hierarchies  # pyright: ignore[reportPrivateUsage]
    assert handler.close_calls == 1
    assert writer._live_snapshot["connected_programs"] == []  # pyright: ignore[reportPrivateUsage]

  def test_unregister_with_stale_connection_is_ignored(self):
    """A reconnect's fresh hierarchy must survive the old connection's unregister."""
    writer = _make_writer()
    manager, root, handler = _make_hierarchy()
    asyncio.run(writer._process(RegisterClient("prog", manager, root, _CONNECTION_B)))  # pyright: ignore[reportPrivateUsage]

    asyncio.run(writer._process(UnregisterClient("prog", _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]

    assert writer._hierarchies["prog"].connection_id == _CONNECTION_B  # pyright: ignore[reportPrivateUsage]
    assert handler.close_calls == 0


class TestDispatch:
  def test_routes_named_records_via_private_manager(self):
    writer = _make_writer()
    manager, root, handler = _make_hierarchy()
    asyncio.run(writer._process(RegisterClient("prog", manager, root, _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]
    record = _make_record(name="prog.module")

    asyncio.run(writer._dispatch(record))  # pyright: ignore[reportPrivateUsage]

    # The record propagated up to the root handler via the private manager.
    assert [r.getMessage() for r in handler.records] == ["hello"]
    assert "prog.module" in manager.loggerDict
    assert "prog.module" not in logging.Logger.manager.loggerDict

  def test_routes_root_records_to_hierarchy_root(self):
    writer = _make_writer()
    manager, root, handler = _make_hierarchy()
    asyncio.run(writer._process(RegisterClient("prog", manager, root, _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]
    record = _make_record(name="root")

    asyncio.run(writer._dispatch(record))  # pyright: ignore[reportPrivateUsage]

    assert [r.getMessage() for r in handler.records] == ["hello"]
    assert "root" not in manager.loggerDict

  def test_unknown_source_warns_once_and_drops(self, caplog: pytest.LogCaptureFixture):
    writer = _make_writer()

    with caplog.at_level(logging.WARNING, logger=wt_mod.__name__):
      asyncio.run(writer._dispatch(_make_record(source_name="ghost")))  # pyright: ignore[reportPrivateUsage]
      asyncio.run(writer._dispatch(_make_record(source_name="ghost")))  # pyright: ignore[reportPrivateUsage]

    warnings = [r for r in caplog.records if "no logging hierarchy" in r.getMessage()]
    assert len(warnings) == 1

  def test_register_resets_unknown_source_warning(self, caplog: pytest.LogCaptureFixture):
    writer = _make_writer()
    asyncio.run(writer._dispatch(_make_record(source_name="prog")))  # pyright: ignore[reportPrivateUsage]
    manager, root, _handler = _make_hierarchy()
    asyncio.run(writer._process(RegisterClient("prog", manager, root, _CONNECTION_A)))  # pyright: ignore[reportPrivateUsage]

    assert "prog" not in writer._warned_unknown_sources  # pyright: ignore[reportPrivateUsage]


class TestIdRegistryAdvancement:
  def test_record_with_resume_metadata_advances_registry(self):
    writer = _make_writer()
    manager, root, _handler = _make_hierarchy()

    async def scenario() -> None:
      await writer._process(RegisterClient("prog", manager, root, _CONNECTION_A))  # pyright: ignore[reportPrivateUsage]
      record = _make_record()
      record.record_id = _RECORD_ID
      await writer._dispatch(record)  # pyright: ignore[reportPrivateUsage]
      state = await writer._id_registry.get("prog")  # pyright: ignore[reportPrivateUsage]
      assert state is not None
      assert state.last_record_id == _RECORD_ID

    asyncio.run(scenario())

    snapshot = writer._live_snapshot  # pyright: ignore[reportPrivateUsage]
    assert snapshot["current_ids"] == {"prog": _RECORD_ID}
    # The first update of the day also persists a midnight baseline file.
    assert wt_mod._MIDNIGHT_BASELINE_PATH.exists()  # pyright: ignore[reportPrivateUsage]

  def test_record_without_record_id_leaves_registry_alone(self):
    writer = _make_writer()
    manager, root, _handler = _make_hierarchy()

    async def scenario() -> None:
      await writer._process(RegisterClient("prog", manager, root, _CONNECTION_A))  # pyright: ignore[reportPrivateUsage]
      await writer._dispatch(_make_record())  # pyright: ignore[reportPrivateUsage]
      assert await writer._id_registry.get("prog") is None  # pyright: ignore[reportPrivateUsage]

    asyncio.run(scenario())

    assert writer._live_snapshot["current_ids"] == {}  # pyright: ignore[reportPrivateUsage]
