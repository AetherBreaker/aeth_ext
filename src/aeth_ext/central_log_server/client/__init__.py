# Standard library imports
import asyncio
import base64
import functools
import logging
import queue as _queue_mod
import select
import socket
import threading
from contextlib import suppress
from logging.handlers import DEFAULT_TCP_LOGGING_PORT, SocketHandler
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, Self, override

# Third party imports
import cloudpickle
import orjson

# First party imports
from aeth_ext.central_log_server.client.durability import RecordDurability
from aeth_ext.central_log_server.client.emergency import EmergencyModeTracker
from aeth_ext.central_log_server.client.filters import RemoteReachability
from aeth_ext.central_log_server.client.history import (
  EmergencyHistoryWriter,
  HistoryEntry,
  RecordHistoryBuffer,
)
from aeth_ext.central_log_server.client.id_checkpoint import (
  IdCheckpointBackend,
  ThreadedIdCheckpointBackend,
)
from aeth_ext.central_log_server.protocol import (
  LENGTH_STRUCT,
  ApplyFailure,
  ClientHandshake,
  HandshakeAck,
  decode_server_message,
  encode_json_packet,
  record_to_payload,
)
from aeth_ext.errors import handle_ack_read_failure, handle_config_rejected, report_exc, shutdown
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from asyncio import AbstractEventLoop
  from collections.abc import Mapping
  from typing import Protocol, TypeVar

  _T_co = TypeVar("_T_co", covariant=True)

  class _QueueLike(Protocol[_T_co]):
    """Structural type for queue-like objects with a blocking ``get``."""

    def get(self, block: bool = True, timeout: float | None = None) -> _T_co: ...

  # First party imports
  from aeth_ext.logging.bases import TaggedLogRecord


settings = BaseSettings.get_settings()
logger = logging.getLogger(__name__)

# How long the threaded shutdown pass will wait on a coroutine it handed back to
# a client's own event loop. Comfortably inside the pass's own budget, so one
# wedged loop cannot consume the whole grace period.
_LOOP_TEARDOWN_TIMEOUT = 3.0


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
  """Read exactly *size* bytes from *sock*, or ``None`` if the peer closed early."""
  chunks = bytearray()
  while len(chunks) < size:
    chunk = sock.recv(size - len(chunks))
    if not chunk:
      return None
    chunks.extend(chunk)
  return bytes(chunks)


def read_server_message_sync(sock: socket.socket, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None:
  """Best-effort read of one server message (D-E5), or ``None`` if malformed/absent/timed out.

  A failure here (timeout, malformed payload, or the connection dying) is not fatal to the connection
  itself - the caller decides what that means for its own purposes - so the socket is left untouched on
  any error; a genuinely broken socket will surface the next time a record is actually sent.
  ``ApplySuccess`` is deliberately not in the return type: nothing reads its fields, callers only ever
  need to check for ``ApplyFailure``.

  Restoring the previous timeout is itself wrapped in ``suppress(OSError)``: when called from a
  background watcher thread, *sock* can be closed concurrently by whichever thread owns it (e.g. a send
  failure or ``close()``), racing this cleanup step after ``recv`` already returned.
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
    message = decode_server_message(payload)
    return message if isinstance(message, HandshakeAck | ApplyFailure) else None
  except OSError:
    return None
  finally:
    with suppress(OSError):
      sock.settimeout(previous_timeout)


async def read_server_message_async(
  reader: asyncio.StreamReader,
  timeout: float | None = 5.0,  # noqa: ASYNC109
) -> HandshakeAck | ApplyFailure | None:
  """Best-effort read of one server message (D-E5), or ``None`` if malformed/absent/timed out.

  ``ApplySuccess`` is deliberately not in the return type: nothing reads its fields, callers only ever
  need to check for ``ApplyFailure``.
  """
  try:
    async with asyncio.timeout(timeout):
      header = await reader.readexactly(LENGTH_STRUCT.size)
      (length,) = LENGTH_STRUCT.unpack(header)
      payload = await reader.readexactly(length)
    message = decode_server_message(payload)
    return message if isinstance(message, HandshakeAck | ApplyFailure) else None
  except OSError, TimeoutError, asyncio.IncompleteReadError:
    return None


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
    host: str = settings.log_conn_host,
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

    self._durability = RecordDurability(
      self._program_name,
      max_history_records,
      max_history_bytes,
      max_history_age,
      id_checkpoint_backend=id_checkpoint_backend,
      event_loop=event_loop,
    )
    self._emergency = EmergencyModeTracker(
      self._durability.history_dir,
      self._program_name,
      time_threshold=emergency_time_threshold * 60.0,
      attempt_threshold=emergency_attempt_threshold,
    )

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(self.close, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY)

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._durability.arm_shutdown()

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
      raise RuntimeError(f"Central log server rejected the handshake for {self._program_name!r}: {self._handshake_rejected}")

  def _send_handshake(self) -> None:
    """Send the identifying handshake as the very first message on the socket.

    A fresh :class:`~aeth_ext.central_log_server.protocol.ClientHandshake` is
    built on every call so each (re)connection sends a clean snapshot of the
    remote config. Once sent, the server's
    :class:`~aeth_ext.central_log_server.protocol.HandshakeAck` reply is read
    (best-effort): a rejection closes the connection and records the server's
    error (D-E6), a failed read is alerted but non-fatal (D-E7), and a
    successful ack is used to replay any backlog the server is missing. On
    success, a short-lived watcher thread is started to catch the D-E2
    out-of-band apply-result message that follows.
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

    message = read_server_message_sync(sock)
    if not isinstance(message, HandshakeAck):
      handle_ack_read_failure(self._program_name)
      return
    if not message.ok:
      self._handshake_rejected = message.error or "rejected without a reason"
      handle_config_rejected(self._program_name, self._handshake_rejected)
      with suppress(OSError):
        sock.close()
      self.sock = None
      return
    self._replay_backlog(message)
    threading.Thread(
      target=self._watch_apply_result,
      args=(sock,),
      name=f"apply-result-watcher-{self._program_name}",
      daemon=True,
    ).start()

  def _watch_apply_result(self, sock: socket.socket) -> None:
    """Short-lived watcher for the D-E2/D-E2a out-of-band apply-result message.

    Always a background thread rather than conditionally an event-loop task:
    detecting whether the calling thread owns a running loop is
    non-deterministic here (``emit()`` can run on whatever thread logged the
    record that triggered a reconnect), so a uniform thread is used
    regardless. Because the server always replies (D-E2a), this thread's
    lifetime is short either way, so the uniform approach costs almost
    nothing while removing that heuristic entirely.
    """
    message = read_server_message_sync(sock, timeout=None)
    if isinstance(message, ApplyFailure):
      handle_config_rejected(self._program_name, message.error)

  def _replay_backlog(self, ack: HandshakeAck) -> None:
    """Resend whatever the server's ack says it is missing, in order."""
    sock = self.sock
    if sock is None:
      return
    for entry in self._durability.resolve_backlog(ack):
      try:
        sock.sendall(self.makePickle(entry.record))
      except OSError:
        sock.close()
        self.sock = None
        return
      self._durability.mark_sent(entry.id)

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
      entry = self._durability.record(record, local_root=None, emergency_writer=self._emergency.writer)

      if self._transmit(entry):
        self._emergency.record_success()
      else:
        self._emergency.record_failure()
        self._emergency.maybe_enter()

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
    if entry.id <= self._durability.last_sent_id:
      return True
    try:
      sock.sendall(self.makePickle(entry.record))
    except OSError:
      with suppress(OSError):
        sock.close()
      self.sock = None
      return False
    self._durability.mark_sent(entry.id)
    return True

  @override
  def makePickle(self, record: TaggedLogRecord) -> bytes:  # pyright: ignore[reportIncompatibleMethodOverride]
    s = orjson.dumps(record_to_payload(record), default=str)
    slen = LENGTH_STRUCT.pack(len(s))
    return slen + s

  @override
  def close(self) -> None:
    """Flush history, then tear down the checkpoint, emergency writer, and socket.

    Reachable both from ``logging.shutdown``'s ``atexit`` pass and from the
    shutdown registry, so every step here has to tolerate running twice. The
    history flush is new: previously nothing flushed it on any path, so whatever
    sat in memory below the spill thresholds was simply lost.
    """
    self._durability.flush()
    self._durability.close()
    self._emergency.close()
    super().close()


class AsyncioQueueDrainer:
  """Drains an :class:`asyncio.Queue` of :class:`~logging.LogRecord` objects and
  forwards them to a central log server over a persistent TCP connection.

  The remote logging configuration is passed in at construction time and sent as
  a :class:`~aeth_ext.central_log_server.protocol.ClientHandshake` on every
  (re)connection.  Use as an async context manager to run the drainer as a
  background task::

      async with AsyncioQueueDrainer(q, "my-app", config, host="logserver") as drainer:
        ...  # records placed on *q* are forwarded while this block runs

  Or drive it manually: ``await drainer.run()`` blocks until :meth:`aclose` is
  called from another coroutine.
  """

  def __init__(
    self,
    queue: asyncio.Queue[logging.LogRecord] | _QueueLike[logging.LogRecord],
    target: str,
    host: str = settings.log_conn_host,
    port: int = DEFAULT_TCP_LOGGING_PORT,
    *,
    log_locally: bool = False,
    config_timeout: float = 30.0,
    max_history_records: int = 50_000,
    max_history_bytes: int = 64 * 1024 * 1024,
    max_history_age: float = 300.0,
    emergency_time_threshold: float = 5.0,
    emergency_attempt_threshold: int = 10,
    reconnect_delay: float = 5.0,
  ) -> None:
    # Deferred to break the config_provider → setup → client import cycle.
    # First party imports
    from aeth_ext.central_log_server.client.config_provider import query_logging_configs

    _cfg = query_logging_configs(target, mode="socket", timeout=config_timeout)
    self._queue = queue
    self._is_asyncio_queue = isinstance(queue, asyncio.Queue)
    self._program_name = _cfg["program_name"]
    self._config: dict[str, Any] = dict(_cfg["remote"])
    self._host = host
    self._port = port
    self._reconnect_delay = reconnect_delay

    self._durability = RecordDurability(self._program_name, max_history_records, max_history_bytes, max_history_age)
    self._emergency = EmergencyModeTracker(
      self._durability.history_dir,
      self._program_name,
      time_threshold=emergency_time_threshold * 60.0,
      attempt_threshold=emergency_attempt_threshold,
    )

    self._local_manager: logging.Manager | None = None
    self._local_root: logging.Logger | None = None
    if log_locally or _cfg["testing"]:
      # First party imports
      from aeth_ext.logging.config import DictConfigurator

      self._local_manager, self._local_root = DictConfigurator(_cfg["local"], log_dir=settings.log_loc_folder).apply(private=True)

    self._stop_event = asyncio.Event()
    self._task: asyncio.Task[None] | None = None

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._durability.arm_shutdown()

  def _finish_shutdown(self) -> None:
    """Threaded-phase teardown (D-I8): drive :meth:`aclose` from off the loop.

    This is the one registrant that is loop-affine -- ``aclose`` awaits
    ``self._task``, which can only make progress on the loop that owns it. The
    threaded pass therefore hands the coroutine back to that loop and waits with
    a timeout, which is precisely why the pass runs on a thread rather than
    inline: if the loop is wedged (a real possibility, and often the reason for
    the shutdown) this times out and moves on instead of hanging forever.

    :meth:`RecordHistoryBuffer.flush` runs first and unconditionally, since it
    does not depend on the loop and must not be lost to a timeout.
    """
    self._durability.flush()

    loop = self._loop_if_running()
    if loop is None:
      return
    try:
      asyncio.run_coroutine_threadsafe(self.aclose(), loop).result(timeout=_LOOP_TEARDOWN_TIMEOUT)
    except TimeoutError, RuntimeError:
      logger.warning("Timed out awaiting AsyncioQueueDrainer.aclose(); the event loop may be wedged", exc_info=True)

  def _loop_if_running(self) -> asyncio.AbstractEventLoop | None:
    """The loop owning :attr:`_task`, or ``None`` if there is nothing to await."""
    if self._task is None:
      return None
    loop = self._task.get_loop()
    return loop if not loop.is_closed() else None

  async def run(self) -> None:
    """Drain the queue indefinitely, reconnecting on dropped connections.

    Returns only after :meth:`aclose` is called.  A handshake rejected by the
    server is treated as a fatal configuration error and stops the loop
    immediately without retrying.  Every failed connection attempt increments an
    internal failure counter; once both the counter and the elapsed time since
    the last successful send exceed the configured thresholds the drainer enters
    emergency mode and writes incoming records directly to the history file so
    they survive a process crash.
    """
    while not self._stop_event.is_set():
      try:
        reader, writer = await asyncio.open_connection(self._host, self._port)
      except OSError:
        self._emergency.record_failure()
        self._emergency.maybe_enter()
        await self._sleep_or_drain(self._reconnect_delay)
        continue

      writer.write(encode_json_packet(ClientHandshake(self._program_name, self._config)))
      try:
        await writer.drain()
      except OSError:
        writer.close()
        self._emergency.record_failure()
        self._emergency.maybe_enter()
        await self._sleep_or_drain(self._reconnect_delay)
        continue

      ack = await read_server_message_async(reader)
      if isinstance(ack, HandshakeAck):
        if not ack.ok:
          handle_config_rejected(self._program_name, ack.error or "rejected without a reason")
          writer.close()
          return
      else:
        handle_ack_read_failure(self._program_name)
        ack = None

      if not await self._replay_backlog(ack, writer):
        with suppress(Exception):
          writer.close()
          await writer.wait_closed()
        self._emergency.record_failure()
        self._emergency.maybe_enter()
        await self._sleep_or_drain(self._reconnect_delay)
        continue

      watcher_task = asyncio.ensure_future(self._watch_apply_result(reader))
      try:
        await self._drain_until_broken(writer)
      finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
          await watcher_task
        with suppress(Exception):
          writer.close()
          await writer.wait_closed()

      if not self._stop_event.is_set():
        await self._sleep_or_drain(self._reconnect_delay)

  async def connect_and_verify(self) -> None:
    """Eagerly connect and fail fast if the server rejects our remote config.

    Normal operation is lazy - the connection opens on the first drain - but
    startup code can call this to surface a rejected handshake as a
    :class:`RuntimeError` immediately instead of silently dropping records
    later.  A merely unreachable server is *not* an error here (the drainer
    reconnects on the next :meth:`run` iteration); only an explicit rejection
    raises.
    """
    try:
      reader, writer = await asyncio.open_connection(self._host, self._port)
    except OSError:
      return
    writer.write(encode_json_packet(ClientHandshake(self._program_name, self._config)))
    try:
      await writer.drain()
    except OSError:
      writer.close()
      return
    ack = await read_server_message_async(reader)
    with suppress(Exception):
      writer.close()
      await writer.wait_closed()
    if isinstance(ack, HandshakeAck) and not ack.ok:
      raise RuntimeError(
        f"Central log server rejected the handshake for {self._program_name!r}: {ack.error or 'rejected without a reason'}"
      )

  async def _watch_apply_result(self, reader: asyncio.StreamReader) -> None:
    """Background watcher for the D-E2/D-E2a out-of-band apply-result message.

    Runs concurrently with :meth:`_drain_until_broken` for the lifetime of one
    connection; cancelled from :meth:`run`'s ``finally`` once that finishes (a
    fresh watcher starts on the next reconnect). No timeout - bounded only by
    that cancellation, since the server's reply time depends on how busy its
    writer thread is (D-E2a guarantees a reply eventually, not promptly under
    load).
    """
    message = await read_server_message_async(reader, timeout=None)
    if isinstance(message, ApplyFailure):
      handle_config_rejected(self._program_name, message.error)

  async def _drain_until_broken(self, writer: asyncio.StreamWriter) -> None:
    loop = asyncio.get_running_loop()
    while not self._stop_event.is_set():
      record: logging.LogRecord
      try:
        if self._is_asyncio_queue:
          record = await asyncio.wait_for(self._queue.get(), timeout=0.5)  # type: ignore[union-attr]
        else:
          record = await loop.run_in_executor(  # type: ignore[assignment]
            None, functools.partial(self._queue.get, timeout=0.5)
          )
      except TimeoutError, _queue_mod.Empty:
        continue
      self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)  # type: ignore[arg-type]
      payload = orjson.dumps(record_to_payload(record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self._emergency.record_success()

  async def _replay_backlog(self, ack: HandshakeAck | None, writer: asyncio.StreamWriter) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    for entry in self._durability.resolve_backlog(ack):
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        return False
      self._durability.mark_sent(entry.id)
    return True

  async def _sleep_or_drain(self, duration: float) -> None:
    """Wait *duration* seconds; in emergency mode drain the queue to the history file instead."""
    if self._emergency.writer is None:
      await asyncio.sleep(duration)
      return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    while not self._stop_event.is_set():
      remaining = deadline - loop.time()
      if remaining <= 0.0:
        break
      record: logging.LogRecord
      try:
        if self._is_asyncio_queue:
          record = await asyncio.wait_for(self._queue.get(), timeout=min(0.5, remaining))  # type: ignore[union-attr]
        else:
          record = await loop.run_in_executor(  # type: ignore[assignment]
            None, functools.partial(self._queue.get, timeout=min(0.4, remaining))
          )
      except TimeoutError, _queue_mod.Empty:
        continue
      self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)  # type: ignore[arg-type]
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]

  async def aclose(self) -> None:
    """Signal the drainer to stop and await its background task if one was started.

    Idempotent -- reachable both from ``__aexit__`` and from the shutdown
    registry via :meth:`_finish_shutdown`.
    """
    self._durability.flush()
    self._stop_event.set()
    if self._task is not None:
      with suppress(asyncio.CancelledError):
        await self._task
      self._task = None
    self._durability.close()
    self._emergency.close()
    if self._local_manager is not None and self._local_root is not None:
      # First party imports
      from aeth_ext.central_log_server.server.dispatch import shutdown_hierarchy

      shutdown_hierarchy(self._local_manager, self._local_root)
      self._local_manager = None
      self._local_root = None

  async def __aenter__(self) -> Self:
    self._stop_event.clear()
    self._task = asyncio.get_running_loop().create_task(self.run())
    return self

  async def __aexit__(self, *_: object) -> None:
    if isinstance(self._queue, asyncio.Queue):
      await self._queue.join()
    elif hasattr(self._queue, "join"):
      await asyncio.get_running_loop().run_in_executor(None, self._queue.join)  # type: ignore[attr-defined]
    await self.aclose()


class ThreadedQueueDrainer:
  """Drains a :class:`queue.Queue` of :class:`~logging.LogRecord` objects and
  forwards them to a central log server over a persistent TCP connection.

  Spins up a private daemon thread on :meth:`start` (or ``__enter__``).  The
  remote logging configuration is passed in at construction time and sent as a
  :class:`~aeth_ext.central_log_server.protocol.ClientHandshake` on every
  (re)connection::

      with ThreadedQueueDrainer(q, "my-app", config, host="logserver") as drainer:
        ...  # records placed on *q* are forwarded while this block runs
  """

  def __init__(
    self,
    queue: _QueueLike[logging.LogRecord],
    target: str,
    host: str = settings.log_conn_host,
    port: int = DEFAULT_TCP_LOGGING_PORT,
    *,
    log_locally: bool = False,
    config_timeout: float = 30.0,
    max_history_records: int = 50_000,
    max_history_bytes: int = 64 * 1024 * 1024,
    max_history_age: float = 300.0,
    emergency_time_threshold: float = 5.0,
    emergency_attempt_threshold: int = 10,
    reconnect_delay: float = 5.0,
  ) -> None:
    # Deferred to break the config_provider → setup → client import cycle.
    # First party imports
    from aeth_ext.central_log_server.client.config_provider import query_logging_configs

    _cfg = query_logging_configs(target, mode="socket", timeout=config_timeout)
    self._queue = queue
    self._program_name = _cfg["program_name"]
    self._config: dict[str, Any] = dict(_cfg["remote"])
    self._host = host
    self._port = port
    self._reconnect_delay = reconnect_delay

    self._history = RecordHistoryBuffer(self._program_name, max_history_records, max_history_bytes, max_history_age)
    checkpoint_path = settings.persisted_dir_loc / "logging_ids.checkpoint"
    self._id_checkpoint: IdCheckpointBackend = ThreadedIdCheckpointBackend(checkpoint_path)
    self._next_id = self._id_checkpoint.load() + 1
    self._last_sent_id = 0

    self._emergency_time_threshold = emergency_time_threshold * 60.0
    self._emergency_attempt_threshold = emergency_attempt_threshold
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    self._emergency_writer: EmergencyHistoryWriter | None = None

    self._local_manager: logging.Manager | None = None
    self._local_root: logging.Logger | None = None
    if log_locally or _cfg["testing"]:
      # First party imports
      from aeth_ext.logging.config import DictConfigurator

      self._local_manager, self._local_root = DictConfigurator(_cfg["local"], log_dir=settings.log_loc_folder).apply(private=True)

    self._stop_event = threading.Event()
    self._thread = threading.Thread(
      target=self._run,
      name=f"queue-drainer-{self._program_name}",
      daemon=False,
    )

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._history.begin_shutdown()
    self._id_checkpoint.begin_shutdown()

  def _finish_shutdown(self) -> None:
    """Threaded-phase teardown (D-I8).

    :attr:`_thread` is deliberately non-daemon, so it would otherwise block
    interpreter exit and the process would sit out the whole grace period before
    being killed. Stopping it here is what makes the early exit (D-I4) reachable
    at all.

    A bounded join rather than ``stop()``'s default of waiting indefinitely: a
    drain thread stuck on an unresponsive socket must not consume the entire
    shutdown budget. If it does outlast the timeout the process falls back to
    SIGKILL, which is the accepted outcome -- the records are already durable by
    this point.
    """
    self._history.flush()
    self.stop(timeout=_LOOP_TEARDOWN_TIMEOUT)

  def start(self) -> None:
    """Start the background drain thread.  Must be called at most once."""
    self._thread.start()

  def stop(self, timeout: float | None = None) -> None:
    """Signal the drain thread to stop and block until it exits.

    ``timeout`` is forwarded to :meth:`threading.Thread.join`; pass ``None``
    (the default) to wait indefinitely.

    Idempotent -- reachable both from ``__exit__`` and from the shutdown
    registry via :meth:`_finish_shutdown`.
    """
    self._history.flush()
    self._stop_event.set()
    self._thread.join(timeout=timeout)
    self._id_checkpoint.close()
    if self._emergency_writer is not None:
      self._emergency_writer.close()
      self._emergency_writer = None
    if self._local_manager is not None and self._local_root is not None:
      # First party imports
      from aeth_ext.central_log_server.server.dispatch import shutdown_hierarchy

      shutdown_hierarchy(self._local_manager, self._local_root)
      self._local_manager = None
      self._local_root = None

  def connect_and_verify(self) -> None:
    """Eagerly connect and fail fast if the server rejects our remote config.

    Normal operation is lazy - the connection opens on the first drain - but
    startup code can call this to surface a rejected handshake as a
    :class:`RuntimeError` immediately instead of silently dropping records
    later.  A merely unreachable server is *not* an error here (the drain
    thread reconnects automatically); only an explicit rejection raises.
    """
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      sock.connect((self._host, self._port))
    except OSError:
      return
    try:
      sock.sendall(encode_json_packet(ClientHandshake(self._program_name, self._config)))
    except OSError:
      with suppress(OSError):
        sock.close()
      return
    ack = self._read_message(sock)
    with suppress(OSError):
      sock.close()
    if isinstance(ack, HandshakeAck) and not ack.ok:
      raise RuntimeError(
        f"Central log server rejected the handshake for {self._program_name!r}: {ack.error or 'rejected without a reason'}"
      )

  def _run(self) -> None:
    while not self._stop_event.is_set():
      sock = self._connect()
      if sock is None:
        self._consecutive_failures += 1
        self._maybe_enter_emergency_mode()
        self._sleep_or_drain(self._reconnect_delay)
        continue
      try:
        self._drain_until_broken(sock)
      finally:
        with suppress(OSError):
          sock.close()
      if not self._stop_event.is_set():
        self._sleep_or_drain(self._reconnect_delay)

  def _connect(self) -> socket.socket | None:
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      sock.connect((self._host, self._port))
    except OSError:
      return None

    try:
      sock.sendall(encode_json_packet(ClientHandshake(self._program_name, self._config)))
    except OSError:
      with suppress(OSError):
        sock.close()
      return None

    ack = self._read_message(sock)
    if isinstance(ack, HandshakeAck):
      if not ack.ok:
        handle_config_rejected(self._program_name, ack.error or "rejected without a reason")
        with suppress(OSError):
          sock.close()
        self._stop_event.set()
        return None
    else:
      handle_ack_read_failure(self._program_name)
      ack = None

    if not self._replay_backlog(ack, sock):
      with suppress(OSError):
        sock.close()
      return None

    return sock

  def _read_message(self, sock: socket.socket, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None:
    """Best-effort read of one server message (D-E5), or ``None`` if malformed/absent/timed out.

    ``ApplySuccess`` is deliberately not in the return type: nothing reads its
    fields, callers only ever need to check for ``ApplyFailure``.
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
      message = decode_server_message(payload)
      return message if isinstance(message, HandshakeAck | ApplyFailure) else None
    except OSError:
      return None
    finally:
      sock.settimeout(previous_timeout)

  def _check_apply_result(self, sock: socket.socket) -> bool:
    """Non-blocking check for the D-E2/D-E2a out-of-band apply-result message.

    Uses ``select`` rather than a dedicated watcher thread, unlike the
    asyncio drainer's task-based approach - this drain loop already polls
    the queue every 0.5s, so a zero-timeout ``select`` check fits into that
    same cadence for free. Returns whether a message was found (regardless
    of outcome), so the caller can stop checking (D-E2a - once resolved,
    never re-checked).
    """
    readable, _, _ = select.select([sock], [], [], 0)
    if not readable:
      return False
    message = self._read_message(sock, timeout=0.5)
    if isinstance(message, ApplyFailure):
      handle_config_rejected(self._program_name, message.error)
    return True

  def _drain_until_broken(self, sock: socket.socket) -> None:
    apply_result_pending = True
    while not self._stop_event.is_set():
      if apply_result_pending:
        apply_result_pending = not self._check_apply_result(sock)
      try:
        record = self._queue.get(timeout=0.5)
      except _queue_mod.Empty:
        continue
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)  # type: ignore[arg-type]
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)
      if self._local_root is not None:
        self._local_root.handle(record)
      if self._emergency_writer is not None:
        entry.persisted = True
        self._emergency_writer.submit(entry)
      payload = orjson.dumps(record_to_payload(record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self._consecutive_failures = 0
      self._last_success_monotonic = monotonic()
      if self._emergency_writer is not None:
        self._exit_emergency_mode()

  def _replay_backlog(self, ack: HandshakeAck | None, sock: socket.socket) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    backlog = self._history.find_after(ack.last_record_id, ack.last_received_at)
    if backlog is None:
      logger.warning(
        "Log server last confirmed record id %s for %r, but it could not be located in history; "
        "some records may already have aged out. Resuming live.",
        ack.last_record_id,
        self._program_name,
      )
      return True
    for entry in backlog:
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        return False
      self._last_sent_id = entry.id
    return True

  def _sleep_or_drain(self, duration: float) -> None:
    """Wait *duration* seconds; in emergency mode drain the queue to the history file instead."""
    if self._emergency_writer is None:
      self._stop_event.wait(timeout=duration)
      return
    deadline = monotonic() + duration
    while not self._stop_event.is_set():
      remaining = deadline - monotonic()
      if remaining <= 0.0:
        break
      try:
        record = self._queue.get(timeout=min(0.5, remaining))
      except _queue_mod.Empty:
        continue
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)  # type: ignore[arg-type]
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)
      entry.persisted = True
      self._emergency_writer.submit(entry)
      if self._local_root is not None:
        self._local_root.handle(record)
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]

  def _maybe_enter_emergency_mode(self) -> None:
    if self._emergency_writer is not None:
      return
    elapsed = monotonic() - self._last_success_monotonic
    if elapsed >= self._emergency_time_threshold and self._consecutive_failures >= self._emergency_attempt_threshold:
      self._emergency_writer = EmergencyHistoryWriter(self._history.history_dir)
      logger.warning(
        "Log server unreachable for %.0fs after %d attempts for %r; writing new records directly to history file",
        elapsed,
        self._consecutive_failures,
        self._program_name,
      )

  def _exit_emergency_mode(self) -> None:
    writer = self._emergency_writer
    if writer is None:
      return
    self._emergency_writer = None
    writer.close()
    logger.info(
      "Log server reachable again for %r; stopped emergency history writer",
      self._program_name,
    )

  def __enter__(self) -> Self:
    self.start()
    return self

  def __exit__(self, *_: object) -> None:
    if hasattr(self._queue, "join"):
      self._queue.join()  # type: ignore[attr-defined]
    self.stop()
