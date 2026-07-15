# Standard library imports
import base64
import logging
from contextlib import suppress
from logging.handlers import DEFAULT_TCP_LOGGING_PORT, SocketHandler
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, override

# Third party imports
import cloudpickle
import orjson

# First party imports
from aeth_ext.central_log_server.client.filters import RemoteReachability
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
  ClientHandshake,
  HandshakeAck,
  encode_json_packet,
  record_to_payload,
)
from aeth_ext.errors import report_exc
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  import socket
  from asyncio import AbstractEventLoop
  from collections.abc import Mapping

  # First party imports
  from aeth_ext.logging.bases import TaggedLogRecord


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


def make_definition(obj: Any) -> str:
  """Encode *obj* for a remote config's ``definition`` key.

  The object (typically a class, factory callable, or fully-constructed
  formatter/filter/handler component) is captured with :mod:`cloudpickle` -
  so things defined outside an importable module (e.g. in ``__main__``) are
  also supported - and base64-encoded so it can travel inside the JSON
  handshake. The server decodes it only when its
  ``LOGGING_ALLOW_PICKLED_DEFINITIONS`` setting permits.
  """
  return base64.b64encode(cloudpickle.dumps(obj)).decode("ascii")


class HandshakeSocketHandler(SocketHandler):
  """A :class:`~logging.handlers.SocketHandler` that identifies itself on connect.

  Import this in client programs whose log records should be routed to a
  dedicated set of files by the central log server.  Immediately after the
  underlying socket connects (or reconnects after a drop), the handler sends a
  JSON :class:`~aeth_ext.central_log_server.protocol.ClientHandshake` carrying
  ``config`` - a standard dict-based logging configuration (see
  `aeth_ext.logging.config.models.LoggingConfigModel`) describing the
  formatters/handlers/loggers the server should apply into a private hierarchy
  dedicated to this program. File paths in the config should use the
  ``logdir://`` prefix so the server resolves them beneath its own per-program
  log directory; custom components can be embedded with the ``definition`` key
  via :func:`make_definition`.

  The server replies with a
  :class:`~aeth_ext.central_log_server.protocol.HandshakeAck`: a rejection
  (invalid config) is treated as fatal for delivery, while a successful ack
  carries resume information used to replay any backlog the server is missing.

  Example::

      import logging
      from aeth_ext.central_log_server.client import HandshakeSocketHandler

      config = {
        "version": 1,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(message)s"}},
        "handlers": {
          "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logdir://app.log",
            "delay": True,
            "maxBytes": 10_000_000,
            "backupCount": 5,
            "formatter": "plain",
          },
        },
        "root": {"level": "DEBUG", "handlers": ["file"]},
      }
      socket_handler = HandshakeSocketHandler(
        program_name="my-program",
        config=config,
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
    config: Mapping[str, Any],
    host: str = "localhost",
    port: int = DEFAULT_TCP_LOGGING_PORT,
    *,
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
        config: Dict-based logging configuration the server applies into this
            program's private hierarchy. Must already be fully resolved
            client-side except for server-side prefixes (``logdir://``,
            ``cfg://``, ``ext://``) and ``definition`` payloads.
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
    self._config: dict[str, Any] = dict(config)
    self._reachability = RemoteReachability(self._config)
    self._handshake_rejected: str | None = None

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

  def connect_and_verify(self) -> None:
    """Eagerly connect and fail fast if the server rejects our remote config.

    Normal operation is lazy - the socket connects on the first emit - but
    startup code can call this to surface a rejected handshake as a
    :class:`RuntimeError` immediately instead of silently dropping records
    later. A merely unreachable server is *not* an error here (the handler
    buffers and retries); only an explicit rejection raises.
    """
    self.acquire()
    try:
      if self.sock is None:
        self.createSocket()
    finally:
      self.release()
    if self._handshake_rejected is not None:
      raise RuntimeError(
        f"Central log server rejected the handshake for {self._program_name!r}: {self._handshake_rejected}"
      )

  def _send_handshake(self) -> None:
    """Send the identifying handshake as the very first message on the socket.

    A fresh :class:`~aeth_ext.central_log_server.protocol.ClientHandshake` is
    built on every call so each (re)connection sends a clean snapshot of the
    remote config. Once sent, the server's
    :class:`~aeth_ext.central_log_server.protocol.HandshakeAck` reply is read
    (best-effort): a rejection closes the connection and records the server's
    error, while a successful ack is used to replay any backlog the server is
    missing.
    """
    sock: socket.socket | None = self.sock
    if sock is None:
      return
    self._handshake_rejected = None
    handshake = ClientHandshake(
      program_name=self._program_name,
      config=self._config,
    )
    try:
      sock.sendall(encode_json_packet(handshake))
    except OSError:
      # Mirror SocketHandler.send's error handling so the next emit reconnects
      # (and re-sends the handshake) instead of streaming records to a server
      # that never received our identity.
      sock.close()
      self.sock = None
      return

    ack = self._read_ack(sock)
    if ack is not None and not ack.ok:
      self._handshake_rejected = ack.error or "rejected without a reason"
      logger.error(
        "Central log server rejected the handshake for %r: %s",
        self._program_name,
        self._handshake_rejected,
      )
      with suppress(OSError):
        sock.close()
      self.sock = None
      return
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
      obj: object = orjson.loads(payload)
      if not isinstance(obj, dict):
        return None
      return HandshakeAck(**obj)
    except (OSError, ValueError, TypeError):
      return None
    finally:
      sock.settimeout(previous_timeout)

  def _replay_backlog(self, ack: HandshakeAck | None) -> None:
    """Resend whatever the server's ack says it is missing, in order.

    If ``ack`` is ``None`` (no ack received), nothing is replayed here and
    the handler simply resumes streaming new records live. If the id the
    server last confirmed can't be located in memory or on disk, the gap is
    logged and, per plan, the client also resumes live rather than blocking.
    """
    if ack is None:
      return
    backlog = self._history.find_after(ack.last_record_id, ack.last_received_at)
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

    Records the remote config *provably* cannot deliver anywhere (per
    :class:`~aeth_ext.central_log_server.client.filters.RemoteReachability`'s
    level-only analysis) are dropped up front - they never consume an id,
    enter history, or cross the wire. Every other record is kept (sent or
    not) in :attr:`_history` so it can be replayed by id after a reconnect;
    delivery itself is attempted immediately via :meth:`_transmit`, which
    also triggers a reconnect (and thus a resume replay) when the socket is
    down.
    """
    with report_exc(f"HandshakeSocketHandler.emit ({self._program_name!r})", reraise=False):
      if record.levelno < self._reachability.threshold_for(record.name):
        return
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
