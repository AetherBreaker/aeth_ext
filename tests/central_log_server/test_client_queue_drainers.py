"""Tests for `aeth_ext.central_log_server.client.AsyncioQueueDrainer`/`ThreadedQueueDrainer`.

Both drainers are exercised against a real `LogRecordServer` (the same server
class production log clients connect to) rather than a hand-rolled fake
protocol server, so the tests cover the genuine wire handshake/ack/record
framing. `query_logging_configs` (which normally spawns a subprocess -- see
`config_provider.py`) is patched via its real module reference to return a
fixed config instantly, since these tests are about the drainers' own
connect/stream/reconnect behavior, not config discovery (already covered in
`test_client_config_provider.py`).
"""

# Standard library imports
import asyncio
import logging
import queue as queue_mod
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest
from aiologic import SimpleQueue as AiologicQueue

# First party imports
from aeth_ext.central_log_server import client as client_mod
from aeth_ext.central_log_server._types import RegisterClient
from aeth_ext.central_log_server.client import AsyncioQueueDrainer, ThreadedQueueDrainer
from aeth_ext.central_log_server.client.history import RecordHistoryBuffer
from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
from aeth_ext.central_log_server.server.reader_server import LogRecordServer
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # First party imports
  from aeth_ext.central_log_server._types import WriterItem
  from aeth_ext.central_log_server.client.config_provider import LoggingConfigResult

_GET_TIMEOUT = 5.0
_VALID_REMOTE_CONFIG: dict[str, Any] = {"version": 1, "root": {"level": "DEBUG"}}
_INVALID_REMOTE_CONFIG: dict[str, Any] = {
  "version": 1,
  "handlers": {"bad": {"class": "not.a.real.module.Handler"}},
  "root": {"handlers": ["bad"]},
}


def _config(program_name: str = "test-program", remote: dict[str, Any] | None = None) -> LoggingConfigResult:
  return {
    "local": {},
    "remote": dict(remote if remote is not None else _VALID_REMOTE_CONFIG),
    "program_name": program_name,
    "testing": False,
  }


def _make_record(message: str = "hello") -> TaggedLogRecord:
  return TaggedLogRecord("prog.module", logging.INFO, __file__, 1, message, None, None)


async def _get(queue: AiologicQueue[WriterItem]) -> WriterItem:
  return await asyncio.wait_for(queue.async_get(), timeout=_GET_TIMEOUT)


def _get_sync(queue: AiologicQueue[WriterItem]) -> WriterItem:
  return queue.green_get(timeout=_GET_TIMEOUT)


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
  persist_dir = tmp_path / "persist"
  persist_dir.mkdir()
  monkeypatch.setattr(client_mod.settings, "persisted_dir_loc", persist_dir)
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "hist")
  return tmp_path


@pytest.fixture
def stub_query_logging_configs(monkeypatch: pytest.MonkeyPatch) -> dict[str, LoggingConfigResult]:
  """Patch `query_logging_configs` via its real module reference to return a fixed config."""

  # First party imports
  from aeth_ext.central_log_server.client import config_provider as config_provider_module

  configs: dict[str, LoggingConfigResult] = {}

  def fake_query(target: str, *, mode: str = "socket", timeout: float = 30.0) -> LoggingConfigResult:
    return configs.get(target, _config(program_name=target))

  monkeypatch.setattr(config_provider_module, "query_logging_configs", fake_query)
  return configs


@pytest.fixture
def running_server(isolated_dirs: Path) -> Any:
  """Run a real `LogRecordServer` on its own dedicated thread + event loop.

  Both drainer classes are tested from both async and sync test bodies. A
  server started via an async fixture would live on pytest-asyncio's own
  per-test loop, which only pumps I/O while that specific coroutine is
  running -- a sync test body never drives it, so on Windows' Proactor loop
  the server would accept a connection at the OS level but never actually
  process the handshake until long after the test already finished. Running
  the server on its own thread with its own always-running loop sidesteps
  that entirely and works the same way for either kind of test.
  """
  server_queue: AiologicQueue[WriterItem] = AiologicQueue()
  id_registry = ClientIdRegistry()
  ready = threading.Event()
  state: dict[str, Any] = {}

  async def _run() -> None:
    state["loop"] = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    state["stop_event"] = stop_event
    server = LogRecordServer(queue=server_queue, id_registry=id_registry, host="127.0.0.1", port=0, log_dir=isolated_dirs / "logs")
    tcp_server = await server.start_server()
    state["port"] = tcp_server.sockets[0].getsockname()[1]
    ready.set()
    try:
      await stop_event.wait()
    finally:
      tcp_server.close()
      await tcp_server.wait_closed()

  thread = threading.Thread(target=lambda: asyncio.run(_run()), daemon=True)
  thread.start()
  assert ready.wait(timeout=5.0), "server thread failed to start"

  try:
    yield state["port"], server_queue
  finally:
    loop: asyncio.AbstractEventLoop = state["loop"]
    stop_event: asyncio.Event = state["stop_event"]
    loop.call_soon_threadsafe(stop_event.set)
    thread.join(timeout=5.0)


class TestAsyncioQueueDrainer:
  async def test_a_queued_record_is_delivered_to_a_real_server(
    self,
    running_server: tuple[int, AiologicQueue[WriterItem]],
    stub_query_logging_configs: dict[str, LoggingConfigResult],
    isolated_dirs: Path,
  ) -> None:
    port, server_queue = running_server
    stub_query_logging_configs["myprog"] = _config("myprog")
    record_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue()

    async with AsyncioQueueDrainer(record_queue, "myprog", host="127.0.0.1", port=port) as drainer:
      await record_queue.put(_make_record("via asyncio drainer"))

      registered = await _get(server_queue)
      assert isinstance(registered, RegisterClient)
      assert registered.program_name == "myprog"

      delivered = await _get(server_queue)
      assert isinstance(delivered, logging.LogRecord)
      assert delivered.getMessage() == "via asyncio drainer"
      assert delivered.source_name == "myprog"

    assert drainer._task is None  # pyright: ignore[reportPrivateUsage]

  async def test_connect_and_verify_raises_on_a_rejected_handshake(
    self,
    running_server: tuple[int, AiologicQueue[WriterItem]],
    stub_query_logging_configs: dict[str, LoggingConfigResult],
    isolated_dirs: Path,
  ) -> None:
    port, _server_queue = running_server
    stub_query_logging_configs["rejected"] = _config("rejected", remote=_INVALID_REMOTE_CONFIG)
    record_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue()
    drainer = AsyncioQueueDrainer(record_queue, "rejected", host="127.0.0.1", port=port)

    with pytest.raises(RuntimeError, match="rejected"):
      await drainer.connect_and_verify()

  async def test_connect_and_verify_is_not_an_error_when_the_server_is_unreachable(
    self, stub_query_logging_configs: dict[str, LoggingConfigResult], isolated_dirs: Path
  ) -> None:
    record_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue()
    drainer = AsyncioQueueDrainer(record_queue, "unreachable", host="127.0.0.1", port=1)

    await drainer.connect_and_verify()  # must not raise

  async def test_enters_emergency_mode_once_both_thresholds_are_exceeded(
    self, stub_query_logging_configs: dict[str, LoggingConfigResult], isolated_dirs: Path
  ) -> None:
    record_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue()
    drainer = AsyncioQueueDrainer(
      record_queue, "emergency", host="127.0.0.1", port=1, emergency_time_threshold=0.0, emergency_attempt_threshold=1
    )
    drainer.record_failure()
    drainer._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    drainer.maybe_enter()

    assert drainer.writer is not None
    drainer.writer.close()


class TestThreadedQueueDrainer:
  def test_a_queued_record_is_delivered_to_a_real_server(
    self,
    running_server: tuple[int, AiologicQueue[WriterItem]],
    stub_query_logging_configs: dict[str, LoggingConfigResult],
    isolated_dirs: Path,
  ) -> None:
    port, server_queue = running_server
    stub_query_logging_configs["threadprog"] = _config("threadprog")
    record_queue: queue_mod.Queue[logging.LogRecord] = queue_mod.Queue()

    with ThreadedQueueDrainer(record_queue, "threadprog", host="127.0.0.1", port=port) as drainer:
      record_queue.put(_make_record("via threaded drainer"))

      registered = _get_sync(server_queue)
      assert isinstance(registered, RegisterClient)
      assert registered.program_name == "threadprog"

      delivered = _get_sync(server_queue)
      assert isinstance(delivered, logging.LogRecord)
      assert delivered.getMessage() == "via threaded drainer"

    assert not drainer._thread.is_alive()  # pyright: ignore[reportPrivateUsage]

  def test_connect_and_verify_raises_on_a_rejected_handshake(
    self,
    running_server: tuple[int, AiologicQueue[WriterItem]],
    stub_query_logging_configs: dict[str, LoggingConfigResult],
    isolated_dirs: Path,
  ) -> None:
    port, _server_queue = running_server
    stub_query_logging_configs["threadrejected"] = _config("threadrejected", remote=_INVALID_REMOTE_CONFIG)
    record_queue: queue_mod.Queue[logging.LogRecord] = queue_mod.Queue()
    drainer = ThreadedQueueDrainer(record_queue, "threadrejected", host="127.0.0.1", port=port)

    with pytest.raises(RuntimeError, match="threadrejected"):
      drainer.connect_and_verify()

  def test_connect_and_verify_is_not_an_error_when_the_server_is_unreachable(
    self, stub_query_logging_configs: dict[str, LoggingConfigResult], isolated_dirs: Path
  ) -> None:
    record_queue: queue_mod.Queue[logging.LogRecord] = queue_mod.Queue()
    drainer = ThreadedQueueDrainer(record_queue, "threadunreachable", host="127.0.0.1", port=1)

    drainer.connect_and_verify()  # must not raise
