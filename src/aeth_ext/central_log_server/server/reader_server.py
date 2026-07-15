# Standard library imports
import asyncio
import logging
import socket
from contextlib import suppress
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
import orjson

# First party imports
from aeth_ext.central_log_server.protocol import (
  LENGTH_STRUCT,
  ClientHandshake,
  HandshakeAck,
  encode_json_packet,
  make_log_record,
)

# Local imports
from aeth_ext.central_log_server.server.dispatch import RegisterClient, UnregisterClient, build_hierarchy
from aeth_ext.errors import FATAL_EVENT, handle_fatal_exc_async

if TYPE_CHECKING:
  # Third party imports
  from aiologic import Queue

  # First party imports
  # Local imports
  from aeth_ext.central_log_server.server.dispatch import WriterItem
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry

logger = logging.getLogger(__name__)


class LogRecordServer:
  """Single-server log receiver that fans records out to per-program hierarchies.

  Concurrency model (intentionally minimal for a resource-constrained vCPU):

  * the **main thread** runs an :mod:`asyncio` event loop that accepts every
    connection and reads its length-prefixed messages. The first message from a
    connection is its JSON
    :class:`~aeth_ext.central_log_server.protocol.ClientHandshake`, carrying the
    remote logging config the server applies into a private logging hierarchy
    dedicated to that program (built here, so an invalid config is rejected in
    the :class:`~aeth_ext.central_log_server.protocol.HandshakeAck` before any
    records flow); each later message is a log record, decoded into a
    :class:`~aeth_ext.logging.bases.TaggedLogRecord`, stamped with the
    program identity, and pushed onto the shared queue. On handshake it also
    enqueues a :class:`~aeth_ext.central_log_server.server.dispatch.RegisterClient`
    event and, when the connection is lost, an
    :class:`~aeth_ext.central_log_server.server.dispatch.UnregisterClient` event.
  * a **single writer thread** drains that queue as its sole owner: it applies
    the register/unregister events and dispatches every record into its
    program's private hierarchy through normal logging machinery. Because only
    that one thread touches the hierarchies no lock is needed, and teardown
    enqueued behind a program's records cannot drop anything in flight.
  """

  def __init__(
    self,
    queue: Queue[WriterItem],
    id_registry: ClientIdRegistry,
    host: str = "0.0.0.0",
    port: int = DEFAULT_TCP_LOGGING_PORT,
    log_dir: Path | str | None = None,
  ) -> None:
    super().__init__()
    self.host: str = host
    self.port: int = port
    self.log_dir: Path = Path(log_dir) if log_dir is not None else Path.cwd() / "logs"

    self._queue = queue
    self._id_registry = id_registry

  # -- lifecycle ------------------------------------------------------------

  async def start_server(self) -> asyncio.Server:
    """Bind the TCP socket and return the running server without blocking.

    The caller is responsible for keeping the server alive (e.g. by holding
    a reference and awaiting shutdown externally) and for closing it when done.
    """
    return await asyncio.start_server(self._handle_client, self.host, self.port)

  # -- asyncio reader (main thread) -----------------------------------------

  MAX_MALFORMED_PACKETS = 5  # drop a connection that sends this many bad messages in a row

  @staticmethod
  async def _read_packet(reader: asyncio.StreamReader) -> bytes | None:
    """Read one length-prefixed payload, or ``None`` if the peer hung up."""
    try:
      header = await reader.readexactly(LENGTH_STRUCT.size)
      return await reader.readexactly(LENGTH_STRUCT.unpack(header)[0])
    except asyncio.IncompleteReadError:
      return None

  @handle_fatal_exc_async
  async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    sock: socket.socket | None = writer.transport.get_extra_info("socket")
    if sock is not None:
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)  # seconds idle before first probe
      if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)  # seconds between probes
      if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)  # probes before giving up

    registered: RegisterClient | None = None
    connection_id = id(writer)
    try:
      payload = await self._read_packet(reader)
      if payload is None:
        return

      logger.info("New client connection from %s", writer.transport.get_extra_info("peername"))

      handshake = self._decode_handshake(payload)
      if handshake is None:
        await self._send_ack(writer, HandshakeAck(ok=False, error="invalid handshake"), "<unidentified client>")
        return

      logger.info("Handshake received from %s", handshake.program_name)

      # Build the program's private hierarchy *before* acking so an invalid
      # remote config is rejected fail-fast at handshake time.
      try:
        manager, root = build_hierarchy(handshake.config, self.log_dir / handshake.program_name)
      except Exception as e:
        logger.warning("Rejecting %r: remote logging config could not be applied", handshake.program_name, exc_info=e)
        await self._send_ack(
          writer,
          HandshakeAck(ok=False, error=f"remote logging config rejected: {e}"),
          handshake.program_name,
        )
        return

      # Tell the client the last record we've ever seen from this program (if
      # any) so it can resume sending immediately after that id instead of
      # resending everything it still has buffered.
      last_state = await self._id_registry.get(handshake.program_name)
      ack = HandshakeAck(
        ok=True,
        last_record_id=last_state.last_record_id if last_state else None,
        last_received_at=last_state.last_received_at.timestamp() if last_state else None,
      )
      if not await self._send_ack(writer, ack, handshake.program_name):
        return

      # Hand the hierarchy to the writer thread before any of this program's
      # records are dispatched.
      registered = RegisterClient(handshake.program_name, manager, root, connection_id)
      await self._queue.async_put(registered)
      await self._receive_records(reader, handshake)

    finally:
      # Enqueued behind every record already sent, so the writer tears the
      # program's hierarchy down only after those records have been flushed.
      if registered is not None:
        await self._queue.async_put(UnregisterClient(registered.program_name, connection_id))

      writer.close()
      with suppress(OSError, asyncio.CancelledError):
        await writer.wait_closed()

  @staticmethod
  def _decode_handshake(payload: bytes) -> ClientHandshake | None:
    """Decode and validate the first packet of a connection, or ``None`` if malformed."""
    try:
      obj: object = orjson.loads(payload)
      if not isinstance(obj, dict):
        raise TypeError(f"expected a JSON object, got {type(obj).__name__}")
      return ClientHandshake(**obj)
    except Exception as e:
      logger.warning("Client sent a malformed packet when a handshake was expected. Closing connection...", exc_info=e)
      return None

  @staticmethod
  async def _send_ack(writer: asyncio.StreamWriter, ack: HandshakeAck, program_name: str) -> bool:
    """Best-effort send of the handshake ack; returns whether it went out."""
    try:
      writer.write(encode_json_packet(ack))
      await writer.drain()
    except OSError:
      logger.warning("Failed to send handshake ack to %s. Closing connection...", program_name)
      return False
    return True

  async def _receive_records(self, reader: asyncio.StreamReader, handshake: ClientHandshake) -> None:
    """Stream a connected program's log records onto the queue until it ends."""
    malformed_packet_count = 0
    while not FATAL_EVENT.is_set():
      payload = await self._read_packet(reader)
      if payload is None:
        return

      try:
        # N.B. record payloads are trusted internal traffic serialised as the
        # record's __dict__ with orjson (the same framing as the handshake).
        obj: object = orjson.loads(payload)
      except orjson.JSONDecodeError as e:
        logger.warning("Client sent malformed packet", exc_info=e)
        malformed_packet_count += 1
        if malformed_packet_count >= self.MAX_MALFORMED_PACKETS:
          logger.warning("Client exceeded maximum malformed packet count; dropping connection")
          return
        continue  # allow the client to try again

      if isinstance(obj, dict):
        await self._queue.async_put(make_log_record(obj, handshake.program_name))
      else:
        logger.warning("Unexpected message type %s from %s", type(obj).__name__, handshake.program_name)
