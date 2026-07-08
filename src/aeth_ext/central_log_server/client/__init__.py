# Standard library imports
import logging
from contextlib import suppress
from logging import FileHandler
from logging.handlers import DEFAULT_TCP_LOGGING_PORT, SocketHandler
from pathlib import Path
from pickle import UnpicklingError
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, override

# Third party imports
import cloudpickle
import orjson

# First party imports
from aeth_ext.central_log_server.client.history import (
  EmergencyHistoryWriter,
  HistoryEntry,
  RecordHistoryBuffer,
)
from aeth_ext.central_log_server.client.id_checkpoint import (
  AsyncioIdCheckpointBackend,
  IdCheckpointBackend,
  ThreadedIdCheckpointBackend,
)
from aeth_ext.central_log_server.protocol import (
  LENGTH_STRUCT,
  ClientLoggingHandshake,
  FilterDef,
  FormatterDef,
  HandlerDef,
  HandshakeAck,
  TaggedLogRecord,
  encode_packet,
  record_to_payload,
)
from aeth_ext.errors import report_exc
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  import socket
  from asyncio import AbstractEventLoop
  from collections.abc import Sequence
  from logging import Filter, Formatter, Handler


settings = BaseSettings.get_settings()
logger = logging.getLogger(__name__)


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
  """Read exactly *size* bytes from *sock*, or ``None`` if the peer closed early."""
  chunks = bytearray()
  while len(chunks) < size:
    chunk = sock.recv(size - len(chunks))
    if not chunk:
      return None
    chunks.extend(chunk)
  return bytes(chunks)


def make_formatter_def(cls: type[Formatter], *args: Any, **kwargs: Any) -> FormatterDef:
  """Build a :class:`~aeth_ext.shared_log_processor.protocol.FormatterDef` for *cls*.

  Pass the same positional and keyword arguments you would pass to
  ``cls.__init__``.  The class itself is captured with :mod:`cloudpickle`, so
  custom formatters defined outside an importable module (e.g. in ``__main__``)
  are also supported.

  Args:
      cls: Formatter class to describe.
      *args: Positional arguments forwarded to ``cls.__init__`` on the server.
      **kwargs: Keyword arguments forwarded to ``cls.__init__`` on the server.
  """
  return FormatterDef(
    pickled_def=cloudpickle.dumps(cls),
    cls_name=cls.__name__,
    args=args,
    kwargs=kwargs,
  )


def make_filter_def(cls: type[Filter], *args: Any, **kwargs: Any) -> FilterDef:
  """Build a :class:`~aeth_ext.shared_log_processor.protocol.FilterDef` for *cls*.

  Args:
      cls: Filter class to describe.
      *args: Positional arguments forwarded to ``cls.__init__`` on the server.
      **kwargs: Keyword arguments forwarded to ``cls.__init__`` on the server.
  """
  return FilterDef(
    pickled_def=cloudpickle.dumps(cls),
    cls_name=cls.__name__,
    args=args,
    kwargs=kwargs,
  )


def make_handler_def(
  cls: type[Handler],
  *args: Any,
  project_name: str,
  formatter: FormatterDef | None = None,
  filters: Sequence[FilterDef] | None = None,
  startup_rollover: bool | None = None,
  level: int | None = None,
  **kwargs: Any,
) -> HandlerDef:
  """Build a :class:`~aeth_ext.shared_log_processor.protocol.HandlerDef` for *cls*.

  The handler is reconstructed on the server; pass the constructor arguments
  that should be used *there* (e.g. server-side file paths).

  For file-based handlers pass ``delay=True`` so the file is opened only on the
  first emit on the server, which prevents stale file handles from being created
  on the client during the handshake.

  Args:
      cls: Handler class to describe.
      *args: Positional arguments forwarded to ``cls.__init__`` on the server.
      formatter: Optional formatter definition to attach to the handler.
      filters: Optional filter definitions to attach to the handler.
      startup_rollover: Optional flag to indicate if the handler should perform a rollover on startup.
      **kwargs: Keyword arguments forwarded to ``cls.__init__`` on the server.
  """

  if issubclass(cls, FileHandler):
    # Ensure that the the path of the working directory is cut off of the file path passed to the handler
    # E.g. if the passed path is
    # "D:\SFT Software Projects\SFT Workspace\persisted_data\logs\subtask\my_program.log" then the resulting
    # path should be "subtask\my_program.log" so that the server can prepend its own log storage directory
    # to the path.

    temp_args = list(args)
    path: str | Path = kwargs.get("filename") or temp_args[0]

    if isinstance(path, str):
      path = Path(path)

    path = path.relative_to(settings.log_loc_folder) if len(path.parts) > 1 else path

    if "filename" in kwargs:
      kwargs["filename"] = path
    else:
      temp_args[0] = path
      args = tuple(temp_args)

  return HandlerDef(
    pickled_def=cloudpickle.dumps(cls),
    cls_name=cls.__name__,
    project_name=project_name,
    args=args,
    kwargs=kwargs,
    formatter=formatter,
    filters=tuple(filters) if filters is not None else None,
    startup_rollover=startup_rollover,
    level=level,
  )


class HandshakeSocketHandler(SocketHandler):
  """A :class:`~logging.handlers.SocketHandler` that identifies itself on connect.

  Import this in client programs whose log records should be routed to a
  dedicated set of files by the shared log server.  Immediately after the
  underlying socket connects (or reconnects after a drop), the handler sends a
  :class:`~aeth_ext.shared_log_processor.protocol.LoggingHandshake` to the server. The
  server reconstructs the supplied handler definitions and registers them before
  any log records arrive so nothing is dropped.

  Use :func:`make_handler_def`, :func:`make_formatter_def`, and
  :func:`make_filter_def` to build the
  :class:`~aeth_ext.shared_log_processor.protocol.HandlerDef` blueprints:

  Example::

      import logging.handlers
      from aeth_ext.shared_log_processor.client import (
        HandshakeSocketHandler,
        make_formatter_def,
        make_handler_def,
      )

      fmt = make_formatter_def(logging.Formatter, "%(asctime)s %(levelname)s %(message)s")
      fh = make_handler_def(
        logging.handlers.RotatingFileHandler,
        "/var/log/my-program/app.log",
        delay=True,
        maxBytes=10_000_000,
        backupCount=5,
        formatter=fmt,
      )
      socket_handler = HandshakeSocketHandler(
        program_name="my-program",
        handlers=(fh,),
        host="logserver",
        port=9020,
      )
      logging.getLogger().addHandler(socket_handler)
  """

  # TODO when there is a major error while the socket connection is confirmed to be down
  # TODO then the handler should spin up a new handler to flush all the records stored in history
  # TODO to an emergency log file for triage.

  def __init__(
    self,
    program_name: str,
    handlers: Sequence[HandlerDef],
    host: str = "localhost",
    port: int = DEFAULT_TCP_LOGGING_PORT,
    *,
    logging_base_name: str | None = None,
    max_history_records: int = 50_000,
    max_history_bytes: int = 64 * 1024 * 1024,
    max_history_age: float = 300.0,
    id_checkpoint_backend: Literal["thread", "asyncio"] = "thread",
    event_loop: AbstractEventLoop | None = None,
    emergency_time_threshold: float = 5.0,
    emergency_attempt_threshold: int = 10,
  ) -> None:
    """See the class docstring for the common parameters.

    Args:
        max_history_records: In-memory record count above which the history
            buffer spills to disk.
        max_history_bytes: Approximate in-memory byte size above which the
            history buffer spills to disk.
        max_history_age: Seconds since the last spill above which the history
            buffer spills to disk, bounding data loss on a hard crash.
        id_checkpoint_backend: How the last-assigned record id is durably
            persisted across restarts - ``"thread"`` for a dedicated daemon
            thread (the default, suitable for any program), or ``"asyncio"``
            for programs that would rather reuse their own event loop.
        event_loop: Required when ``id_checkpoint_backend="asyncio"``; the
            loop that owns the id-persistence coroutine.
        emergency_time_threshold: Minutes since the last successful send
            after which, combined with ``emergency_attempt_threshold``, the
            handler assumes the server is down and starts writing every new
            record straight to disk instead of relying on the lazy buffer.
        emergency_attempt_threshold: Consecutive failed send attempts
            required (alongside ``emergency_time_threshold``) to enter
            emergency mode.
    """
    super().__init__(host, port)
    self._program_name = program_name
    self._handler_defs = handlers
    self._logging_base_name = logging_base_name

    self._history = RecordHistoryBuffer(max_history_records, max_history_bytes, max_history_age)

    checkpoint_path = settings.persisted_dir_loc / "logging_ids.checkpoint"
    self._id_checkpoint: IdCheckpointBackend
    if id_checkpoint_backend == "asyncio":
      if event_loop is None:
        raise ValueError("event_loop is required when id_checkpoint_backend='asyncio'")
      self._id_checkpoint = AsyncioIdCheckpointBackend(checkpoint_path, event_loop)
    else:
      self._id_checkpoint = ThreadedIdCheckpointBackend(checkpoint_path)

    self._next_id = self._id_checkpoint.load() + 1
    self._last_sent_id = 0

    self._emergency_time_threshold = emergency_time_threshold * 60.0
    self._emergency_attempt_threshold = emergency_attempt_threshold
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    self._emergency_writer: EmergencyHistoryWriter | None = None

  @override
  def createSocket(self) -> None:
    previous_sock = self.sock
    super().createSocket()
    # ``super().createSocket()`` only assigns a new object to ``self.sock`` when
    # a fresh connection is actually established, so this guard fires exactly
    # once per (re)connection - the moment we must announce ourselves.
    if self.sock is not None and self.sock is not previous_sock:
      self._send_handshake()

  def _send_handshake(self) -> None:
    """Send the identifying handshake as the very first message on the socket.

    A fresh :class:`~aeth_ext.shared_log_processor.protocol.ClientLoggingHandshake` is
    built on every call so each (re)connection sends a clean snapshot of the
    handler definitions rather than anything that may have accumulated state.
    Once sent, the server's :class:`~aeth_ext.shared_log_processor.protocol.HandshakeAck`
    reply is read (best-effort) and used to replay any backlog the server is
    missing.
    """
    sock: socket.socket | None = self.sock
    if sock is None:
      return
    handshake = ClientLoggingHandshake(
      program_name=self._program_name,
      handlers=tuple(self._handler_defs),
      logging_base_name=self._logging_base_name,
    )
    try:
      sock.sendall(encode_packet(handshake))
    except OSError:
      # Mirror SocketHandler.send's error handling so the next emit reconnects
      # (and re-sends the handshake) instead of streaming records to a server
      # that never received our identity.
      sock.close()
      self.sock = None
      return

    ack = self._read_ack(sock)
    self._replay_backlog(ack)

  def _read_ack(self, sock: socket.socket, timeout: float = 5.0) -> HandshakeAck | None:
    """Best-effort read of the server's post-handshake acknowledgement.

    A failure here (timeout, malformed payload, or the connection dying) is
    not fatal to the connection itself - it just means resume-by-id is
    skipped and the client resumes streaming live, so ``self.sock`` is left
    untouched on any error; a genuinely broken socket will surface the next
    time a record is actually sent.
    """
    previous_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
      header = _recv_exact(sock, LENGTH_STRUCT.size)
      if header is None:
        return None
      (length,) = LENGTH_STRUCT.unpack(header)
      payload = _recv_exact(sock, length)
      if payload is None:
        return None
      ack_or_obj = cloudpickle.loads(payload)
    except (
      OSError,
      UnpicklingError,
      EOFError,
      AttributeError,
      ImportError,
      IndexError,
    ):
      return None
    finally:
      sock.settimeout(previous_timeout)
    if not isinstance(ack_or_obj, HandshakeAck):
      return None
    else:
      ack = ack_or_obj
    return ack

  def _replay_backlog(self, ack: HandshakeAck | None) -> None:
    """Resend whatever the server's ack says it is missing, in order.

    If ``ack`` is ``None`` (no ack received), nothing is replayed here and
    the handler simply resumes streaming new records live. If the id the
    server last confirmed can't be located in memory or on disk, the gap is
    logged and, per plan, the client also resumes live rather than blocking.
    """
    if ack is None:
      return
    # TODO review whether timestamp should be kept as a datetime to allow for more efficient lookup with less iteration.
    # TODO Currently, the timestamp is converted to a float for comparison with the created time of the record,
    # TODO which is also a float. This may be inefficient if there are many records in the history.
    hint_created = ack.last_received_at.timestamp() if ack.last_received_at is not None else None
    backlog = self._history.find_after(ack.last_record_id, hint_created)
    if backlog is None:
      logger.warning(
        "Log server last confirmed record id %s for %r, but it could not be located in history; "
        "some records may already have aged out. Resuming live.",
        ack.last_record_id,
        self._program_name,
      )
      return
    sock = self.sock
    if sock is None:
      return
    for entry in backlog:
      try:
        sock.sendall(self.makePickle(entry.record))
      except OSError:
        sock.close()
        self.sock = None
        return
      self._last_sent_id = entry.id

  @override
  def emit(self, record: TaggedLogRecord) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Assign a durable id, record history, then attempt delivery.

    Every record is kept (sent or not) in :attr:`_history` so it can be
    replayed by id after a reconnect; delivery itself is attempted
    immediately via :meth:`_transmit`, which also triggers a reconnect (and
    thus a resume replay) when the socket is down.
    """
    with report_exc(f"HandshakeSocketHandler.emit ({self._program_name!r})", reraise=False):
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)

      if self._emergency_writer is not None:
        entry.persisted = True
        self._emergency_writer.submit(entry)

      if self._transmit(entry):
        self._consecutive_failures = 0
        self._last_success_monotonic = monotonic()
        if self._emergency_writer is not None:
          self._exit_emergency_mode()
      else:
        self._consecutive_failures += 1
        self._maybe_enter_emergency_mode()

  def _transmit(self, entry: HistoryEntry) -> bool:
    """Ensure *entry* has been (or already was) delivered to the server.

    Returns ``True`` on success. If a reconnect just happened, the entry may
    already have been sent as part of the handshake's backlog replay - in
    that case this is a no-op that still reports success.
    """
    if self.sock is None:
      self.createSocket()  # stdlib SocketHandler.createSocket never raises
    sock = self.sock
    if sock is None:
      return False
    if entry.id <= self._last_sent_id:
      return True
    try:
      sock.sendall(self.makePickle(entry.record))
    except OSError:
      with suppress(OSError):
        sock.close()
      self.sock = None
      return False
    self._last_sent_id = entry.id
    return True

  def _maybe_enter_emergency_mode(self) -> None:
    if self._emergency_writer is not None:
      return
    elapsed = monotonic() - self._last_success_monotonic
    if elapsed >= self._emergency_time_threshold and self._consecutive_failures >= self._emergency_attempt_threshold:
      self._emergency_writer = EmergencyHistoryWriter(self._history.history_dir)
      logger.warning(
        "Log server unreachable for %.0fs after %d attempts; writing new records directly to history file",
        elapsed,
        self._consecutive_failures,
      )

  def _exit_emergency_mode(self) -> None:
    writer = self._emergency_writer
    if writer is None:
      return
    self._emergency_writer = None
    writer.close()
    self._consecutive_failures = 0
    logger.info(
      "Log server reachable again for %r; stopped emergency history writer",
      self._program_name,
    )

  @override
  def makePickle(self, record: TaggedLogRecord) -> bytes:  # pyright: ignore[reportIncompatibleMethodOverride]
    s = orjson.dumps(record_to_payload(record), default=str)
    slen = LENGTH_STRUCT.pack(len(s))
    return slen + s

  @override
  def close(self) -> None:
    self._id_checkpoint.close()
    if self._emergency_writer is not None:
      self._emergency_writer.close()
      self._emergency_writer = None
    super().close()
