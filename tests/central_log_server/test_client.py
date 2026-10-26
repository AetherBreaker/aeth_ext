# Standard library imports
import asyncio
import base64
import logging
import queue
import socket
import threading
from contextlib import suppress
from time import monotonic, time
from typing import TYPE_CHECKING, Any, override

# Third party imports
import cloudpickle
import orjson
import pytest

# First party imports
from aeth_ext.central_log_server import client as client_mod
from aeth_ext.central_log_server.client import HandshakeSocketHandler, emergency as emergency_mod, make_definition
from aeth_ext.central_log_server.client.durability import RecordDurability
from aeth_ext.central_log_server.client.history import EmergencyHistoryWriter, HistoryEntry, RecordHistoryBuffer
from aeth_ext.central_log_server.protocol import ApplyFailure, ApplySuccess, HandshakeAck, encode_json_packet
from aeth_ext.logging.bases import TaggedLogRecord
from tests.conftest import wait_until

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Generator
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
def make_handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[Callable[..., HandshakeSocketHandler]]:
  persist_dir = tmp_path / "persist"
  persist_dir.mkdir()
  monkeypatch.setattr(client_mod.settings, "persisted_dir_loc", persist_dir)
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "hist")
  created: list[HandshakeSocketHandler] = []

  def factory(config: dict[str, Any], *, port: int = 1, **kwargs: Any) -> HandshakeSocketHandler:
    handler = HandshakeSocketHandler("prog", config, host="127.0.0.1", port=port, **kwargs)
    created.append(handler)
    return handler

  yield factory

  for handler in created:
    handler.close()
    assert wait_until(lambda h=handler: not h._sender.is_alive()), "a sender thread outlived its handler"  # pyright: ignore[reportPrivateUsage]


def _read_frame(conn: socket.socket) -> bytes | None:
  try:
    header = client_mod._recv_exact(conn, client_mod.LENGTH_STRUCT.size)  # pyright: ignore[reportPrivateUsage]
    return None if header is None else client_mod._recv_exact(conn, client_mod.LENGTH_STRUCT.unpack(header)[0])  # pyright: ignore[reportPrivateUsage]
  except OSError:
    return None


class _FakeLogServer:
  """Just enough of the log server's side of the handshake protocol; keeps the ids of the records it receives, in order.

  A good ack names the highest id received so far, or ``acked_through`` when set. ``ack_ok=False`` sends a malformed
  ack, ``close_after_ack`` hangs up right after acking, ``drop_first_after`` closes the first connection after that many
  records (its unread data turns the close into an RST), and ``stall`` accepts and never reads.
  """

  def __init__(
    self,
    *,
    stall: bool = False,
    ack_ok: bool = True,
    close_after_ack: bool = False,
    drop_first_after: int | None = None,
    acked_through: int | None = None,
    acked_at: float | None = None,
  ) -> None:
    self.stall = stall
    self.ack_ok = ack_ok
    self.close_after_ack = close_after_ack
    self.drop_first_after = drop_first_after
    self.acked_through = acked_through
    self.acked_at = acked_at
    self.ids: list[int] = []
    self.acks: list[bool] = []  # per handshake: whether its ack was a good one
    self.connections = 0
    self.first_dropped = threading.Event()
    self._created: dict[int, float] = {}
    self._lock = threading.Lock()
    self._stopped = threading.Event()
    self._conns: list[socket.socket] = []
    self._threads: list[threading.Thread] = []
    self._listener = socket.create_server(("127.0.0.1", 0))
    self.port: int = self._listener.getsockname()[1]
    self._start(self._accept)

  def _start(self, target: Callable[..., None], *args: object) -> None:
    thread = threading.Thread(target=target, args=args, daemon=True)
    self._threads.append(thread)
    thread.start()

  def _accept(self) -> None:
    while True:
      try:
        conn, _ = self._listener.accept()
      except OSError:
        return
      with self._lock:
        self.connections += 1
        self._conns.append(conn)
        index = self.connections
      self._start(self._serve, conn, index)

  def _serve(self, conn: socket.socket, index: int) -> None:
    with conn:
      if self.stall:
        self._stopped.wait()
        return
      if _read_frame(conn) is None:
        return
      with self._lock:
        self.acks.append(self.ack_ok)
        last_id = self.acked_through if self.acked_through is not None else max(self.ids, default=None)
        last_at = self.acked_at if self.acked_through is not None else self._created.get(last_id or 0)
      if self.ack_ok:
        conn.sendall(encode_json_packet(HandshakeAck(ok=True, last_record_id=last_id, last_received_at=last_at)))
      else:
        conn.sendall(client_mod.LENGTH_STRUCT.pack(len(b"garbage")) + b"garbage")
      if self.close_after_ack:
        return
      received = 0
      while (frame := _read_frame(conn)) is not None:
        payload = orjson.loads(frame)
        with self._lock:
          self.ids.append(payload["record_id"])
          self._created[payload["record_id"]] = payload["created"]
        received += 1
        if index == 1 and received == self.drop_first_after:
          self.first_dropped.set()
          return

  def stop(self) -> None:
    self._stopped.set()
    self._listener.close()
    with self._lock:
      conns = list(self._conns)
    for conn in conns:
      with suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)
    for thread in self._threads:
      thread.join(timeout=5.0)


@pytest.fixture
def fake_server() -> Generator[Callable[..., _FakeLogServer]]:
  servers: list[_FakeLogServer] = []

  def factory(**kwargs: Any) -> _FakeLogServer:
    server = _FakeLogServer(**kwargs)
    servers.append(server)
    return server

  yield factory

  for server in servers:
    server.stop()


class _CountingDoorbell(queue.SimpleQueue[None]):
  def __init__(self) -> None:
    self.rings = 0

  @override
  def put(self, item: None, block: bool = True, timeout: float | None = None) -> None:
    self.rings += 1
    super().put(item, block, timeout)


class TestMakeDefinition:
  def test_round_trips_via_cloudpickle(self) -> None:
    encoded = make_definition(_sample_factory)

    decoded = cloudpickle.loads(base64.b64decode(encoded))

    assert decoded() == "made by _sample_factory"

  def test_result_is_ascii_text(self) -> None:
    encoded = make_definition(_sample_factory)

    assert isinstance(encoded, str)
    assert encoded.isascii()


class TestEmitPreFilter:
  def test_undeliverable_record_never_consumes_an_id(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_UNREACHABLE_CONFIG)
    doorbell = _CountingDoorbell()
    monkeypatch.setattr(handler, "_doorbell", doorbell)
    appended: list[HistoryEntry] = []
    monkeypatch.setattr(handler.history, "append", appended.append)

    handler.emit(_make_record(logging.CRITICAL))

    assert appended == []
    assert doorbell.rings == 0
    assert handler._next_id == _FIRST_ID  # pyright: ignore[reportPrivateUsage]

  def test_deliverable_record_gets_an_id_a_history_entry_and_a_doorbell_ring(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    doorbell = _CountingDoorbell()
    monkeypatch.setattr(handler, "_doorbell", doorbell)
    appended: list[HistoryEntry] = []
    monkeypatch.setattr(handler.history, "append", appended.append)
    record = _make_record()

    handler.emit(record)

    (entry,) = appended
    assert entry.id == _FIRST_ID
    assert record.record_id == _FIRST_ID
    assert doorbell.rings == 1
    assert handler._next_id == _FIRST_ID + 1  # pyright: ignore[reportPrivateUsage]

  def test_records_below_reachable_level_are_dropped(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_ERROR_ONLY_CONFIG)
    appended: list[HistoryEntry] = []
    monkeypatch.setattr(handler.history, "append", appended.append)

    handler.emit(_make_record(logging.INFO))
    handler.emit(_make_record(logging.ERROR))

    (entry,) = appended
    assert entry.record.levelno == logging.ERROR


class TestReadServerMessageSync:
  def test_parses_valid_ack(self) -> None:
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

  def test_parses_apply_failure(self) -> None:
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
  def test_malformed_payload_returns_none(self, payload: bytes) -> None:
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)

      assert client_mod.read_server_message_sync(client_side) is None
    finally:
      client_side.close()
      server_side.close()

  def test_peer_hangup_returns_none(self) -> None:
    client_side, server_side = socket.socketpair()
    try:
      server_side.close()

      assert client_mod.read_server_message_sync(client_side) is None
    finally:
      client_side.close()

  def test_a_socket_its_owner_already_closed_returns_none(self) -> None:
    client_side, server_side = socket.socketpair()
    server_side.close()
    client_side.close()  # as the sender does on a failed send, before the apply-result watcher reads

    assert client_mod.read_server_message_sync(client_side, timeout=None) is None


class TestReadServerMessageAsync:
  async def test_parses_valid_ack(self) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(encode_json_packet(HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID)))
    reader.feed_eof()

    message = await client_mod.read_server_message_async(reader)

    assert isinstance(message, HandshakeAck)
    assert message.ok is True
    assert message.last_record_id == _ACK_LAST_ID

  async def test_parses_apply_failure(self) -> None:
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
  async def test_malformed_payload_returns_none(self, payload: bytes) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)
    reader.feed_eof()

    assert await client_mod.read_server_message_async(reader) is None

  async def test_peer_hangup_returns_none(self) -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()

    assert await client_mod.read_server_message_async(reader) is None


class TestSendHandshake:
  def test_rejection_records_error_and_drops_socket(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    rejections: list[str] = []
    monkeypatch.setattr(client_mod, "trigger_shutdown", lambda reason, details, **_kw: rejections.append(details))
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=False, error="config invalid")))
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- held around every handshake, as by the sender
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
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    ack_failures: list[str] = []
    alert_kwargs: list[dict[str, object]] = []
    monkeypatch.setattr(
      client_mod,
      "alert",
      lambda _reason, details, **kw: (ack_failures.append(details), alert_kwargs.append(kw)),
    )
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
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- held around every handshake, as by the sender
        handler.sock = client_side
        handler._send_handshake()  # pyright: ignore[reportPrivateUsage]

      assert len(ack_failures) == 1
      assert "prog" in ack_failures[0]
      assert handler._handshake_rejected is None  # pyright: ignore[reportPrivateUsage]
      assert alert_kwargs[0].get("in_except_block") is False
      assert alert_kwargs[0].get("wait") is False  # holding `_sock_lock`: must not wait on the send
    finally:
      client_side.close()
      server_side.close()

  def test_failed_ack_read_continues_after_what_this_process_already_sent(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(client_mod, "alert", lambda *_a, **_kw: None)
    for _ in range(3):
      handler.emit(_make_record())
    client_side, server_side = socket.socketpair()
    try:
      server_side.shutdown(socket.SHUT_WR)  # no ack; see the test above
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage]
        handler.mark_sent(_FIRST_ID)
        handler.sock = client_side
        handler._send_handshake()  # pyright: ignore[reportPrivateUsage]

        assert handler.last_sent_id == _FIRST_ID, "records 2 and 3 were never sent, so they must not be skipped"
    finally:
      client_side.close()
      server_side.close()

  def test_failed_ack_read_still_starts_apply_result_watcher(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(client_mod, "alert", lambda *_a, **_kw: None)
    rejections: list[str] = []
    monkeypatch.setattr(client_mod, "trigger_shutdown", lambda reason, details, **_kw: rejections.append(details))
    client_side, server_side = socket.socketpair()
    try:
      # A malformed (rather than absent) ack leaves the socket fully open on both ends,
      # unlike the SHUT_WR case above -- needed here so the server can still send a
      # real ApplyFailure afterward for the watcher to catch.
      garbage = b"garbage"
      server_side.sendall(client_mod.LENGTH_STRUCT.pack(len(garbage)) + garbage)
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- held around every handshake, as by the sender
        handler.sock = client_side
        handler._send_handshake()  # pyright: ignore[reportPrivateUsage]
      server_side.sendall(encode_json_packet(ApplyFailure(error="disk full")))

      deadline = monotonic() + 5.0
      while not rejections and monotonic() < deadline:
        pass
      assert len(rejections) == 1
      assert "prog" in rejections[0]
      assert "disk full" in rejections[0]
    finally:
      handler.sock = None
      client_side.close()
      server_side.close()

  def test_successful_ack_triggers_backlog_replay(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
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
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- held around every handshake, as by the sender
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
  _WATCH_TIMEOUT = 5.0

  def test_apply_failure_triggers_config_rejected(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(handler.history, "find_after", lambda *_a: ())
    rejections: list[str] = []
    monkeypatch.setattr(client_mod, "trigger_shutdown", lambda reason, details, **_kw: rejections.append(details))
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=True)))
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- held around every handshake, as by the sender
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
  ) -> None:
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
      with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- held around every handshake, as by the sender
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
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)

    def fake_create_socket() -> None:
      handler._handshake_rejected = "bad remote config"  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(handler, "createSocket", fake_create_socket)

    with pytest.raises(RuntimeError, match="bad remote config"):
      handler.connect_and_verify()

  def test_unreachable_server_is_not_an_error(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    monkeypatch.setattr(handler, "createSocket", lambda: None)

    handler.connect_and_verify()

    assert handler.sock is None


class TestEmergencyMode:
  def test_enters_emergency_mode_once_both_thresholds_are_exceeded(self, make_handler: Callable[..., HandshakeSocketHandler]) -> None:
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=0.0, emergency_attempt_threshold=1)
    handler.record_failure()
    handler._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    handler.maybe_enter()

    assert handler.writer is not None

  def test_stays_out_of_emergency_mode_below_thresholds(self, make_handler: Callable[..., HandshakeSocketHandler]) -> None:
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=999.0, emergency_attempt_threshold=999)
    handler.record_failure()

    handler.maybe_enter()

    assert handler.writer is None

  def test_record_success_closes_the_writer_and_resets_failures(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
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
  ) -> None:
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
  def test_bare_flush_resolves_to_the_no_op_handler_flush_not_record_durability(self) -> None:
    assert HandshakeSocketHandler.flush is logging.Handler.flush
    mro_names = [cls.__name__ for cls in HandshakeSocketHandler.__mro__]
    assert mro_names.index("SocketHandler") < mro_names.index("RecordDurability")
    assert mro_names.index("SocketHandler") < mro_names.index("EmergencyModeTracker")


class TestCreateSocket:
  def test_a_real_connection_triggers_the_handshake_exactly_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
      # stdlib makeSocket leaves the 1s *connect* timeout on the socket, so any send that
      # stalls >1s silently drops the connection; a connected socket gets its own budget.
      assert handler.sock.gettimeout() == HandshakeSocketHandler.SEND_TIMEOUT
    finally:
      handler.close()
      listener.close()


class TestSender:
  def test_emit_never_touches_the_socket(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    callers: list[threading.Thread] = []
    monkeypatch.setattr(handler, "createSocket", lambda: callers.append(threading.current_thread()))

    handler.emit(_make_record())

    assert wait_until(lambda: bool(callers))
    assert threading.current_thread() not in callers

  def test_emit_returns_fast_when_the_server_stalls(
    self, make_handler: Callable[..., HandshakeSocketHandler], fake_server: Callable[..., _FakeLogServer]
  ) -> None:
    server = fake_server(stall=True)
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)

    start = monotonic()
    for _ in range(200):
      handler.emit(_make_record())
    elapsed = monotonic() - start

    assert elapsed < 0.5  # noqa: PLR2004
    assert wait_until(lambda: server.connections >= 1)

  @pytest.mark.parametrize("max_history_records", [50_000, 7], ids=["in memory", "spilling to disk"])
  def test_reconnect_delivers_every_record_once_in_order(
    self, make_handler: Callable[..., HandshakeSocketHandler], fake_server: Callable[..., _FakeLogServer], max_history_records: int
  ) -> None:
    server = fake_server(drop_first_after=50)
    handler = make_handler(_REACHABLE_CONFIG, port=server.port, max_history_records=max_history_records)

    for _ in range(100):
      handler.emit(_make_record())
    assert server.first_dropped.wait(timeout=5.0)
    # Sent after the drop, so the sender meets the reset and reconnects; the second ack names id 50.
    for _ in range(200):
      handler.emit(_make_record())

    assert wait_until(lambda: len(server.ids) >= 300, timeout=15.0), server.ids[-5:]  # noqa: PLR2004
    handler.close()
    assert server.ids == list(range(1, 301))

  def test_a_multi_day_outage_delivers_every_retained_record_once_in_order(
    self, make_handler: Callable[..., HandshakeSocketHandler], fake_server: Callable[..., _FakeLogServer]
  ) -> None:
    now = time()
    days = [now - 3 * 86_400, now - 2 * 86_400, now - 86_400, now]
    # The server already holds ids 1 and 2, both from the outage's first day.
    server = fake_server(acked_through=2, acked_at=days[0])
    handler = make_handler(_REACHABLE_CONFIG, port=server.port, max_history_records=3)
    with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- the outage: no connecting until released
      for created in days:
        for _ in range(4):
          record = _make_record()
          record.created = created
          handler.emit(record)

    assert wait_until(lambda: len(server.ids) >= 14, timeout=10.0), server.ids  # noqa: PLR2004
    handler.close()
    assert server.ids == list(range(3, 17))

  def test_reentrant_emit_on_the_same_thread_neither_blocks_nor_sends(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    connecting_threads: list[threading.Thread] = []
    monkeypatch.setattr(handler, "createSocket", lambda: connecting_threads.append(threading.current_thread()))
    original_append = handler.history.append

    def append_then_log_again(entry: HistoryEntry) -> None:
      original_append(entry)
      if entry.id == _FIRST_ID:
        handler.handle(_make_record(logging.WARNING))  # as a signal handler that logs would, mid-emit

    monkeypatch.setattr(handler.history, "append", append_then_log_again)
    emitter = threading.Thread(target=handler.handle, args=(_make_record(),))

    emitter.start()
    emitter.join(timeout=5.0)

    assert not emitter.is_alive(), "the nested emit deadlocked"
    assert handler._next_id == _FIRST_ID + 1  # pyright: ignore[reportPrivateUsage]
    assert "prog.module WARNING: hello" in capsys.readouterr().err, "the nested record was lost instead of diverted"
    assert wait_until(lambda: bool(connecting_threads))
    assert emitter not in connecting_threads

  def test_close_is_bounded_when_the_server_stalls(
    self, make_handler: Callable[..., HandshakeSocketHandler], fake_server: Callable[..., _FakeLogServer]
  ) -> None:
    server = fake_server(stall=True)
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)
    for _ in range(50):
      handler.emit(_make_record())
    assert wait_until(lambda: server.connections >= 1)

    start = monotonic()
    handler.close()

    assert monotonic() - start < 4.0  # noqa: PLR2004

  def test_close_twice_is_safe(self, make_handler: Callable[..., HandshakeSocketHandler]) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    handler.emit(_make_record())
    handler.close()

    start = monotonic()
    handler.close()

    assert monotonic() - start < 1.0
    assert not handler._sender.is_alive()  # pyright: ignore[reportPrivateUsage]

  def test_close_from_the_sender_thread_does_not_join_itself(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
  ) -> None:
    handler = make_handler(_REACHABLE_CONFIG)
    closed = threading.Event()

    def close_from_the_sender() -> None:
      handler.close()
      closed.set()

    monkeypatch.setattr(handler, "createSocket", close_from_the_sender)

    handler.emit(_make_record())

    assert closed.wait(timeout=5.0)
    assert wait_until(lambda: not handler._sender.is_alive())  # pyright: ignore[reportPrivateUsage]
    assert "log sender" not in capsys.readouterr().err

  def test_ack_read_failure_alerts_once_per_outage(
    self,
    make_handler: Callable[..., HandshakeSocketHandler],
    fake_server: Callable[..., _FakeLogServer],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    ack_alerts: list[str] = []
    monkeypatch.setattr(
      client_mod, "alert", lambda reason, _details, **_kw: ack_alerts.append(reason) if "handshake ack" in reason else None
    )
    server = fake_server(ack_ok=False, close_after_ack=True)
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)

    def emitting_until(predicate: Callable[[], bool]) -> bool:
      # Each send meets the closed connection, so the sender reconnects for as long as records keep coming.
      return wait_until(lambda: (handler.emit(_make_record()), predicate())[1], timeout=15.0)

    assert emitting_until(lambda: server.acks.count(False) >= 3)  # noqa: PLR2004
    assert len(ack_alerts) == 1
    server.ack_ok = True
    assert emitting_until(lambda: True in server.acks)
    server.ack_ok = False
    first_good = server.acks.index(True)
    assert emitting_until(lambda: False in server.acks[first_good + 1 :])
    assert wait_until(lambda: len(ack_alerts) == 2), "an ack that reads ends the outage, so the next failure alerts again"  # noqa: PLR2004

  def test_first_connect_without_an_ack_skips_the_backlog(
    self,
    make_handler: Callable[..., HandshakeSocketHandler],
    fake_server: Callable[..., _FakeLogServer],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    monkeypatch.setattr(client_mod, "alert", lambda *_a, **_kw: None)
    server = fake_server(ack_ok=False)
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)
    with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- holds the sender off until these are recorded
      for _ in range(3):
        handler.emit(_make_record())
    assert wait_until(lambda: handler.last_sent_id == 3)  # noqa: PLR2004

    handler.emit(_make_record())
    handler.emit(_make_record())

    assert wait_until(lambda: len(server.ids) >= 2)  # noqa: PLR2004
    handler.close()
    assert server.ids == [4, 5]

  def test_a_good_ack_with_an_empty_backlog_does_not_resend_earlier_history(
    self, make_handler: Callable[..., HandshakeSocketHandler], fake_server: Callable[..., _FakeLogServer]
  ) -> None:
    earlier = make_handler(_REACHABLE_CONFIG)
    for _ in range(5):
      earlier.emit(_make_record())
    earlier.close()  # its history is on disk and the id checkpoint holds 5
    server = fake_server(acked_through=5, acked_at=time())
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)
    handler.connect_and_verify()

    handler.emit(_make_record())

    assert wait_until(lambda: bool(server.ids))
    handler.close()
    assert server.ids == [6]
    assert server.connections == 1, "the sender found the socket connect_and_verify made"

  def test_emergency_writer_entries_still_queued_at_reconnect_are_delivered(
    self,
    make_handler: Callable[..., HandshakeSocketHandler],
    fake_server: Callable[..., _FakeLogServer],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    gate = threading.Event()

    class _GatedWriter(EmergencyHistoryWriter):
      """Holds every entry in its queue until closed: a writer that has not caught up when the server comes back."""

      @override
      def _run(self) -> None:
        gate.wait()
        super()._run()

      @override
      def close(self) -> None:
        gate.set()
        super().close()

    monkeypatch.setattr(emergency_mod, "EmergencyHistoryWriter", _GatedWriter)
    server = fake_server()
    # The third record spills the first three while the writer still holds all four, so the spill writes them and the
    # writer, once released, skips them; the fourth stays in memory.
    handler = make_handler(
      _REACHABLE_CONFIG, port=server.port, max_history_records=3, emergency_time_threshold=0.0, emergency_attempt_threshold=1
    )
    with handler._sock_lock:  # pyright: ignore[reportPrivateUsage] -- the outage: no connecting until released
      handler.record_failure()
      handler.maybe_enter()
      assert isinstance(handler.writer, _GatedWriter)
      for _ in range(4):
        handler.emit(_make_record())

    assert wait_until(lambda: len(server.ids) >= 4)  # noqa: PLR2004
    handler.close()
    assert server.ids == [1, 2, 3, 4]

  def test_an_unencodable_record_is_skipped_rather_than_retried(
    self,
    make_handler: Callable[..., HandshakeSocketHandler],
    fake_server: Callable[..., _FakeLogServer],
    capsys: pytest.CaptureFixture[str],
  ) -> None:
    server = fake_server()
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)
    handler.connect_and_verify()

    handler.emit(TaggedLogRecord("prog.module", logging.INFO, __file__, 1, "%d apples", ("many",), None))
    handler.emit(_make_record())

    assert wait_until(lambda: bool(server.ids)), "delivery wedged behind the record it can't encode"
    handler.close()
    assert server.ids == [_FIRST_ID + 1]
    assert f"skipped record {_FIRST_ID}" in capsys.readouterr().err

  def test_a_history_gap_is_reported_once_and_delivery_resumes_after_it(
    self,
    make_handler: Callable[..., HandshakeSocketHandler],
    fake_server: Callable[..., _FakeLogServer],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
  ) -> None:
    server = fake_server()
    handler = make_handler(_REACHABLE_CONFIG, port=server.port)
    handler.connect_and_verify()
    for _ in range(3):
      handler.emit(_make_record())
    assert wait_until(lambda: server.ids == [1, 2, 3])
    # Id 3 ages out of history, and ids 4 and 5 are lost before they reach it.
    RecordDurability.flush(handler)
    for path in handler.history_dir.glob("*.jsonl"):
      path.unlink()
    original_append = handler.history.append
    monkeypatch.setattr(handler.history, "append", lambda entry: None if entry.id in {4, 5} else original_append(entry))
    original_find_after = handler.history.find_after
    lookups: list[int | None] = []

    def counting_find_after(last_id: int | None, hint_created: float | None) -> tuple[HistoryEntry, ...] | None:
      lookups.append(last_id)
      return original_find_after(last_id, hint_created)

    monkeypatch.setattr(handler.history, "find_after", counting_find_after)

    # Each lost record's ring finds the same gap and nothing retained after it: one lookup, one full read.
    handler.emit(_make_record())
    assert wait_until(lambda: len(lookups) >= 2)  # noqa: PLR2004
    handler.emit(_make_record())
    assert wait_until(lambda: len(lookups) >= 4)  # noqa: PLR2004
    handler.emit(_make_record())
    handler.emit(_make_record())

    assert wait_until(lambda: len(server.ids) >= 5)  # noqa: PLR2004
    handler.close()
    assert server.ids == [1, 2, 3, 6, 7]
    assert capsys.readouterr().err.count("gone from history") == 1
