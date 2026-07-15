"""Tests for `aeth_ext.central_log_server.server.reader_server`."""

# Standard library imports
import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Third party imports
import orjson
import pytest
from aiologic import Queue, QueueEmpty

# First party imports
from aeth_ext.central_log_server.protocol import LENGTH_STRUCT, encode_json_packet, record_to_payload
from aeth_ext.central_log_server.server.dispatch import RegisterClient, UnregisterClient, shutdown_hierarchy
from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry, ClientIdState
from aeth_ext.central_log_server.server.reader_server import LogRecordServer
from aeth_ext.logging.bases import TaggedLogRecord
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # First party imports
  from aeth_ext.central_log_server.server.dispatch import WriterItem

_GET_TIMEOUT = 5.0
_LAST_RECORD_ID = 41
_VALID_CONFIG: dict[str, Any] = {"version": 1, "root": {"level": "DEBUG"}}
_INVALID_CONFIG: dict[str, Any] = {
  "version": 1,
  "handlers": {"bad": {"class": "not.a.real.module.Handler"}},
  "root": {"handlers": ["bad"]},
}


class TestDecodeHandshake:
  def test_valid_payload(self):
    payload = orjson.dumps({"program_name": "prog", "config": _VALID_CONFIG})

    handshake = LogRecordServer._decode_handshake(payload)  # pyright: ignore[reportPrivateUsage]

    assert handshake is not None
    assert handshake.program_name == "prog"
    assert handshake.config == _VALID_CONFIG

  @pytest.mark.parametrize(
    "payload",
    [
      pytest.param(b"not json", id="malformed json"),
      pytest.param(orjson.dumps([1, 2, 3]), id="non-dict"),
      pytest.param(orjson.dumps({"unexpected": "keys"}), id="wrong keys"),
    ],
  )
  def test_malformed_payload_returns_none(self, payload: bytes):
    assert LogRecordServer._decode_handshake(payload) is None  # pyright: ignore[reportPrivateUsage]


async def _read_packet(reader: asyncio.StreamReader) -> dict[str, Any]:
  header = await reader.readexactly(LENGTH_STRUCT.size)
  payload = await reader.readexactly(LENGTH_STRUCT.unpack(header)[0])
  decoded = orjson.loads(payload)
  assert isinstance(decoded, dict)
  return decoded


async def _get(queue: Queue[WriterItem]) -> WriterItem:
  return await asyncio.wait_for(queue.async_get(), timeout=_GET_TIMEOUT)


class _Client:
  """A raw TCP client speaking the length-prefixed JSON protocol."""

  def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    self.reader = reader
    self.writer = writer

  async def send(self, obj: Any) -> None:
    self.writer.write(encode_json_packet(obj))
    await self.writer.drain()

  async def close(self) -> None:
    self.writer.close()
    await self.writer.wait_closed()


class _ServerHarness:
  """Runs a `LogRecordServer` on an ephemeral port for a single test."""

  def __init__(self, log_dir: Path) -> None:
    self.queue: Queue[WriterItem] = Queue()
    self.id_registry = ClientIdRegistry()
    self.server = LogRecordServer(self.queue, self.id_registry, host="127.0.0.1", port=0, log_dir=log_dir)
    self.tcp_server: asyncio.Server | None = None

  async def __aenter__(self) -> "_ServerHarness":
    self.tcp_server = await self.server.start_server()
    return self

  async def __aexit__(self, *exc_info: object) -> None:
    assert self.tcp_server is not None
    self.tcp_server.close()
    await self.tcp_server.wait_closed()

  async def connect(self) -> _Client:
    assert self.tcp_server is not None
    port: int = self.tcp_server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return _Client(reader, writer)


class TestHandshakeFlow:
  def test_happy_path_registers_streams_and_unregisters(self, tmp_path: Path):
    async def scenario() -> None:
      async with _ServerHarness(tmp_path) as harness:
        client = await harness.connect()
        await client.send({"program_name": "prog", "config": _VALID_CONFIG})

        ack = await _read_packet(client.reader)
        assert ack["ok"] is True
        assert ack["last_record_id"] is None
        assert ack["last_received_at"] is None

        register = await _get(harness.queue)
        assert isinstance(register, RegisterClient)
        assert register.program_name == "prog"
        shutdown_hierarchy(register.manager, register.root)

        record = TaggedLogRecord("prog.module", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        await client.send(record_to_payload(record))
        received = await _get(harness.queue)
        assert isinstance(received, logging.LogRecord)
        assert received.getMessage() == "hello world"
        assert received.source_name == "prog"

        await client.close()
        unregister = await _get(harness.queue)
        assert isinstance(unregister, UnregisterClient)
        assert unregister.program_name == "prog"
        assert unregister.connection_id == register.connection_id

    asyncio.run(scenario())

  def test_ack_reports_persisted_resume_state(self, tmp_path: Path):
    async def scenario() -> None:
      async with _ServerHarness(tmp_path) as harness:
        last_received = datetime.now(BaseSettings.get_settings().tz)
        harness.id_registry._states["prog"] = ClientIdState(_LAST_RECORD_ID, last_received)  # pyright: ignore[reportPrivateUsage]

        client = await harness.connect()
        await client.send({"program_name": "prog", "config": _VALID_CONFIG})

        ack = await _read_packet(client.reader)
        assert ack["ok"] is True
        assert ack["last_record_id"] == _LAST_RECORD_ID
        assert ack["last_received_at"] == pytest.approx(last_received.timestamp())

        register = await _get(harness.queue)
        assert isinstance(register, RegisterClient)
        shutdown_hierarchy(register.manager, register.root)
        await client.close()

    asyncio.run(scenario())

  def test_invalid_config_is_rejected_before_registration(self, tmp_path: Path):
    async def scenario() -> None:
      async with _ServerHarness(tmp_path) as harness:
        client = await harness.connect()
        await client.send({"program_name": "prog", "config": _INVALID_CONFIG})

        ack = await _read_packet(client.reader)
        assert ack["ok"] is False
        assert "remote logging config rejected" in ack["error"]

        # The server closes the connection without ever registering.
        assert await client.reader.read() == b""
        with pytest.raises(QueueEmpty):
          harness.queue.green_get(blocking=False)
        await client.close()

    asyncio.run(scenario())

  def test_malformed_first_packet_is_rejected(self, tmp_path: Path):
    async def scenario() -> None:
      async with _ServerHarness(tmp_path) as harness:
        client = await harness.connect()
        await client.send([1, 2, 3])

        ack = await _read_packet(client.reader)
        assert ack["ok"] is False
        assert ack["error"] == "invalid handshake"
        with pytest.raises(QueueEmpty):
          harness.queue.green_get(blocking=False)
        await client.close()

    asyncio.run(scenario())

  def test_malformed_records_eventually_drop_the_connection(self, tmp_path: Path):
    async def scenario() -> None:
      async with _ServerHarness(tmp_path) as harness:
        client = await harness.connect()
        await client.send({"program_name": "prog", "config": _VALID_CONFIG})
        await _read_packet(client.reader)  # ack
        register = await _get(harness.queue)
        assert isinstance(register, RegisterClient)
        shutdown_hierarchy(register.manager, register.root)

        for _ in range(LogRecordServer.MAX_MALFORMED_PACKETS):
          client.writer.write(LENGTH_STRUCT.pack(len(b"not json")) + b"not json")
        await client.writer.drain()

        # The server drops the connection and unregisters the program.
        unregister = await _get(harness.queue)
        assert isinstance(unregister, UnregisterClient)
        assert await client.reader.read() == b""
        await client.close()

    asyncio.run(scenario())
