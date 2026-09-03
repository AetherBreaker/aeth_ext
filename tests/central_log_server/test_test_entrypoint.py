# Standard library imports
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Third party imports
import orjson
import pytest

# First party imports
from aeth_ext.central_log_server import test_entrypoint

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator

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
  def __init__(self, proc: subprocess.Popen[str], ready: dict[str, Any]) -> None:
    self.proc = proc
    self.ready = ready

  @property
  def log_port(self) -> int:
    return self.ready["log_port"]

  @property
  def web_viewer_port(self) -> int | None:
    return self.ready["web_viewer_port"]

  @property
  def log_dir(self) -> Path:
    return Path(self.ready["log_dir"])

  def connect(self) -> socket.socket:
    return socket.create_connection(("127.0.0.1", self.log_port), timeout=5)

  def request_shutdown(self, timeout: float = _SHUTDOWN_TIMEOUT) -> int:
    assert self.proc.stdin is not None
    self.proc.stdin.close()
    return self.proc.wait(timeout=timeout)


@pytest.fixture
def spawn_entrypoint(tmp_path: Path) -> Iterator[Callable[..., EntrypointProcess]]:
  spawned: list[subprocess.Popen[str]] = []

  def _spawn(*extra_args: str, include_log_dir: bool = True) -> EntrypointProcess:
    # PERSISTED_DIR_LOC governs where ClientIdRegistry persists client_ids.json
    # (Settings.persisted_dir_loc defaults to a fixed path under the real
    # process cwd, independent of --log-dir); overriding it keeps each test's
    # resume-id state isolated instead of leaking into a shared on-disk file.
    env = {**os.environ, "PERSISTED_DIR_LOC": str(tmp_path / "_persisted")}
    log_dir_args = ["--log-dir", str(tmp_path)] if include_log_dir else []
    proc = subprocess.Popen(
      [sys.executable, "-m", test_entrypoint.__name__, "run", *log_dir_args, *extra_args],
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
  def test_web_viewer_off_by_default_reports_null_port(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint()

    assert ep.log_port > 0
    assert ep.web_viewer_port is None
    assert ep.request_shutdown() == 0

  def test_ephemeral_log_port_is_actually_bindable(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint()

    sock = ep.connect()
    sock.close()
    assert ep.request_shutdown() == 0


class TestClientLifecycle:
  def test_handshake_ack_and_record_round_trip(self, spawn_entrypoint: Callable[..., EntrypointProcess], tmp_path: Path) -> None:
    ep = spawn_entrypoint()
    sock = ep.connect()
    try:
      _send_packet(sock, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
      ack = _recv_packet(sock)
      assert ack == {
        "ok": True,
        "error": None,
        "last_record_id": None,
        "last_received_at": None,
        "type": "handshake_ack",
      }

      _send_packet(sock, _make_record(program_name="acme", record_id=1, msg="hello from the test client"))
    finally:
      sock.close()

    log_file = tmp_path / "acme" / "app.log"
    deadline = time.monotonic() + _RECORD_WRITE_TIMEOUT
    while time.monotonic() < deadline and "hello from the test client" not in (log_file.read_text() if log_file.exists() else ""):
      time.sleep(_RECORD_WRITE_POLL)

    assert log_file.exists(), "writer thread never created the program's log file"
    assert "hello from the test client" in log_file.read_text()
    assert ep.request_shutdown() == 0

  def test_reconnect_ack_reports_last_seen_record_id(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint()

    first = ep.connect()
    try:
      _send_packet(first, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
      _recv_packet(first)
      _send_packet(first, _make_record(program_name="acme", record_id=_RECONNECT_TEST_RECORD_ID, msg="first connection"))
    finally:
      first.close()

    # Poll by reconnecting rather than sleeping a fixed guess: the writer
    # thread advances the id registry asynchronously, so retry the handshake
    # until it reports the record or the deadline passes.
    deadline = time.monotonic() + _RECORD_WRITE_TIMEOUT
    ack: dict[str, Any] = {}
    while time.monotonic() < deadline:
      second = ep.connect()
      try:
        _send_packet(second, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
        ack = _recv_packet(second)
        if ack.get("last_record_id") == _RECONNECT_TEST_RECORD_ID:
          break
      finally:
        second.close()
      time.sleep(_RECORD_WRITE_POLL)

    assert ack.get("ok") is True
    assert ack.get("last_record_id") == _RECONNECT_TEST_RECORD_ID

    assert ep.request_shutdown() == 0

  def test_invalid_handshake_config_is_rejected(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
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
  def test_stdin_close_triggers_graceful_exit(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint()

    assert ep.proc.poll() is None  # still running before shutdown is requested
    assert ep.request_shutdown() == 0
    assert ep.proc.poll() == 0

  def test_shutdown_with_in_flight_record_exits_cleanly(
    self, spawn_entrypoint: Callable[..., EntrypointProcess], tmp_path: Path
  ) -> None:
    ep = spawn_entrypoint()
    sock = ep.connect()
    _send_packet(sock, {"program_name": "acme", "config": _FILE_HANDLER_CONFIG})
    _recv_packet(sock)
    _send_packet(sock, _make_record(program_name="acme", record_id=1, msg="flushed before shutdown"))
    sock.close()

    assert ep.request_shutdown() == 0

    log_file = tmp_path / "acme" / "app.log"
    assert log_file.exists()


class TestWebViewer:
  def test_opt_in_web_viewer_reports_a_bound_port(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint("--with-web-viewer", "--web-viewer-port", "0")

    assert ep.web_viewer_port is not None
    assert ep.web_viewer_port > 0
    assert ep.request_shutdown() == 0


class TestLogDirCleanup:
  def test_caller_supplied_log_dir_is_reported_and_survives_shutdown(
    self, spawn_entrypoint: Callable[..., EntrypointProcess], tmp_path: Path
  ) -> None:
    ep = spawn_entrypoint()

    assert ep.log_dir == tmp_path
    assert ep.request_shutdown() == 0
    assert tmp_path.exists()

  def test_auto_created_log_dir_survives_shutdown_by_default(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint(include_log_dir=False)

    assert ep.request_shutdown() == 0
    assert ep.log_dir.exists()

  def test_auto_cleanup_removes_the_auto_created_log_dir_on_shutdown(self, spawn_entrypoint: Callable[..., EntrypointProcess]) -> None:
    ep = spawn_entrypoint("--auto-cleanup", include_log_dir=False)

    assert ep.request_shutdown() == 0
    assert not ep.log_dir.exists()

  def test_auto_cleanup_never_deletes_a_caller_supplied_log_dir(
    self, spawn_entrypoint: Callable[..., EntrypointProcess], tmp_path: Path
  ) -> None:
    ep = spawn_entrypoint("--auto-cleanup")

    assert ep.request_shutdown() == 0
    assert tmp_path.exists()

  def test_cleanup_command_removes_a_given_directory(self, tmp_path: Path) -> None:
    target = tmp_path / "leftover_logs"
    target.mkdir()
    (target / "app.log").write_text("hello")

    result = subprocess.run(
      [sys.executable, "-m", test_entrypoint.__name__, "cleanup", str(target)],
      capture_output=True,
      text=True,
      timeout=10,
      check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not target.exists()

  def test_cleanup_command_on_missing_directory_does_not_error(self, tmp_path: Path) -> None:
    missing = tmp_path / "never_created"

    result = subprocess.run(
      [sys.executable, "-m", test_entrypoint.__name__, "cleanup", str(missing)],
      capture_output=True,
      text=True,
      timeout=10,
      check=False,
    )

    assert result.returncode == 0, result.stderr


class TestStartupFailure:
  def test_port_already_in_use_reports_an_error_line_and_exits_nonzero(self, tmp_path: Path) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken_port = blocker.getsockname()[1]

    env = {**os.environ, "PERSISTED_DIR_LOC": str(tmp_path / "_persisted")}
    proc = subprocess.Popen(
      [sys.executable, "-m", test_entrypoint.__name__, "run", "--log-dir", str(tmp_path), "--port", str(taken_port)],
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      env=env,
    )
    try:
      assert proc.stdout is not None
      line = proc.stdout.readline()
      assert line, "expected a JSON error line on stdout, got EOF instead"
      payload = json.loads(line)
      assert "error" in payload, f"expected an error field, got: {payload}"

      # A timeout here means the writer thread never got SHUTDOWN and is
      # keeping the (non-daemon) process alive -- the exact hang this test
      # guards against.
      _, stderr = proc.communicate(timeout=_SHUTDOWN_TIMEOUT)
      assert proc.returncode != 0, stderr
    finally:
      blocker.close()
      if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
