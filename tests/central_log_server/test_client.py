"""Tests for `aeth_ext.central_log_server.client.HandshakeSocketHandler` and helpers."""

# Standard library imports
import base64
import logging
import socket
from typing import TYPE_CHECKING, Any

# Third party imports
import cloudpickle
import pytest

# First party imports
from aeth_ext.central_log_server import client as client_mod
from aeth_ext.central_log_server.client import HandshakeSocketHandler, make_definition
from aeth_ext.central_log_server.client.history import RecordHistoryBuffer
from aeth_ext.central_log_server.protocol import HandshakeAck, encode_json_packet
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path

  # First party imports
  from aeth_ext.central_log_server.client.history import HistoryEntry

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
  monkeypatch.setattr(RecordHistoryBuffer, "history_dir", tmp_path / "hist")
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
    monkeypatch.setattr(handler._history, "append", appended.append)  # pyright: ignore[reportPrivateUsage]

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


class TestReadAck:
  def test_parses_valid_ack(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG)
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID)))

      ack = handler._read_ack(client_side)  # pyright: ignore[reportPrivateUsage]

      assert ack is not None
      assert ack.ok is True
      assert ack.last_record_id == _ACK_LAST_ID
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
  def test_malformed_ack_returns_none(self, make_handler: Callable[..., HandshakeSocketHandler], payload: bytes):
    handler = make_handler(_REACHABLE_CONFIG)
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)

      assert handler._read_ack(client_side) is None  # pyright: ignore[reportPrivateUsage]
    finally:
      client_side.close()
      server_side.close()

  def test_peer_hangup_returns_none(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG)
    client_side, server_side = socket.socketpair()
    try:
      server_side.close()

      assert handler._read_ack(client_side) is None  # pyright: ignore[reportPrivateUsage]
    finally:
      client_side.close()


class TestSendHandshake:
  def test_rejection_records_error_and_drops_socket(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG)
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=False, error="config invalid")))
      handler.sock = client_side

      handler._send_handshake()  # pyright: ignore[reportPrivateUsage]

      assert handler._handshake_rejected == "config invalid"  # pyright: ignore[reportPrivateUsage]
      assert handler.sock is None
    finally:
      server_side.close()

  def test_successful_ack_triggers_backlog_replay(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG)
    find_after_calls: list[tuple[int | None, float | None]] = []

    def fake_find_after(last_id: int | None, hint_created: float | None) -> tuple[()]:
      find_after_calls.append((last_id, hint_created))
      return ()

    monkeypatch.setattr(handler._history, "find_after", fake_find_after)  # pyright: ignore[reportPrivateUsage]
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
