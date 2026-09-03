# Standard library imports
import logging
import socket
import threading
from typing import TYPE_CHECKING, Any, override

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server import client as client_mod
from aeth_ext.central_log_server.client import HandshakeSocketHandler
from aeth_ext.central_log_server.client.history import EmergencyHistoryWriter, HistoryEntry, RecordHistoryBuffer
from aeth_ext.errors import err_handling
from aeth_ext.logging import emergency_diagnostics
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator
  from pathlib import Path

# `_next_id` after one / two records have consumed an id.
_NEXT_ID_AFTER_ONE = 2
_NEXT_ID_AFTER_TWO = 3

_REACHABLE_CONFIG: dict[str, Any] = {
  "version": 1,
  "handlers": {"file": {"class": "logging.NullHandler"}},
  "root": {"level": "DEBUG", "handlers": ["file"]},
}


class _DeadSocket:
  def __init__(self) -> None:
    self.sends = 0

  def sendall(self, _data: bytes) -> None:
    self.sends += 1
    raise BrokenPipeError(32, "Broken pipe")

  def close(self) -> None:
    pass


@pytest.fixture
def root_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[HandshakeSocketHandler]:
  persist_dir = tmp_path / "persist"
  persist_dir.mkdir()
  monkeypatch.setattr(client_mod.settings, "persisted_dir_loc", persist_dir)
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "hist")
  handler = HandshakeSocketHandler("prog", _REACHABLE_CONFIG, host="127.0.0.1", port=1)
  root = logging.getLogger()
  root.addHandler(handler)
  previous_level = root.level
  root.setLevel(logging.DEBUG)
  # Records reach the handler through the real `logging` machinery, so they must be built by the
  # project's record factory, as `aeth_ext.logging.setup` installs it in every consumer.
  previous_factory = logging.getLogRecordFactory()
  logging.setLogRecordFactory(TaggedLogRecord)
  try:
    yield handler
  finally:
    logging.setLogRecordFactory(previous_factory)
    root.removeHandler(handler)
    root.setLevel(previous_level)
    handler.close()


@pytest.fixture
def forbid_fatal_path(monkeypatch: pytest.MonkeyPatch) -> None:
  def _forbidden(*args: object, **kwargs: object) -> None:
    pytest.fail(f"transport failure escalated to the fatal path: {args} {kwargs}")

  monkeypatch.setattr(err_handling, "_handle_fatal", _forbidden)
  monkeypatch.setattr(client_mod, "alert", _forbidden)
  monkeypatch.setattr(client_mod.shutdown, "run_shutdown", _forbidden)


@pytest.mark.usefixtures("forbid_fatal_path")
class TestDeadSocket:
  def test_one_failed_send_drops_the_socket_and_stops(
    self, root_handler: HandshakeSocketHandler, capsys: pytest.CaptureFixture[str]
  ) -> None:
    dead = _DeadSocket()
    root_handler.sock = dead  # pyright: ignore[reportAttributeAccessIssue]
    # Never reconnect during this test: the point is what happens on the *first* failure.
    root_handler.retryTime = float("inf")

    logging.getLogger("app").info("an ordinary record")

    assert dead.sends == 1, "the failure report was re-delivered through the dead socket"
    assert root_handler.sock is None
    assert root_handler._consecutive_failures == 1  # pyright: ignore[reportPrivateUsage]
    assert root_handler._next_id == _NEXT_ID_AFTER_ONE, "exactly one record consumed an id"  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "dropping the connection" in err
    assert "Broken pipe" in err

  def test_reconnect_failure_also_stays_quiet(self, root_handler: HandshakeSocketHandler, capsys: pytest.CaptureFixture[str]) -> None:
    assert root_handler.sock is None
    root_handler.retryTime = None

    logging.getLogger("app").info("first")
    logging.getLogger("app").info("second")

    assert root_handler._next_id == _NEXT_ID_AFTER_TWO  # pyright: ignore[reportPrivateUsage]
    assert root_handler._consecutive_failures == _NEXT_ID_AFTER_TWO - 1  # pyright: ignore[reportPrivateUsage]
    capsys.readouterr()  # no assertion on content: a refused connect is silent by stdlib design


@pytest.mark.usefixtures("forbid_fatal_path")
class TestReentrancyGuard:
  def test_record_logged_from_inside_the_transport_is_dropped(
    self, root_handler: HandshakeSocketHandler, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
  ) -> None:
    calls: list[int] = []

    def _transmit_that_logs(entry: HistoryEntry) -> bool:
      calls.append(entry.id)
      logging.getLogger("aeth_ext.something").warning("logged from inside the transport")
      return True

    monkeypatch.setattr(root_handler, "_transmit", _transmit_that_logs)

    logging.getLogger("app").info("outer")

    assert calls == [1], "the nested record was delivered instead of diverted"
    assert root_handler._next_id == _NEXT_ID_AFTER_ONE, "the nested record must not consume an id"  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "diverted a record logged from inside the transport" in err
    assert "aeth_ext.something WARNING: logged from inside the transport" in err

  def test_diverted_record_keeps_its_traceback(
    self, root_handler: HandshakeSocketHandler, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
  ) -> None:
    def _transmit_that_fails_and_logs(_entry: HistoryEntry) -> bool:
      try:
        raise ValueError("inner cause")
      except ValueError:
        logging.getLogger("aeth_ext.something").exception("inner failure")
      return True

    monkeypatch.setattr(root_handler, "_transmit", _transmit_that_fails_and_logs)

    logging.getLogger("app").info("outer")

    err = capsys.readouterr().err
    assert "inner failure" in err
    assert "ValueError: inner cause" in err
    assert "Traceback (most recent call last)" in err
    assert "inner failure" in emergency_diagnostics.emergency_diagnostics_path.read_text(encoding="utf-8")

  def test_guard_is_per_thread(self, root_handler: HandshakeSocketHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    delivered: list[str] = []
    monkeypatch.setattr(root_handler, "_transmit", lambda entry: delivered.append(entry.record.getMessage()) or True)
    root_handler._emit_guard.active = True  # pyright: ignore[reportPrivateUsage] -- this thread is "inside emit"
    try:
      t = threading.Thread(target=lambda: logging.getLogger("app").info("from another thread"))
      t.start()
      t.join(timeout=5)
    finally:
      root_handler._emit_guard.active = False  # pyright: ignore[reportPrivateUsage]

    assert delivered == ["from another thread"]


@pytest.mark.usefixtures("forbid_fatal_path")
class TestDeliveryFailureIsNotFatal:
  def test_unexpected_exception_is_reported_and_swallowed(
    self, root_handler: HandshakeSocketHandler, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
  ) -> None:
    def _boom(_entry: HistoryEntry) -> bool:
      raise RuntimeError("boom")

    monkeypatch.setattr(root_handler, "_transmit", _boom)

    logging.getLogger("app").info("record")  # must not raise

    assert root_handler._consecutive_failures == 1  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "delivery failed" in err
    assert "RuntimeError: boom" in err

  def test_guard_is_released_after_a_failure(
    self, root_handler: HandshakeSocketHandler, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
  ) -> None:
    attempts: list[int] = []

    def _fail_once_then_ok(entry: HistoryEntry) -> bool:
      attempts.append(entry.id)
      if len(attempts) == 1:
        raise RuntimeError("first")
      return True

    monkeypatch.setattr(root_handler, "_transmit", _fail_once_then_ok)

    logging.getLogger("app").info("one")
    logging.getLogger("app").info("two")

    assert attempts == [1, 2]
    assert "diverted a record" not in capsys.readouterr().err


class TestEmergencyWriterDoesNotLog:
  def test_unwritable_history_file_reports_to_stderr_only(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
  ) -> None:
    # First party imports
    from aeth_ext.central_log_server.client import history as history_mod

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    # The day's file path resolves to a directory, so opening it for append fails with an OSError.
    monkeypatch.setattr(history_mod, "_history_file_for_date", lambda _d, _day: blocked)
    writer = EmergencyHistoryWriter(tmp_path / "history")
    record = TaggedLogRecord("prog", logging.INFO, __file__, 1, "hello", None, None)
    with caplog.at_level(logging.DEBUG):
      writer.submit(HistoryEntry(id=1, created=record.created, record=record))
      writer.close()

    assert caplog.records == [], "the writer reported its failure through logging"
    assert "emergency history writer failed to open" in capsys.readouterr().err


class TestSocketStillCloses:
  def test_dead_socket_close_errors_are_suppressed(self, root_handler: HandshakeSocketHandler) -> None:
    class _ExplodingClose(_DeadSocket):
      @override
      def close(self) -> None:
        raise OSError("close failed too")

    root_handler.sock = _ExplodingClose()  # pyright: ignore[reportAttributeAccessIssue]
    root_handler.retryTime = float("inf")

    logging.getLogger("app").info("record")  # must not raise

    assert root_handler.sock is None


def test_real_peer_close_reproduces_the_production_sequence(
  root_handler: HandshakeSocketHandler, capsys: pytest.CaptureFixture[str]
) -> None:
  listener = socket.socket()
  listener.bind(("127.0.0.1", 0))
  listener.listen(1)
  client = socket.create_connection(listener.getsockname())
  server_side, _ = listener.accept()
  server_side.close()
  listener.close()
  root_handler.sock = client
  root_handler.retryTime = float("inf")
  try:
    # The first send after a peer close can succeed locally (the RST arrives afterwards); the
    # second is EPIPE/ECONNRESET. Either way the handler must end with no socket and no recursion.
    for _ in range(3):
      logging.getLogger("app").info("record")
  finally:
    client.close()

  assert root_handler.sock is None
  assert "dropping the connection" in capsys.readouterr().err


class TestEmergencyDiagnosticsArePersisted:
  def test_dead_socket_failure_lands_in_the_persisted_file(self, root_handler: HandshakeSocketHandler) -> None:
    root_handler.sock = _DeadSocket()  # pyright: ignore[reportAttributeAccessIssue]
    root_handler.retryTime = float("inf")

    logging.getLogger("app").info("record")

    text = emergency_diagnostics.emergency_diagnostics_path.read_text(encoding="utf-8")
    assert "dropping the connection" in text
    assert "Broken pipe" in text
    assert "MainThread" in text
    assert text[:4].isdigit(), "each line starts with a timestamp"

  def test_traceback_is_written_in_full(self) -> None:
    try:
      raise RuntimeError("the cause")
    except RuntimeError as e:
      emergency_diagnostics.emergency_diagnostic("something failed", exc=e)

    text = emergency_diagnostics.emergency_diagnostics_path.read_text(encoding="utf-8")
    assert "something failed" in text
    assert "RuntimeError: the cause" in text
    assert "Traceback (most recent call last)" in text
    # The origin column names the *reporting* call site, not `emergency_diagnostic` itself.
    assert "test_client_transport_reentrancy.test_traceback_is_written_in_full:" in text

  def test_rotates_once_past_the_size_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emergency_diagnostics, "_MAX_BYTES", 200)
    for i in range(20):
      emergency_diagnostics.emergency_diagnostic(f"line {i} " + "x" * 40)

    rotated = emergency_diagnostics.emergency_diagnostics_path.with_name(emergency_diagnostics.emergency_diagnostics_path.name + ".1")
    assert rotated.exists()
    assert emergency_diagnostics.emergency_diagnostics_path.stat().st_size <= 200 + 200, "the live file was rotated, not left to grow"
    assert "line 19" in emergency_diagnostics.emergency_diagnostics_path.read_text(encoding="utf-8")

  def test_unwritable_file_still_reaches_stderr_and_never_raises(
    self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
  ) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the parent directory should be")
    monkeypatch.setattr(emergency_diagnostics, "emergency_diagnostics_path", blocker / "cannot" / "exist.log")

    emergency_diagnostics.emergency_diagnostic("still reported")  # must not raise

    assert "still reported" in capsys.readouterr().err
