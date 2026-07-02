# Standard library imports
import asyncio
import logging
import socket
from contextlib import suppress
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path
from pickle import UnpicklingError
from typing import TYPE_CHECKING

# Third party imports
import cloudpickle

# First party imports
from aeth_ext.errors import FATAL_EVENT

# Local imports
from aeth_ext.shared_log_processor.dispatch import RegisterHandlers, UnregisterHandlers
from aeth_ext.shared_log_processor.protocol import LENGTH_STRUCT, ClientLoggingHandshake, LoggingHandshake, make_log_record

if TYPE_CHECKING:
  # Third party imports
  from aiologic import Queue

  # First party imports
  # Local imports
  from aeth_ext.shared_log_processor.dispatch import WriterItem

logger = logging.getLogger(__name__)


class LogRecordServer:
  """Single-server log receiver that fans records out to per-program files.

  Concurrency model (intentionally minimal for a resource-constrained vCPU):

  * the **main thread** runs an :mod:`asyncio` event loop that accepts every
    connection and reads its length-prefixed messages. The first message from a
    connection is its :class:`~aeth_ext.shared_log_processor.protocol.LoggingHandshake`;
    each later message is a log record, decoded into a
    :class:`~aeth_ext.shared_log_processor.protocol.LabelledLogRecord`, stamped with the
    program identity, and pushed onto the shared queue. On handshake it also
    enqueues a :class:`~aeth_ext.shared_log_processor.dispatch.RegisterHandlers` event
    and, when the connection is lost, an
    :class:`~aeth_ext.shared_log_processor.dispatch.UnregisterHandlers` event.
  * a **single writer thread** drains that queue as its sole owner: it applies
    the register/unregister events and feeds every record to the shared
    dispatch logger, whose per-handler
    :class:`~aeth_ext.shared_log_processor.dispatch.ProgramFilter` /
    :class:`~aeth_ext.shared_log_processor.dispatch.ServerFilter` route it through normal
    logging machinery. Because only that one thread mutates the handler list no
    lock is needed, and teardown enqueued behind a program's records cannot drop
    anything in flight.
  """

  def __init__(
    self,
    queue: Queue[WriterItem],
    host: str = "0.0.0.0",
    port: int = DEFAULT_TCP_LOGGING_PORT,
    log_dir: Path | str | None = None,
  ) -> None:
    super().__init__()
    self.host: str = host
    self.port: int = port
    self.log_dir: Path = Path(log_dir) if log_dir is not None else Path.cwd() / "logs"

    self._queue = queue

  # -- lifecycle ------------------------------------------------------------

  async def start_server(self) -> None:
    server = await asyncio.start_server(self._handle_client, self.host, self.port)
    async with server:
      await server.serve_forever()

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

    handshake: LoggingHandshake | None = None
    try:
      payload = await self._read_packet(reader)
      if payload is None:
        return

      try:
        # N.B. the payload is trusted internal traffic; cloudpickle mirrors the
        # framing used by stdlib logging.handlers.SocketHandler.
        obj: object = cloudpickle.loads(payload)
      except (UnpicklingError, EOFError, AttributeError, ImportError, IndexError) as e:
        logger.warning("Client sent malformed packet when handshake expected. Closing connection...", exc_info=e)
        return

      if not isinstance(obj, ClientLoggingHandshake):
        # First message must identify the program; drop a misbehaving client.
        logger.error(f"First message from client was not a ClientLoggingHandshake. Closing connection... {obj}")
        return
      # Convert the wire-format handshake into a LoggingHandshake; pydantic's
      # BeforeValidator fires here on the server, reconstructing each HandlerDef
      # into an actual Handler instance before any records are dispatched.
      handshake = LoggingHandshake(
        handlers=obj.handlers,  # type: ignore[arg-type]  # BeforeValidator converts HandlerDef → Handler
        program_name=obj.program_name,
        logging_base_name=obj.logging_base_name,
      )

      # Ask the writer thread to stand up this program's handlers before any of
      # its records are dispatched.
      await self._queue.async_put(RegisterHandlers(handshake))
      await self._receive_records(reader, handshake)

    finally:
      # Enqueued behind every record already sent, so the writer tears the
      # program's handlers down only after those records have been flushed.
      if handshake is not None:
        await self._queue.async_put(UnregisterHandlers(handshake.program_name))

      writer.close()
      with suppress(OSError, asyncio.CancelledError):
        await writer.wait_closed()

  async def _receive_records(self, reader: asyncio.StreamReader, handshake: LoggingHandshake) -> None:
    """Stream a connected program's log records onto the queue until it ends."""
    malformed_packet_count = 0
    while not FATAL_EVENT.is_set():
      payload = await self._read_packet(reader)
      if payload is None:
        return

      try:
        # N.B. the payload is trusted internal traffic; cloudpickle mirrors the
        # framing used by stdlib logging.handlers.SocketHandler.
        obj: object = cloudpickle.loads(payload)
      except (UnpicklingError, EOFError, AttributeError, ImportError, IndexError) as e:
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
