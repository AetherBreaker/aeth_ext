# Standard library imports
import asyncio
import logging
import socket
from contextlib import suppress
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path
from typing import TYPE_CHECKING, Literal

# Third party imports
import orjson

# First party imports
from aeth_ext.central_log_server._types import ApplyResultEvent, RegisterClient, UnregisterClient
from aeth_ext.central_log_server.protocol import (
  LENGTH_STRUCT,
  ApplyFailure,
  ApplySuccess,
  ClientHandshake,
  HandshakeAck,
  encode_json_packet,
  make_log_record,
)
from aeth_ext.errors import handle_fatal_exc_async
from aeth_ext.errors.shutdown import SHUTDOWN
from aeth_ext.logging.config import DictConfigurator

if TYPE_CHECKING:
  # Third party imports
  from aiologic import SimpleQueue

  # First party imports
  from aeth_ext.central_log_server._types import WriterItem
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry

logger = logging.getLogger(__name__)


class LogRecordServer:
  """Single-server log receiver that fans records out to per-program hierarchies.

  Concurrency model (intentionally minimal for a resource-constrained vCPU):

  * the **main thread** runs an :mod:`asyncio` event loop that accepts every
    connection and reads its length-prefixed messages. The first message from a
    connection is its JSON
    :class:`~aeth_ext.central_log_server.protocol.ClientHandshake`, carrying the
    remote logging config the server *validates* (pure, cheap, no I/O - see
    :meth:`~aeth_ext.logging.config.DictConfigurator.validate`) into a
    :class:`~aeth_ext.logging.config.DictConfigurator`, so a structurally
    invalid config is rejected in the
    :class:`~aeth_ext.central_log_server.protocol.HandshakeAck` before any
    records flow; each later message is a log record, decoded into a
    :class:`~aeth_ext.logging.bases.TaggedLogRecord`, stamped with the
    program identity, and pushed onto the shared queue. On handshake it also
    enqueues a :class:`~aeth_ext.central_log_server.server.dispatch.RegisterClient`
    event carrying that configurator and, when the connection is lost, an
    :class:`~aeth_ext.central_log_server.server.dispatch.UnregisterClient` event.
  * a **single writer thread** drains that queue as its sole owner: it *applies*
    each configurator (the actual disk I/O and hierarchy construction, off its
    own event loop via ``asyncio.to_thread``) and dispatches every record into
    its program's private hierarchy through normal logging machinery. Because
    only that one thread touches the hierarchies no lock is needed, and
    teardown enqueued behind a program's records cannot drop anything in flight.
  """

  def __init__(
    self,
    queue: SimpleQueue[WriterItem],
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

  @staticmethod
  def _enable_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)  # seconds idle before first probe
    if hasattr(socket, "TCP_KEEPINTVL"):
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)  # seconds between probes
    if hasattr(socket, "TCP_KEEPCNT"):
      sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)  # probes before giving up

  @handle_fatal_exc_async
  async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    sock: socket.socket | None = writer.transport.get_extra_info("socket")
    if sock is not None:
      self._enable_keepalive(sock)

    registered: RegisterClient | None = None
    connection_id = id(writer)
    try:
      payload = await self._read_packet(reader)
      if payload is None:
        return

      logger.info("New client connection from %s", writer.transport.get_extra_info("peername"))

      handshake = self._decode_handshake(payload)
      if handshake is None:
        await self._send_message(writer, HandshakeAck(ok=False, error="invalid handshake"), "<unidentified client>")
        return

      logger.info("Handshake received from %s", handshake.program_name)
      logger.debug(
        "Config received from %s:\n%s",
        handshake.program_name,
        orjson.dumps(handshake.config, option=orjson.OPT_INDENT_2).decode(),
      )

      # Validate the program's config *before* acking so a structurally invalid
      # remote config is rejected fail-fast at handshake time (D-D1: a
      # validation failure is the client's fault, no fallback). Validation is
      # pure and cheap - no filesystem I/O, no logging lock - so it runs
      # inline here rather than in a worker thread. The actual apply() (disk
      # I/O, hierarchy construction) happens later, in the writer thread.
      configurator = DictConfigurator(handshake.config, log_dir=self.log_dir / handshake.program_name)
      try:
        configurator.validate()
      except Exception as e:
        logger.warning("Rejecting %r: remote logging config failed validation", handshake.program_name, exc_info=e)
        await self._send_message(
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
      if not await self._send_message(writer, ack, handshake.program_name):
        return

      # Hand the validated configurator to the writer thread, which applies it
      # (D-C2) before any of this program's records are dispatched.
      registered = RegisterClient(handshake.program_name, configurator, connection_id)
      await self._queue.async_put(registered)
      await self._receive_records_pending_apply(reader, writer, handshake, registered.apply_result)

    except OSError as e:
      # A peer resetting its socket (or any other transport-level failure) is a
      # routine disconnect, not a server fault: without this it would escape into
      # ``handle_fatal_exc_async`` and take the whole server down.
      logger.warning(
        "Connection from %s lost: %s",
        registered.program_name if registered is not None else "<unidentified client>",
        e,
      )
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
  async def _send_message(
    writer: asyncio.StreamWriter, message: HandshakeAck | ApplySuccess | ApplyFailure, program_name: str
  ) -> bool:
    """Best-effort send of a server->client message; returns whether it went out."""
    try:
      writer.write(encode_json_packet(message))
      await writer.drain()
    except OSError:
      logger.warning("Failed to send %s to %s. Closing connection...", type(message).__name__, program_name)
      return False
    return True

  async def _receive_records(
    self,
    reader: asyncio.StreamReader,
    handshake: ClientHandshake,
    malformed_packet_count: int = 0,
  ) -> None:
    """Stream a connected program's log records onto the queue until it ends.

    Plain read loop, no per-packet task bookkeeping. This is the fast path
    for the vast majority of a connection's life - once the writer thread's
    apply outcome is already known, nothing is left to race a read against.
    See :meth:`_receive_records_pending_apply` for the brief window right
    after handshake where that isn't yet true.
    """
    while not SHUTDOWN.is_set():
      payload = await self._read_packet(reader)
      if payload is None:
        return

      if await self._ingest_record_payload(payload, handshake.program_name):
        continue
      malformed_packet_count += 1
      if malformed_packet_count >= self.MAX_MALFORMED_PACKETS:
        logger.warning("Client exceeded maximum malformed packet count; dropping connection")
        return

  async def _receive_records_pending_apply(
    self,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handshake: ClientHandshake,
    apply_result: ApplyResultEvent,
  ) -> None:
    """Juggle record reads against the writer thread's apply outcome, then hand off.

    Until the outcome is known, each read is raced against ``apply_result``
    via ``asyncio.wait(..., FIRST_COMPLETED)`` (D-E2) so a quiet client is
    notified exactly as promptly as a chatty one. The outcome puts exactly
    one out-of-band message on the wire (D-E2a): failure (D-D4 - apply
    itself failed) closes the connection immediately after, while success
    hands the connection off to the plain :meth:`_receive_records` loop for
    the rest of its life - so the cost of the race (one extra
    :class:`asyncio.Task` per packet plus ``asyncio.wait``'s bookkeeping) is
    only ever paid during this initial window, not for a long-lived
    connection's entire duration.
    """
    malformed_packet_count = 0
    read_task: asyncio.Task[bytes | None] = asyncio.ensure_future(self._read_packet(reader))
    event_task: asyncio.Task[Literal["success", "failure"]] = asyncio.ensure_future(apply_result.wait_outcome())
    try:
      while not SHUTDOWN.is_set():
        done, _ = await asyncio.wait((read_task, event_task), return_when=asyncio.FIRST_COMPLETED)

        if event_task in done:
          if await self._notify_apply_outcome(event_task, writer, handshake.program_name):
            return

          # Success: fold the read already in flight into the handoff, then
          # let the plain loop take over - no more racing needed.
          payload = await read_task
          if payload is None:
            return
          if not await self._ingest_record_payload(payload, handshake.program_name):
            malformed_packet_count += 1
            if malformed_packet_count >= self.MAX_MALFORMED_PACKETS:
              logger.warning("Client exceeded maximum malformed packet count; dropping connection")
              return
          await self._receive_records(reader, handshake, malformed_packet_count)
          return

        payload = read_task.result()
        if payload is None:
          return
        read_task = asyncio.ensure_future(self._read_packet(reader))

        if await self._ingest_record_payload(payload, handshake.program_name):
          continue
        malformed_packet_count += 1
        if malformed_packet_count >= self.MAX_MALFORMED_PACKETS:
          logger.warning("Client exceeded maximum malformed packet count; dropping connection")
          return
    finally:
      read_task.cancel()
      event_task.cancel()

  async def _notify_apply_outcome(
    self,
    event_task: asyncio.Task[Literal["success", "failure"]],
    writer: asyncio.StreamWriter,
    program_name: str,
  ) -> bool:
    """Send the D-E2a out-of-band message for a resolved apply outcome. Returns whether to close the connection."""
    if event_task.result() == "success":
      await self._send_message(writer, ApplySuccess(), program_name)
      return False
    await self._send_message(
      writer,
      ApplyFailure(error="remote logging config could not be applied by the server"),
      program_name,
    )
    return True

  async def _ingest_record_payload(self, payload: bytes, program_name: str) -> bool:
    """Decode and enqueue one record payload. Returns whether it was well-formed."""
    try:
      # N.B. record payloads are trusted internal traffic serialised as the
      # record's __dict__ with orjson (the same framing as the handshake).
      obj: object = orjson.loads(payload)
    except orjson.JSONDecodeError as e:
      logger.warning("Client sent malformed packet", exc_info=e)
      return False
    if isinstance(obj, dict):
      await self._queue.async_put(make_log_record(obj, program_name))
      return True
    logger.warning("Unexpected message type %s from %s", type(obj).__name__, program_name)
    return False
