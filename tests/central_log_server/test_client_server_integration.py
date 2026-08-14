"""End-to-end tests exercising a real client (`HandshakeSocketHandler`) talking to a real,
subprocess-hosted `central_log_server` over an actual TCP socket.

Unlike `test_client.py` (which drives `HandshakeSocketHandler` against socketpairs/mocks) and
`test_test_entrypoint.py` (which drives the real server with hand-crafted raw packets), these
tests pair the real client class with the real server subprocess so the wire protocol, the
handshake ack, and the id-based backlog replay are all exercised together, including what
happens when either side of the connection goes down mid-stream.
"""

# Standard library imports
import json
import logging
import os
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server import client as client_mod, test_entrypoint
from aeth_ext.central_log_server.client import HandshakeSocketHandler
from aeth_ext.central_log_server.client.history import RecordHistoryBuffer
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator
  from pathlib import Path

_SHUTDOWN_TIMEOUT = 10.0
_RECORD_WRITE_TIMEOUT = 5.0
_RECORD_WRITE_POLL = 0.05

_FILE_HANDLER_CONFIG: dict[str, Any] = {
  "version": 1,
  "formatters": {"plain": {"format": "%(message)s"}},
  "handlers": {
    "file": {"class": "logging.FileHandler", "filename": "logdir://app.log", "formatter": "plain", "delay": True},
  },
  "root": {"level": "DEBUG", "handlers": ["file"]},
}


def _free_port() -> int:
  """Grab an OS-assigned ephemeral port and release it immediately for reuse.

  Needed only when the server must be restarted on the *same* port (to
  simulate an outage-and-recovery cycle) - normal single-run tests let the
  entrypoint pick its own ephemeral port instead.
  """
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def _wait_for_text(path: Path, expected: str, timeout: float = _RECORD_WRITE_TIMEOUT) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if path.exists() and expected in path.read_text():
      return
    time.sleep(_RECORD_WRITE_POLL)
  actual = path.read_text() if path.exists() else "<file does not exist>"
  pytest.fail(f"timed out waiting for {expected!r} to appear in {path}; contents:\n{actual}")


class EntrypointProcess:
  """A running `test_entrypoint` subprocess plus the ready info it reported."""

  def __init__(self, proc: subprocess.Popen[str], ready: dict[str, Any]) -> None:
    self.proc = proc
    self.ready = ready

  @property
  def log_port(self) -> int:
    return self.ready["log_port"]

  def request_shutdown(self, timeout: float = _SHUTDOWN_TIMEOUT) -> int:
    """Close stdin (the graceful-shutdown signal) and wait for the process to exit."""
    assert self.proc.stdin is not None
    self.proc.stdin.close()
    return self.proc.wait(timeout=timeout)


@pytest.fixture
def spawn_entrypoint(tmp_path: Path) -> Iterator[Callable[..., EntrypointProcess]]:
  """Factory fixture: spawn a real `central_log_server` subprocess with the given CLI args.

  Every call within a test shares the same ``--log-dir`` and ``PERSISTED_DIR_LOC``
  (both derived from the test's own ``tmp_path``), so spawning a second server
  after killing the first - to simulate an outage-and-recovery cycle - resumes
  from the same on-disk log files and id registry a real restart would see.
  Any process still alive at teardown is killed so a failing assertion mid-test
  can't leak a server into later test runs.
  """
  spawned: list[subprocess.Popen[str]] = []

  def _spawn(*extra_args: str) -> EntrypointProcess:
    env = {**os.environ, "PERSISTED_DIR_LOC": str(tmp_path / "_persisted")}
    proc = subprocess.Popen(
      [sys.executable, "-m", test_entrypoint.__name__, "run", "--log-dir", str(tmp_path), *extra_args],
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      env=env,
    )
    spawned.append(proc)
    assert proc.stdout is not None
    ready_line = proc.stdout.readline()
    if not ready_line:
      proc.kill()
      stderr = proc.stderr.read() if proc.stderr is not None else ""
      pytest.fail(f"entrypoint produced no ready line; exited with {proc.poll()}\nSTDERR:\n{stderr}")
    ready: dict[str, Any] = json.loads(ready_line)
    return EntrypointProcess(proc, ready)

  yield _spawn

  for proc in spawned:
    if proc.poll() is None:
      proc.kill()
      proc.wait(timeout=5)


@pytest.fixture
def make_client_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Callable[..., HandshakeSocketHandler]]:
  """Build real `HandshakeSocketHandler`s whose disk state (id checkpoint, history spill) stays
  inside `tmp_path`, closing them all on teardown.

  Also installs `TaggedLogRecord` as the process's log record factory for the
  duration of the test (and restores whatever was installed before): the
  handler's own `HistoryEntry` bookkeeping requires every emitted record to
  actually be a `TaggedLogRecord`, which plain `logging.getLogger(...)`
  loggers only produce once this factory is installed.
  """
  persist_dir = tmp_path / "client_persist"
  persist_dir.mkdir()
  monkeypatch.setattr(client_mod.settings, "persisted_dir_loc", persist_dir)
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "client_history")

  previous_factory = logging.getLogRecordFactory()
  logging.setLogRecordFactory(TaggedLogRecord)

  created: list[HandshakeSocketHandler] = []

  def factory(program_name: str, port: int, **kwargs: Any) -> HandshakeSocketHandler:
    handler = HandshakeSocketHandler(program_name, _FILE_HANDLER_CONFIG, host="127.0.0.1", port=port, **kwargs)
    created.append(handler)
    return handler

  yield factory

  for handler in created:
    handler.close()
  logging.setLogRecordFactory(previous_factory)


class TestFullCommunicationCycle:
  def test_client_and_server_round_trip_a_record_over_the_real_wire_protocol(
    self,
    spawn_entrypoint: Callable[..., EntrypointProcess],
    make_client_handler: Callable[..., HandshakeSocketHandler],
    tmp_path: Path,
  ) -> None:
    ep = spawn_entrypoint()
    handler = make_client_handler("acme", ep.log_port)
    handler.connect_and_verify()

    emitter = logging.getLogger("acme.full-cycle")
    emitter.setLevel(logging.DEBUG)
    emitter.addHandler(handler)
    try:
      emitter.info("hello over the real wire")
    finally:
      emitter.removeHandler(handler)

    _wait_for_text(tmp_path / "acme" / "app.log", "hello over the real wire")

    # A live client connection keeps the server's per-connection task running,
    # which (as of the asyncio.Server semantics this project targets) blocks
    # `wait_closed()` during shutdown; disconnect first, as a real client
    # would before the server it's talking to goes away.
    assert handler.sock is not None
    handler.sock.close()
    handler.sock = None

    assert ep.request_shutdown() == 0


class TestServerOutageAndRecovery:
  def test_records_emitted_during_a_server_outage_are_replayed_after_it_comes_back(
    self,
    spawn_entrypoint: Callable[..., EntrypointProcess],
    make_client_handler: Callable[..., HandshakeSocketHandler],
    tmp_path: Path,
  ) -> None:
    port = _free_port()
    ep1 = spawn_entrypoint("--port", str(port))
    handler = make_client_handler("acme", port)
    handler.connect_and_verify()

    logger_ = logging.getLogger("acme.outage")
    logger_.setLevel(logging.DEBUG)
    logger_.addHandler(handler)
    try:
      log_file = tmp_path / "acme" / "app.log"
      logger_.info("before outage")
      _wait_for_text(log_file, "before outage")
      # Give the writer thread's idle-triggered id-registry save (every
      # LogWriterThread.POLL_INTERVAL seconds of queue silence) a chance to
      # land on disk, so the *second* server process resumes from exactly
      # this record instead of replaying everything from scratch.
      time.sleep(0.75)

      ep1.proc.kill()
      ep1.proc.wait(timeout=5)

      # Let the client discover the outage the way it would in production:
      # by attempting a real send over the now-dead TCP connection and
      # having the OS eventually report the failure, rather than closing the
      # socket out from under it ourselves. Whether that surfaces on the
      # first attempt or only after several depends on OS-level details (a
      # graceful close from the dying process's FIN vs. an abortive RST, how
      # much the client has buffered, etc.), so keep sending real records -
      # each one must itself survive to be replayed once the outage is
      # detected - until the handler notices, bounded by a generous timeout.
      buffered_messages: list[str] = []
      detection_deadline = time.monotonic() + 20.0
      while handler.sock is not None:
        assert time.monotonic() < detection_deadline, "client never detected the dead server via a failed send"
        message = f"buffered during outage {len(buffered_messages) + 1}"
        logger_.info(message)
        buffered_messages.append(message)
        time.sleep(0.2)

      assert buffered_messages, "the outage was detected before any record was buffered during it"

      ep2 = spawn_entrypoint("--port", str(port))
      try:
        # Bypass SocketHandler's built-in reconnect backoff (set by the
        # failed attempts above) so the next emit retries immediately rather
        # than possibly waiting out `retryPeriod` first.
        handler.retryTime = None
        logger_.info("after reconnect")

        for message in buffered_messages:
          _wait_for_text(log_file, message)
        _wait_for_text(log_file, "after reconnect")

        # The record from before the outage must not have been replayed a
        # second time - proof the server's ack resumed the client precisely
        # at its last confirmed id rather than resending everything.
        assert log_file.read_text().count("before outage") == 1

        # Disconnect before the (test-initiated, graceful) shutdown below -
        # unlike the outage above, there's nothing to simulate here.
        assert handler.sock is not None
        handler.sock.close()
        handler.sock = None
      finally:
        assert ep2.request_shutdown() == 0
    finally:
      logger_.removeHandler(handler)


class TestClientOutage:
  def test_an_abruptly_closed_client_connection_does_not_disrupt_the_server(
    self,
    spawn_entrypoint: Callable[..., EntrypointProcess],
    make_client_handler: Callable[..., HandshakeSocketHandler],
    tmp_path: Path,
  ) -> None:
    ep = spawn_entrypoint()

    crashing_handler = make_client_handler("crashy", ep.log_port)
    crashing_handler.connect_and_verify()
    crashing_logger = logging.getLogger("crashy.outage")
    crashing_logger.setLevel(logging.DEBUG)
    crashing_logger.addHandler(crashing_handler)
    try:
      crashing_logger.info("last words before crashing")
      _wait_for_text(tmp_path / "crashy" / "app.log", "last words before crashing")
    finally:
      crashing_logger.removeHandler(crashing_handler)

    # Simulate the client process dying mid-connection: yank the socket out
    # from under the handler directly, rather than `handler.close()` (which
    # would perform a clean, graceful shutdown the server would never see
    # from an actual crash).
    assert crashing_handler.sock is not None
    crashing_handler.sock.close()

    # The server must still be alive and able to serve an unrelated program
    # after losing that connection without warning.
    assert ep.proc.poll() is None
    survivor_handler = make_client_handler("survivor", ep.log_port)
    survivor_handler.connect_and_verify()
    survivor_logger = logging.getLogger("survivor.outage")
    survivor_logger.setLevel(logging.DEBUG)
    survivor_logger.addHandler(survivor_handler)
    try:
      survivor_logger.info("still working")
      _wait_for_text(tmp_path / "survivor" / "app.log", "still working")
    finally:
      survivor_logger.removeHandler(survivor_handler)

    assert ep.proc.poll() is None

    assert survivor_handler.sock is not None
    survivor_handler.sock.close()
    survivor_handler.sock = None

    assert ep.request_shutdown() == 0
