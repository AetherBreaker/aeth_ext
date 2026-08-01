"""Tests for `aeth_ext.central_log_server.test_entrypoint` -- the lightweight
subprocess-based log-server entrypoint other programs' test suites spawn as a
fixture.

Every test here spawns the entrypoint as a genuine subprocess (matching how
real consumers use it) rather than importing and calling its internals
in-process, since the whole point of this module is its CLI/subprocess
contract: ready-line reporting, port binding, and stdin-close shutdown.
`FATAL_EVENT` is process-global and one-shot, so a fresh interpreter per test
is also what keeps these tests independent of each other and of the rest of
the suite.
"""

# Standard library imports
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

# Third party imports
import orjson
import pytest

# First party imports
from aeth_ext.central_log_server import test_entrypoint

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator
  from pathlib import Path

_LENGTH = struct.Struct(">L")
_SHUTDOWN_TIMEOUT = 10.0
_RECORD_WRITE_TIMEOUT = 5.0
_RECORD_WRITE_POLL = 0.05
_RECONNECT_TEST_RECORD_ID = 7

_FILE_HANDLER_CONFIG: dict[str, Any] = {
  "version": 1,
  "formatters": {"plain": {"format": "%(message)s"}},
  "handlers": {
    "file": {"class": "logging.FileHandler", "filename": "logdir://app.log", "formatter": "plain", "delay": True},
  },
  "root": {"level": "DEBUG", "handlers": ["file"]},
}

_INVALID_CONFIG: dict[str, Any] = {
  "version": 1,
  "handlers": {"bad": {"class": "not.a.real.module.Handler"}},
  "root": {"handlers": ["bad"]},
}


def _send_packet(sock: socket.socket, obj: object) -> None:
  data = orjson.dumps(obj, default=str)
  sock.sendall(_LENGTH.pack(len(data)) + data)


def _recv_packet(sock: socket.socket) -> dict[str, Any]:
  header = b""
  while len(header) < _LENGTH.size:
    chunk = sock.recv(_LENGTH.size - len(header))
    assert chunk, "connection closed while reading packet header"
    header += chunk
  (size,) = _LENGTH.unpack(header)
  body = b""
  while len(body) < size:
    chunk = sock.recv(size - len(body))
    assert chunk, "connection closed while reading packet body"
    body += chunk
  decoded = orjson.loads(body)
  assert isinstance(decoded, dict)
  return decoded


def _make_record(*, program_name: str, record_id: int, msg: str) -> dict[str, Any]:
  """A minimal `TaggedLogRecord`-shaped payload, as a real client would send."""
  return {
    "name": "root",
    "msg": msg,
    "args": None,
    "levelname": "INFO",
    "levelno": logging.INFO,
    "pathname": "somewhere.py",
    "filename": "somewhere.py",
    "module": "somewhere",
    "exc_info": None,
    "exc_text": None,
    "stack_info": None,
    "lineno": 1,
    "funcName": "some_func",
    "created": time.time(),
    "msecs": 0.0,
    "relativeCreated": 0.0,
    "thread": 1,
    "threadName": "MainThread",
    "processName": "MainProcess",
    "process": 1,
    "source_name": program_name,
    "record_id": record_id,
  }


class EntrypointProcess:
  """Wraps a spawned `test_entrypoint` subprocess plus its reported ready info."""

  def __init__(self, proc: subprocess.Popen[str], ready: dict[str, Any]) -> None:
    self.proc = proc
    self.ready = ready

  @property
  def log_port(self) -> int:
    return self.ready["log_port"]

  @property
  def web_viewer_port(self) -> int | None:
    return self.ready["web_viewer_port"]

  def connect(self) -> socket.socket:
    return socket.create_connection(("127.0.0.1", self.log_port), timeout=5)

  def request_shutdown(self, timeout: float = _SHUTDOWN_TIMEOUT) -> int:
    """Close stdin (the shutdown signal) and wait for the process to exit."""
    assert self.proc.stdin is not None
    self.proc.stdin.close()
    return self.proc.wait(timeout=timeout)


@pytest.fixture
def spawn_entrypoint(tmp_path: Path) -> Iterator[Callable[..., EntrypointProcess]]:
  """Factory fixture: spawn `test_entrypoint` as a subprocess with the given CLI args.

  Any process still alive at teardown is killed so a failing assertion
  mid-test can't leak a lingering server into later test runs.
  """
  spawned: list[subprocess.Popen[str]] = []

  def _spawn(*extra_args: str) -> EntrypointProcess:
    # PERSISTED_DIR_LOC governs where ClientIdRegistry persists client_ids.json
    # (Settings.persisted_dir_loc defaults to a fixed path under the real
    # process cwd, independent of --log-dir); overriding it keeps each test's
    # resume-id state isolated instead of leaking into a shared on-disk file.
    env = {**os.environ, "PERSISTED_DIR_LOC": str(tmp_path / "_persisted")}
    proc = subprocess.Popen(
      [sys.executable, "-m", test_entrypoint.__name__, "--log-dir", str(tmp_path), *extra_args],
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


class TestReadyReporting:
  def test_web_viewer_off_by_default_reports_null_port(self, spawn_entrypoint: Callable[..., EntrypointProcess]):
    ep = spawn_entrypoint()

    assert ep.log_port > 0
    assert ep.web_viewer_port is None
    assert ep.request_shutdown() == 0

  def test_ephemeral_log_port_is_actually_bindable(self, spawn_entrypoint: Callable[..., EntrypointProcess]):
    ep = spawn_entrypoint()

    sock = ep.connect()
    sock.close()
    assert ep.request_shutdown() == 0


class TestClientLifecycle:
  def test_handshake_ack_and_record_round_trip(self, spawn_entrypoint: Callable[..., EntrypointProcess], tmp_path: Path):
    ep = spawn_entrypoint()
    sock = ep.connect()
    try:
      _send_packet(sock, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
      ack = _recv_packet(sock)
      assert ack == {"ok": True, "error": None, "last_record_id": None, "last_received_at": None}

      _send_packet(sock, _make_record(program_name="acme", record_id=1, msg="hello from the test client"))
    finally:
      sock.close()

    log_file = tmp_path / "acme" / "app.log"
    deadline = time.monotonic() + _RECORD_WRITE_TIMEOUT
    while time.monotonic() < deadline and not (log_file.exists() and log_file.read_text().strip()):
      time.sleep(_RECORD_WRITE_POLL)

    assert log_file.exists(), "writer thread never created the program's log file"
    assert "hello from the test client" in log_file.read_text()
    assert ep.request_shutdown() == 0

  def test_reconnect_ack_reports_last_seen_record_id(self, spawn_entrypoint: Callable[..., EntrypointProcess]):
    ep = spawn_entrypoint()

    first = ep.connect()
    try:
      _send_packet(first, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
      _recv_packet(first)
      _send_packet(first, _make_record(program_name="acme", record_id=_RECONNECT_TEST_RECORD_ID, msg="first connection"))
      time.sleep(0.2)  # give the writer thread time to advance the id registry
    finally:
      first.close()

    second = ep.connect()
    try:
      _send_packet(second, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
      ack = _recv_packet(second)
      assert ack["ok"] is True
      assert ack["last_record_id"] == _RECONNECT_TEST_RECORD_ID
    finally:
      second.close()

    assert ep.request_shutdown() == 0

  def test_invalid_handshake_config_is_rejected(self, spawn_entrypoint: Callable[..., EntrypointProcess]):
    ep = spawn_entrypoint()
    sock = ep.connect()
    try:
      _send_packet(sock, {"program_name": "acme", "config": _INVALID_CONFIG})
      ack = _recv_packet(sock)
      assert ack["ok"] is False
      assert ack["error"] is not None
    finally:
      sock.close()
    assert ep.request_shutdown() == 0


class TestShutdown:
  def test_stdin_close_triggers_graceful_exit(self, spawn_entrypoint: Callable[..., EntrypointProcess]):
    ep = spawn_entrypoint()

    assert ep.proc.poll() is None  # still running before shutdown is requested
    assert ep.request_shutdown() == 0
    assert ep.proc.poll() == 0

  def test_record_sent_just_before_shutdown_is_still_flushed(
    self, spawn_entrypoint: Callable[..., EntrypointProcess], tmp_path: Path
  ):
    """A record sent right before shutdown must be drained and written, not dropped.

    `LogWriterThread` is documented to drain whatever is still queued once
    `FATAL_EVENT` fires, then flush and close every hierarchy before exiting;
    this exercises that guarantee through the real subprocess/socket path
    rather than just the writer thread's own unit tests.
    """
    ep = spawn_entrypoint()
    sock = ep.connect()
    _send_packet(sock, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
    _recv_packet(sock)
    _send_packet(sock, _make_record(program_name="acme", record_id=1, msg="flushed before shutdown"))
    sock.close()

    assert ep.request_shutdown() == 0

    log_file = tmp_path / "acme" / "app.log"
    assert log_file.exists()
    assert "flushed before shutdown" in log_file.read_text()


class TestWebViewer:
  def test_opt_in_web_viewer_reports_a_bound_port(self, spawn_entrypoint: Callable[..., EntrypointProcess]):
    ep = spawn_entrypoint("--with-web-viewer", "--web-viewer-port", "0")

    assert ep.web_viewer_port is not None
    assert ep.web_viewer_port > 0
    assert ep.request_shutdown() == 0
