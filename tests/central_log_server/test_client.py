"""Tests for `aeth_ext.central_log_server.client.HandshakeSocketHandler` and helpers."""

# Standard library imports
import asyncio
import base64
import logging
import socket
import threading
from time import monotonic
from typing import TYPE_CHECKING, Any

# Third party imports
import cloudpickle
import pytest

# First party imports
from aeth_ext.central_log_server import client as client_mod
from aeth_ext.central_log_server.client import HandshakeSocketHandler, make_definition
from aeth_ext.central_log_server.client.history import HistoryEntry, RecordHistoryBuffer
from aeth_ext.central_log_server.protocol import ApplyFailure, ApplySuccess, HandshakeAck, encode_json_packet
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path

_FIRST_ID = 1
_ACK_LAST_ID = 3
_ACK_LAST_RECEIVED = 123.5

# A config whose root reaches a catch-all handler: nothing is pre-filtered.
_REACHABLE_CONFIG: dict[str, Any] = {
  "version": 1,
  "handlers": {"file": {"class": "logging.NullHandler"}},
  "root": {"level": "DEBUG", "handlers": ["file"]},
}
# A config with no handlers anywhere: every record is provably undeliverable.
_UNREACHABLE_CONFIG: dict[str, Any] = {"version": 1, "root": {"level": "DEBUG"}}
# Only ERROR and above can reach the handler.
_ERROR_ONLY_CONFIG: dict[str, Any] = {
  "version": 1,
  "handlers": {"file": {"class": "logging.NullHandler"}},
  "root": {"level": "ERROR", "handlers": ["file"]},
}


def _sample_factory() -> str:
  return "made by _sample_factory"


def _make_record(level: int = logging.INFO) -> TaggedLogRecord:
  return TaggedLogRecord("prog.module", level, __file__, 1, "hello", None, None)


@pytest.fixture
def make_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
  """Build handlers whose disk side effects stay inside tmp_path, closing them on teardown."""
  persist_dir = tmp_path / "persist"
  persist_dir.mkdir()
  monkeypatch.setattr(client_mod.settings, "persisted_dir_loc", persist_dir)
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "hist")
  created: list[HandshakeSocketHandler] = []

  def factory(config: dict[str, Any], **kwargs: Any) -> HandshakeSocketHandler:
    handler = HandshakeSocketHandler("prog", config, host="127.0.0.1", port=1, **kwargs)
    created.append(handler)
    return handler

  yield factory

  for handler in created:
    handler.close()


class TestMakeDefinition:
  def test_round_trips_via_cloudpickle(self):
    encoded = make_definition(_sample_factory)

    decoded = cloudpickle.loads(base64.b64decode(encoded))

    assert decoded() == "made by _sample_factory"

  def test_result_is_ascii_text(self):
    encoded = make_definition(_sample_factory)

    assert isinstance(encoded, str)
    assert encoded.isascii()


class TestEmitPreFilter:
  def test_undeliverable_record_never_consumes_an_id(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_UNREACHABLE_CONFIG)
    transmitted: list[HistoryEntry] = []
    monkeypatch.setattr(handler, "_transmit", lambda entry: transmitted.append(entry) or True)
    appended: list[HistoryEntry] = []
    monkeypatch.setattr(handler.history, "append", appended.append)

    handler.emit(_make_record(logging.CRITICAL))

    assert transmitted == []
    assert appended == []
    assert handler._next_id == _FIRST_ID  # pyright: ignore[reportPrivateUsage]

  def test_deliverable_record_gets_id_history_and_transmission(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    transmitted: list[HistoryEntry] = []
    monkeypatch.setattr(handler, "_transmit", lambda entry: transmitted.append(entry) or True)
    record = _make_record()

    handler.emit(record)

    (entry,) = transmitted
    assert entry.id == _FIRST_ID
    assert record.record_id == _FIRST_ID
    assert handler._next_id == _FIRST_ID + 1  # pyright: ignore[reportPrivateUsage]

  def test_records_below_reachable_level_are_dropped(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_ERROR_ONLY_CONFIG)
    transmitted: list[HistoryEntry] = []
    monkeypatch.setattr(handler, "_transmit", lambda entry: transmitted.append(entry) or True)

    handler.emit(_make_record(logging.INFO))
    handler.emit(_make_record(logging.ERROR))

    (entry,) = transmitted
    assert entry.record.levelno == logging.ERROR


class TestReadServerMessageSync:
  def test_parses_valid_ack(self):
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID)))

      message = client_mod.read_server_message_sync(client_side)

      assert isinstance(message, HandshakeAck)
      assert message.ok is True
      assert message.last_record_id == _ACK_LAST_ID
    finally:
      client_side.close()
      server_side.close()

  def test_parses_apply_failure(self):
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(ApplyFailure(error="disk full")))

      message = client_mod.read_server_message_sync(client_side)

      assert isinstance(message, ApplyFailure)
      assert message.error == "disk full"
    finally:
      client_side.close()
      server_side.close()

  @pytest.mark.parametrize(
    "payload",
    [
      pytest.param(b"garbage", id="malformed json"),
      pytest.param(b"[1, 2]", id="non-dict"),
      pytest.param(b'{"nonsense": true}', id="wrong keys"),
    ],
  )
  def test_malformed_payload_returns_none(self, payload: bytes):
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)

      assert client_mod.read_server_message_sync(client_side) is None
    finally:
      client_side.close()
      server_side.close()

  def test_peer_hangup_returns_none(self):
    client_side, server_side = socket.socketpair()
    try:
      server_side.close()

      assert client_mod.read_server_message_sync(client_side) is None
    finally:
      client_side.close()


class TestReadServerMessageAsync:
  async def test_parses_valid_ack(self):
    reader = asyncio.StreamReader()
    reader.feed_data(encode_json_packet(HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID)))
    reader.feed_eof()

    message = await client_mod.read_server_message_async(reader)

    assert isinstance(message, HandshakeAck)
    assert message.ok is True
    assert message.last_record_id == _ACK_LAST_ID

  async def test_parses_apply_failure(self):
    reader = asyncio.StreamReader()
    reader.feed_data(encode_json_packet(ApplyFailure(error="disk full")))
    reader.feed_eof()

    message = await client_mod.read_server_message_async(reader)

    assert isinstance(message, ApplyFailure)
    assert message.error == "disk full"

  @pytest.mark.parametrize(
    "payload",
    [
      pytest.param(b"garbage", id="malformed json"),
      pytest.param(b"[1, 2]", id="non-dict"),
      pytest.param(b'{"nonsense": true}', id="wrong keys"),
    ],
  )
  async def test_malformed_payload_returns_none(self, payload: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)
    reader.feed_eof()

    assert await client_mod.read_server_message_async(reader) is None

  async def test_peer_hangup_returns_none(self):
    reader = asyncio.StreamReader()
    reader.feed_eof()

    assert await client_mod.read_server_message_async(reader) is None


class TestSendHandshake:
  def test_rejection_records_error_and_drops_socket(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    rejections: list[str] = []
    monkeypatch.setattr(client_mod, "trigger_shutdown", lambda reason, details, **_kw: rejections.append(details))
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=False, error="config invalid")))
      handler.sock = client_side

      handler._send_handshake()  # pyright: ignore[reportPrivateUsage]

      assert handler._handshake_rejected == "config invalid"  # pyright: ignore[reportPrivateUsage]
      assert handler.sock is None
      assert len(rejections) == 1
      assert "prog" in rejections[0]
      assert "config invalid" in rejections[0]
    finally:
      server_side.close()

  def test_failed_ack_read_alerts_without_setting_handshake_rejected(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    """D-E7: a failed/timed-out ack read alerts but is not treated as a rejection."""
    handler = make_handler(_REACHABLE_CONFIG)
    ack_failures: list[str] = []
    monkeypatch.setattr(client_mod, "alert", lambda _reason, details, **_kw: ack_failures.append(details))
    client_side, server_side = socket.socketpair()
    try:
      # Half-close (SHUT_WR) rather than close(): the handshake's own sendall()
      # must still succeed -- a fully dead connection is a different, earlier
      # code path in _send_handshake (the OSError handler around sendall,
      # which reconnects on the next emit instead of reading an ack at all).
      # A hard close() only proves that distinction on Windows, where
      # socketpair() is TCP-loopback-emulated and a small sendall() survives a
      # fully closed peer; on Linux's real AF_UNIX pair, close() tears the
      # connection down immediately and sendall() itself fails with
      # BrokenPipeError, so the ack-read path this test targets is never
      # reached. Shutting down only the write side leaves the server's
      # receive buffer open for the handshake write, and makes the
      # subsequent ack read see a clean EOF on every platform.
      server_side.shutdown(socket.SHUT_WR)  # peer hangs up before ever sending an ack
      handler.sock = client_side

      handler._send_handshake()  # pyright: ignore[reportPrivateUsage]

      assert len(ack_failures) == 1
      assert "prog" in ack_failures[0]
      assert handler._handshake_rejected is None  # pyright: ignore[reportPrivateUsage]
    finally:
      client_side.close()
      server_side.close()

  def test_successful_ack_triggers_backlog_replay(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    find_after_calls: list[tuple[int | None, float | None]] = []

    def fake_find_after(last_id: int | None, hint_created: float | None) -> tuple[()]:
      find_after_calls.append((last_id, hint_created))
      return ()

    monkeypatch.setattr(handler.history, "find_after", fake_find_after)
    client_side, server_side = socket.socketpair()
    try:
      ack = HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID, last_received_at=_ACK_LAST_RECEIVED)
      server_side.sendall(encode_json_packet(ack))
      handler.sock = client_side

      handler._send_handshake()  # pyright: ignore[reportPrivateUsage]

      assert find_after_calls == [(_ACK_LAST_ID, _ACK_LAST_RECEIVED)]
      assert handler._handshake_rejected is None  # pyright: ignore[reportPrivateUsage]
      assert handler.sock is client_side
    finally:
      handler.sock = None
      client_side.close()
      server_side.close()


class TestWatchApplyResult:
  """D-E2/D-E2a: the out-of-band apply-result message the writer thread sends after the ack."""

  _WATCH_TIMEOUT = 5.0

  def test_apply_failure_triggers_config_rejected(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(handler.history, "find_after", lambda *_a: ())
    rejections: list[str] = []
    monkeypatch.setattr(client_mod, "trigger_shutdown", lambda reason, details, **_kw: rejections.append(details))
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=True)))
      handler.sock = client_side

      handler._send_handshake()  # pyright: ignore[reportPrivateUsage]
      server_side.sendall(encode_json_packet(ApplyFailure(error="disk full")))

      deadline = monotonic() + self._WATCH_TIMEOUT
      while not rejections and monotonic() < deadline:
        pass
      assert len(rejections) == 1
      assert "prog" in rejections[0]
      assert "disk full" in rejections[0]
    finally:
      handler.sock = None
      client_side.close()
      server_side.close()

  def test_apply_success_does_not_alert(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(handler.history, "find_after", lambda *_a: ())
    rejections: list[str] = []
    monkeypatch.setattr(client_mod, "trigger_shutdown", lambda reason, details, **_kw: rejections.append(details))
    client_side, server_side = socket.socketpair()
    watcher_done = threading.Event()
    original_watch = handler._watch_apply_result  # pyright: ignore[reportPrivateUsage]

    def watch_and_signal(sock: socket.socket) -> None:
      original_watch(sock)
      watcher_done.set()

    monkeypatch.setattr(handler, "_watch_apply_result", watch_and_signal)
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=True)))
      handler.sock = client_side

      handler._send_handshake()  # pyright: ignore[reportPrivateUsage]
      server_side.sendall(encode_json_packet(ApplySuccess()))

      assert watcher_done.wait(timeout=self._WATCH_TIMEOUT)
      assert rejections == []
    finally:
      handler.sock = None
      client_side.close()
      server_side.close()


class TestConnectAndVerify:
  def test_raises_runtime_error_when_rejected(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)

    def fake_create_socket() -> None:
      handler._handshake_rejected = "bad remote config"  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(handler, "createSocket", fake_create_socket)

    with pytest.raises(RuntimeError, match="bad remote config"):
      handler.connect_and_verify()

  def test_unreachable_server_is_not_an_error(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(handler, "createSocket", lambda: None)

    handler.connect_and_verify()

    assert handler.sock is None


class TestTransmit:
  def test_fresh_send_delivers_the_record_and_advances_last_sent_id(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG)
    client_side, server_side = socket.socketpair()
    try:
      handler.sock = client_side
      entry = HistoryEntry(id=_FIRST_ID, created=0.0, record=_make_record())

      result = handler._transmit(entry)  # pyright: ignore[reportPrivateUsage]

      assert result is True
      assert handler.last_sent_id == _FIRST_ID
      assert server_side.recv(65536)
    finally:
      handler.sock = None
      client_side.close()
      server_side.close()

  def test_an_entry_at_or_before_the_last_sent_id_is_a_noop(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG)
    client_side, server_side = socket.socketpair()
    try:
      handler.sock = client_side
      handler.mark_sent(_ACK_LAST_ID)
      entry = HistoryEntry(id=_FIRST_ID, created=0.0, record=_make_record())

      result = handler._transmit(entry)  # pyright: ignore[reportPrivateUsage]

      assert result is True
      assert handler.last_sent_id == _ACK_LAST_ID
      server_side.setblocking(False)  # noqa: FBT003
      with pytest.raises(BlockingIOError):
        server_side.recv(1)
    finally:
      handler.sock = None
      client_side.close()
      server_side.close()

  def test_send_failure_drops_the_socket_and_returns_false(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG)

    class _FailingSocket:
      def __init__(self) -> None:
        self.closed = False

      def sendall(self, _data: bytes) -> None:
        raise OSError("broken pipe")

      def close(self) -> None:
        self.closed = True

    fake_sock = _FailingSocket()
    handler.sock = fake_sock  # pyright: ignore[reportAttributeAccessIssue]
    entry = HistoryEntry(id=_FIRST_ID, created=0.0, record=_make_record())

    result = handler._transmit(entry)  # pyright: ignore[reportPrivateUsage]

    assert result is False
    assert handler.sock is None
    assert fake_sock.closed is True


class TestEmergencyMode:
  def test_enters_emergency_mode_once_both_thresholds_are_exceeded(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=0.0, emergency_attempt_threshold=1)
    handler.record_failure()
    handler._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    handler.maybe_enter()

    assert handler.writer is not None

  def test_stays_out_of_emergency_mode_below_thresholds(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=999.0, emergency_attempt_threshold=999)
    handler.record_failure()

    handler.maybe_enter()

    assert handler.writer is None

  def test_record_success_closes_the_writer_and_resets_failures(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=0.0, emergency_attempt_threshold=1)
    handler.record_failure()
    handler._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    handler.maybe_enter()
    writer = handler.writer
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))

    handler.record_success()

    assert handler.writer is None
    assert handler._consecutive_failures == 0  # pyright: ignore[reportPrivateUsage]
    assert writer_closed == [True]


class TestClose:
  def test_close_closes_id_checkpoint_and_any_active_emergency_writer(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=0.0, emergency_attempt_threshold=1)
    checkpoint_closed: list[bool] = []
    monkeypatch.setattr(
      handler._id_checkpoint,  # pyright: ignore[reportPrivateUsage]
      "close",
      lambda: checkpoint_closed.append(True),
    )
    handler.record_failure()
    handler._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    handler.maybe_enter()
    writer = handler.writer
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))
    handler.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    handler.close()

    assert checkpoint_closed == [True]
    assert writer_closed == [True]
    assert handler.writer is None
    assert handler.sock is None


class TestFlushResolution:
  def test_bare_flush_resolves_to_the_no_op_handler_flush_not_record_durability(self):
    """Pins the MRO order this refactor depends on: SocketHandler before RecordDurability/EmergencyModeTracker."""
    assert HandshakeSocketHandler.flush is logging.Handler.flush
    mro_names = [cls.__name__ for cls in HandshakeSocketHandler.__mro__]
    assert mro_names.index("SocketHandler") < mro_names.index("RecordDurability")
    assert mro_names.index("SocketHandler") < mro_names.index("EmergencyModeTracker")


class TestCreateSocket:
  def test_a_real_connection_triggers_the_handshake_exactly_once(
    self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
  ):
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    monkeypatch.setattr(client_mod.settings, "persisted_dir_loc", persist_dir)
    monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "hist")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    handler = HandshakeSocketHandler("prog", _REACHABLE_CONFIG, host="127.0.0.1", port=port)
    handshake_calls: list[bool] = []
    monkeypatch.setattr(handler, "_send_handshake", lambda: handshake_calls.append(True))

    try:
      handler.createSocket()

      assert handshake_calls == [True]
      assert handler.sock is not None
    finally:
      handler.close()
      listener.close()


